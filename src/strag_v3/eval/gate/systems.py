"""相關判斷（gate）的消融：主系統只換 gate，其他都不動。

| 名稱 | gate | 說明 |
|---|---|---|
| `llm` | `Params.gate = "llm"` | 跟 JEV 同樣的問法，改問 `claude-haiku-5-5`（effort medium），只回是／否 |
| `semantic` | `Params.gate = "semantic"` | aspect 跟片段的 cosine，≥ 最高分 − δ 的全部通過 |

JEV 那一組就是 baseline 的 `strag`（full），不在這裡重跑。規劃器也不重跑：每題沿用 full 結果檔裡存的檢索計畫，
所以三組的候選片段完全一樣，差別只來自 gate。地點只從快取解析（full 跑過，都在快取裡）。
生成器跟 full 相同（`claude-opus-5-5`、主系統的生成器提示詞），不快取；一次呼叫超過 `GENERATE_TIMEOUT` 秒就重問
（CLI 偶爾會掛住不回），試 `GENERATE_TRIES` 次還不行，該題記成錯誤。

每題的 `Outcome.extra["gate"]` 記這題 gate 的成本：
    candidates  交給 gate 判斷的片段數（含讀快取的）
    passed      通過的片段數（退讓時是退讓後那一輪的）
    cached      llm：讀快取的片段數
    input_tokens / output_tokens / cost_usd / duration_ms
                llm：各次呼叫加總；讀快取的照當初那次呼叫的算。cost_usd 是 CLI 照 API 定價估的金額
    embed_texts / embed_chars
                semantic：送去 embedding 的文字數和字元數（含 aspect；不分是否讀快取）
`Outcome.cost.llm_calls` 是規劃器 1（沿用，但照算）＋生成器 1＋llm gate 的呼叫數。
"""
import asyncio
import sqlite3
from dataclasses import asdict, dataclass, field

from service.llm import LLMError

from ...rag import llm as rag_llm
from ...rag.embedding import Embedder
from ...rag.params import Params
from ...rag.plan import Plan
from ...rag.retrieve import Retriever
from ..baseline.questions import Question
from ..baseline.results import Cost, Outcome
from ..baseline import systems as baseline_systems

# 命令列 --system 的選項 → Params.gate
GATES = ("llm", "semantic")
GENERATE_TIMEOUT = 300
GENERATE_TRIES = 3


async def generate(system: str, user: str) -> tuple[str, int]:
    for _ in range(GENERATE_TRIES):
        try:
            return await asyncio.wait_for(baseline_systems.generate(system, user), GENERATE_TIMEOUT)
        except TimeoutError:
            continue
    raise LLMError(f"生成器逾時（{GENERATE_TIMEOUT} 秒，試了 {GENERATE_TRIES} 次）")


class CountingEmbedder:
    """包住另一個 Embedder，數送進去的文字數和字元數。"""

    def __init__(self, inner: Embedder):
        self.inner = inner
        self.model = inner.model
        self.texts = 0
        self.chars = 0
        self.queries = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts += len(texts)
        self.chars += sum(map(len, texts))
        self.queries += 1
        return self.inner.embed(texts)


@dataclass
class GateStrag:
    """conn 要可寫：llm gate 的快取（`llm_gate_cache`）跟 JEV 的快取一樣放在片段的 DB。"""
    conn: sqlite3.Connection
    embedder: Embedder
    params: Params
    # 題目 id → full 那次的檢索計畫
    plans: dict[str, Plan]
    name: str = "gate"
    retrieval: str = "fragment"
    # 所有題目共用的 llm gate 同時請求上限
    _sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(8))

    async def answer(self, q: Question) -> Outcome:
        plan = self.plans[q.id]
        embedder = CountingEmbedder(self.embedder)
        retriever = Retriever(self.conn, embedder, self.params)
        if retriever.llm_gate is not None:
            retriever.llm_gate.sem = self._sem
        sections = await retriever.retrieve(plan, q.question)
        evidence = [e for s in sections for e in s.evidence]
        contents = list(dict.fromkeys(e.content_id for e in evidence))

        gate: dict = {"passed": len(contents)}
        gate_calls = 0
        if retriever.llm_gate is not None:
            u = retriever.llm_gate.usage
            gate |= {"candidates": u.calls, **{k: v for k, v in asdict(u).items() if k != "calls"}}
            gate_calls = u.calls
        else:
            # 每個子查詢的每一輪：1 個 aspect 加上它的候選片段
            gate |= {"candidates": embedder.texts - embedder.queries, "embed_texts": embedder.texts,
                     "embed_chars": embedder.chars}

        system = (rag_llm.PROMPT_DIR / "generator.md").read_text()
        answer, tokens = await generate(system, rag_llm.build_answer_prompt(q.question, sections))
        return Outcome(
            q.id, answer=answer, contents=contents,
            articles=list(dict.fromkeys(e.article_id for e in evidence)),
            backoff=sorted({e.content_id for e in evidence if e.time_backoff or e.space_backoff}),
            cost=Cost(tokens, 2 + gate_calls, 0),
            place_unresolved=any(r.place_unresolved for s in sections for r in s.results),
            extra={"plan": plan.to_dict(), "gate": gate},
        )
