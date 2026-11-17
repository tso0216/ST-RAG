"""量 JEV gate 的 token 用量：full 的結果檔只記了判斷的片段數，沒記 token，這裡把同樣的請求重問一次只為了拿用量。

沿用 full 結果檔的檢索計畫，候選片段跟 full 相同；每個候選片段照 `JEVGate` 的問法實際再問 JEV 一次（不讀快取），
記下回應的 `usage`，但是否通過仍照 `jev_cache` 裡當初的 noul，所以退讓的走法跟 full 一樣，也不改快取。
每題一列寫到 `result/jev_gate_usage.csv`。會真的呼叫 JEV（約 500 次，照定價不到 0.05 美元）。
"""
import csv
import sqlite3
from contextlib import closing
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient, Noul, NoulCriteria, RetryPolicy

from service.jev import NAME, RETRIES, JEVClient

from ...rag.params import Params
from ...rag.plan import Plan
from ...rag.relevance import CRITERIA, INSTRUCTIONS, JEVGate
from ...rag.retrieve import Retriever
from ...rag.store import Content
from ..baseline import chunks
from ..baseline.questions import Question

USAGE_CSV = Path(__file__).resolve().parent / "result" / "jev_gate_usage.csv"


class UsageJEVGate(JEVGate):
    """每個候選片段都實際問一次 JEV 記用量；回傳的 noul 用快取裡的（沒有快取才用這次的）。"""

    def __init__(self, conn: sqlite3.Connection, raw: AsyncTypeSafeClient, model: str, concurrency: int):
        super().__init__(conn, JEVClient(raw), model, concurrency)
        self.raw = raw
        self.calls = self.input_tokens = self.output_tokens = self.unreported = 0

    async def _one(self, question: str, aspect: str | None, c: Content) -> float:
        state = {"question": question, **({"aspect": aspect} if aspect else {}), "passage": c.text}
        ask = Noul(instructions=INSTRUCTIONS, criteria=NoulCriteria(true=CRITERIA["true"], false=CRITERIA["false"]))
        async with self.sem:
            r = await self.raw.system_one(state=state, questions={NAME: ask}, model=self.model)
        self.calls += 1
        if r.usage.input_tokens is None:
            self.unreported += 1
        self.input_tokens += r.usage.input_tokens or 0
        self.output_tokens += r.usage.output_tokens or 0
        return await super()._one(question, aspect, c)


async def measure(questions: list[Question], plans: dict[str, Plan], db: Path, embedder_of) -> list[dict]:
    params = Params(gate="jev")
    raw = AsyncTypeSafeClient(retry=RetryPolicy(max_retries=RETRIES))
    rows = []
    try:
        with closing(chunks.connect()) as cache, closing(sqlite3.connect(db)) as conn:
            embedder = embedder_of(cache)
            for q in questions:
                retriever = Retriever(conn, embedder, params, jev=JEVClient(raw))
                gate = UsageJEVGate(conn, raw, params.jev_model, params.jev_concurrency)
                retriever.jev_gate = gate
                await retriever.retrieve(plans[q.id], q.question)
                rows.append({"id": q.id, "type": q.type, "model": params.jev_model, "calls": gate.calls,
                             "input_tokens": gate.input_tokens, "output_tokens": gate.output_tokens,
                             "unreported": gate.unreported})
                print(f"[{len(rows)}/{len(questions)}] {q.id}  {gate.calls} 次  輸入 {gate.input_tokens} token", flush=True)
    finally:
        await raw.aclose()
    return rows


def write_csv(rows: list[dict], path: Path = USAGE_CSV) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path
