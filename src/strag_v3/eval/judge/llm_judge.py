"""LLM 評審（對照）：跟 JEV 評審同樣的兩種問法、同樣的 `state`，改問 `service/llm.py` 的模型。

structured output 只回是／否，記成 noul 1.0／0.0，所以 `Verdict`、指標、一致率的算法都跟 JEV 評審共用。
不快取。一次呼叫超過 `TIMEOUT` 秒就重問（CLI 偶爾會掛住不回），試 `TRIES` 次還不行就整批停下。
"""
import asyncio
import json
from dataclasses import asdict, dataclass

from service.llm import LLMError, call_detailed

from ..baseline.judge import COVERAGE, PASS, UNSUPPORTED, Judge

MODEL = "claude-sonnet-5-5"
EFFORT = "medium"
TIMEOUT = 180
TRIES = 3

SCHEMA = {"type": "object", "properties": {"answer": {"type": "boolean"}}, "required": ["answer"],
          "additionalProperties": False}
# 輸入 JSON 各欄位的說明；JEV 那邊只靠欄位名稱，這裡補一句讓 LLM 知道每一欄是什麼
FIELDS = {
    "covers_point": "輸入是一個 JSON：question 是旅客的問題，point 是要檢查的重點，answer 是系統的答案。",
    "unsupported_claim": (
        "輸入是一個 JSON：question 是旅客的問題，asked_time 是問的時間，reference_fragments 是標準答案的片段"
        "（place 是地點、time 是該片段的時間標籤、text 是內容），time_unknown_fragments 是時間標籤不明的片段，"
        "answer 是系統的答案。"),
}


def system_prompt(question: dict) -> str:
    return (f"{question['instructions']}\n\n"
            f"- answer = true：{question['criteria']['true']}\n"
            f"- answer = false：{question['criteria']['false']}\n\n"
            f"{FIELDS[question['name']]}")


@dataclass
class JudgeUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


class LLMJudge(Judge):
    """跟 `Judge` 同一個介面（`judge`、`config`），只換掉「問一次」的做法。`usage` 累計這個評審的用量。"""

    def __init__(self, model: str = MODEL, effort: str = EFFORT, n: int = 1, concurrency: int = 8, call=None):
        self.model = model
        self.effort = effort
        self.n = n
        self.sem = asyncio.Semaphore(concurrency)
        self.call = call or call_detailed  # 測試時換成假的
        self.usage = JudgeUsage()

    def config(self) -> dict:
        return {"model": self.model, "effort": self.effort, "n": self.n, "pass": PASS,
                "coverage": COVERAGE, "unsupported": UNSUPPORTED,
                "system_prompts": {q["name"]: system_prompt(q) for q in (COVERAGE, UNSUPPORTED)},
                "usage": asdict(self.usage)}

    async def _ask(self, state: dict, question: dict) -> list[float]:
        system = system_prompt(question)
        user = json.dumps(state, ensure_ascii=False)
        return list(await asyncio.gather(*(self._once(system, user) for _ in range(self.n))))

    async def _once(self, system: str, user: str) -> float:
        async with self.sem:
            for _ in range(TRIES):
                try:
                    out, u = await asyncio.wait_for(self.call(system, user, SCHEMA, self.model, self.effort), TIMEOUT)
                    break
                except TimeoutError:
                    continue
            else:
                raise LLMError(f"評審逾時（{TIMEOUT} 秒，試了 {TRIES} 次）")
        self.usage.calls += 1
        self.usage.input_tokens += u.input_tokens
        self.usage.output_tokens += u.output_tokens
        self.usage.cost_usd += u.cost_usd
        self.usage.duration_ms += u.duration_ms
        return 1.0 if out["answer"] else 0.0
