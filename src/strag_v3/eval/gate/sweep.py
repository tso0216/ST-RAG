"""semantic gate 的 δ 檢索掃描：只跑檢索、不呼叫 LLM，看片段級指標怎麼隨 δ 變。

沿用 full 結果檔裡每題的檢索計畫，所以各個 δ 的候選片段完全相同；embedding 都讀 chunks.db 的快取，不花錢。
每個 δ × 每題一列，寫到 `result/delta_retrieval_scores.csv`，圖表在 `result/gate.ipynb`。
"""
import csv
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from ...rag.params import Params
from ...rag.plan import Plan
from ...rag.retrieve import Retriever
from ..baseline import chunks
from ..baseline.fragments import Fragment
from ..baseline.judge import Verdict
from ..baseline.metrics import score_question
from ..baseline.questions import Question
from ..baseline.results import Outcome
from .systems import CountingEmbedder

RETRIEVAL_CSV = Path(__file__).resolve().parent / "result" / "delta_retrieval_scores.csv"
# 預設的 δ：0.00、0.02、…、0.60
DELTAS = [round(i * 0.02, 2) for i in range(31)]
SCORE_COLS = ("article_recall", "precision", "recall", "f1", "retrieved", "violations", "empty")


async def sweep(deltas: list[float], questions: list[Question], plans: dict[str, Plan],
                fragments: dict[int, Fragment], db: Path, embedder_of) -> list[dict]:
    """embedder_of(快取的連線) → Embedder。"""
    rows = []
    with closing(chunks.connect()) as cache, closing(sqlite3.connect(db)) as conn:
        base = embedder_of(cache)
        for delta in deltas:
            for q in questions:
                embedder = CountingEmbedder(base)
                sections = await Retriever(conn, embedder, Params(gate="semantic", delta=delta)).retrieve(
                    plans[q.id], q.question)
                evidence = [e for s in sections for e in s.evidence]
                o = Outcome(q.id, contents=list(dict.fromkeys(e.content_id for e in evidence)),
                            articles=list(dict.fromkeys(e.article_id for e in evidence)),
                            backoff=sorted({e.content_id for e in evidence if e.time_backoff or e.space_backoff}))
                s = asdict(score_question(q, o, Verdict(q.id, skipped=True), fragments, "fragment"))
                rows.append({"delta": delta, "id": q.id, "type": q.type,
                             "candidates": embedder.texts - embedder.queries, "passed": len(o.contents),
                             **{k: s[k] for k in SCORE_COLS}})
    return rows


def write_csv(rows: list[dict], path: Path = RETRIEVAL_CSV) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path
