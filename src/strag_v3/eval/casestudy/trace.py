"""帶紀錄的檢索：跟主系統的 `Retriever` 行為完全相同，另外記下每個子查詢試過的每一層。

一層的紀錄：哪一層（原條件、時間退讓、空間退讓）、用的時間條件、空間退讓到哪個行政區、
地點範圍內的片段數（pool）→ 時間相符的片段數（time_matched）→ 相關判斷通過的片段數（passed），
以及每個候選片段的相關分數（judged）。只包住 `_retrieve_one` 和 `_run`，不改它們的邏輯。
"""
from dataclasses import asdict

from ...rag.evidence import Evidence, SubQueryResult
from ...rag.plan import SubQuery
from ...rag.retrieve import Retriever
from ...rag.store import Content
from ...rag.timematch import TimeQuery

EXACT, TIME_BACKOFF, SPACE_BACKOFF, NONE, UNRESOLVED = "原條件", "時間退讓", "空間退讓", "沒有證據", "地點解析不到"


class TracingRetriever(Retriever):
    """`trace` 依子查詢的順序，一個子查詢一筆。只支援預設的 gate=jev。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.jev_gate is None:
            raise ValueError("案例研究只跑 full（gate=jev）")
        self.trace: list[dict] = []
        self._last: tuple[list[Content], list[float]] = ([], [])
        nouls = self.jev_gate.nouls

        async def recorded(question, aspect, candidates):
            rels = await nouls(question, aspect, candidates)
            self._last = (list(candidates), list(rels))
            return rels
        self.jev_gate.nouls = recorded  # type: ignore[method-assign]

    async def _retrieve_one(self, sq: SubQuery, question: str) -> SubQueryResult:
        entry: dict = {"sub_query": asdict(sq), "layers": []}
        self.trace.append(entry)
        result = await super()._retrieve_one(sq, question)
        if result.place_unresolved:
            entry["stage"] = UNRESOLVED
        else:
            entry["stage"] = entry["layers"][-1]["layer"] if result.evidence else NONE
        entry["available"] = asdict(result.available)
        return result

    async def _run(self, sq: SubQuery, question: str, pool: list[Content], time: TimeQuery | None,
                   time_backoff: bool, backoff_area: str | None) -> list[Evidence]:
        self._last = ([], [])
        evidence = await super()._run(sq, question, pool, time, time_backoff, backoff_area)
        candidates, rels = self._last
        passed = {e.content_id for e in evidence}
        self.trace[-1]["layers"].append({
            "layer": SPACE_BACKOFF if backoff_area else TIME_BACKOFF if time_backoff else EXACT,
            "time": asdict(time) if time else None,
            "area": backoff_area,
            "pool": len(pool),
            "time_matched": sum(time is None or time.matches(c.time) for c in pool),
            "passed": len(evidence),
            "judged": [{"id": c.id, "place": c.place, "noul": round(r, 3), "passed": c.id in passed}
                       for c, r in zip(candidates, rels)],
        })
        return evidence
