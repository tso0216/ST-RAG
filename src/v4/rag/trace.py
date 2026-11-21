"""紀錄（規格 18）：收集每一步的結果、寫成 JSON。

- `step(label, model)`：標出現在在做哪一步、用哪個模型；`Calls` 記每次 LLM 呼叫時用它。
  用 ContextVar，同時跑的好幾個節點各記各的。
- `Calls`：交給 `service.llm.track()` 的 `Tally`，另外記下每次呼叫的步驟、模型、token、估計金額、時間。
- `node_record`、`result_record`、`dropped_record`：節點、節點結果、拿掉的節點寫成紀錄的樣子。
- `write`：寫成 JSON 檔。

紀錄的整體結構由 `pipeline.py` 組，這裡只放共用的寫法。
"""
import copy
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path

from service import llm

from .data_schema import NODE_MOVE, NODE_POINT, DayView, Dropped, Evidence, Node, NodeResult, Scope

_step: ContextVar[tuple[str, str | None]] = ContextVar("rag_step", default=("", None))


@contextmanager
def step(label: str, model: str | None) -> Iterator[None]:
    """這個區塊裡的 LLM 呼叫記成這一步、這個模型。"""
    token = _step.set((label, model))
    try:
        yield
    finally:
        _step.reset(token)


@dataclass
class Calls(llm.Tally):
    """用量合計，另外記每一次呼叫。"""
    records: list[dict] = field(default_factory=list)

    def add(self, usage: llm.Usage) -> None:
        super().add(usage)
        label, model = _step.get()
        self.records.append({"步驟": label, "模型": model, "輸入 token": usage.input_tokens,
                             "快取讀取 token": usage.cache_read_tokens, "輸出 token": usage.output_tokens,
                             "估計金額（美元）": round(usage.cost_usd, 6), "時間（毫秒）": usage.duration_ms})

    def summary(self) -> dict:
        return {"次數": self.calls, "輸入 token": self.input_tokens, "快取讀取 token": self.cache_read_tokens,
                "輸出 token": self.output_tokens, "估計金額（美元）": round(self.cost_usd, 6),
                "每一次": self.records}


def scope_record(sc: Scope | None) -> dict | None:
    if sc is None:
        return None
    out: dict = {"寫法": sc.mode}
    if sc.where is not None:
        w = sc.where
        out.update({"地點": w.label, "編號": w.ref, "日文名稱": w.name_ja, "城市": w.city, "種類": w.kind,
                    "place_id": w.place_id})
    if sc.km is not None:
        out["半徑（公里）"] = sc.km
    return out


def node_record(n: Node) -> dict:
    out: dict = {"編號": n.no, "種類": n.kind, "條件": n.describe(), "第幾輪": n.round, "時間條件": n.time.parts(),
                 "面向": n.aspect, "理由": n.reason}
    if n.kind == NODE_POINT:
        out["對象"] = scope_record(n.target)
    elif n.kind == NODE_MOVE:
        out.update({"起點": scope_record(n.origin), "終點": scope_record(n.dest), "反方向也要": n.both_ways})
    else:
        out["排法"] = list(n.arrangement)
        out["排法展開"] = [{"站": s.where.label, "place_id": s.where.place_id, "從哪一天": s.day, "種類": s.type}
                       for s in n.stops]
    if n.relax_from:
        out["從哪個節點放寬"] = n.relax_from
        out["改了什麼"] = list(n.changes)
    out["LLM 的輸出"] = n.raw
    return out


def evidence_record(e: Evidence) -> dict:
    return {"編號": e.ref, "文章": e.article_id, "地點": e.place, "組": e.group, "時間": e.time, "年": e.year,
            "分數": e.score, "標記": list(e.flags)}


def day_record(d: DayView) -> dict:
    return {"編號": f"day {d.id}", "文章": d.article_id, "標題": d.title, "發文時間": d.posted_at, "第幾天": d.no,
            "那篇有幾天": d.days_in_article, "月份": d.month,
            "站": [f"{s['name']}（{s['type']}{'，相同' if s['same'] else ''}）" for s in d.stops],
            "相同的站": list(d.same), "時間不明": list(d.unknown),
            "這天的評語": [n["id"] for n in d.notes], "整趟的評語": [n["id"] for n in d.trip_notes]}


def result_record(r: NodeResult) -> dict:
    return {
        "狀態": r.status,
        "候選筆數": r.candidates,
        "時間相符筆數": r.time_matched,
        "embedding 先取了幾筆": r.embedded,
        "送相關判斷筆數": r.judged,
        "通過相關判斷筆數": r.passed,
        "至少一站相同、時間相符的天數": r.same_days,
        "證據": [evidence_record(e) for e in r.evidence],
        "分組": [{"組": name, "證據": refs} for name, refs in r.groups],
        "整理重點": asdict(r.summary) if r.summary is not None else None,
        "其他條件下的筆數": copy.deepcopy(r.other_counts),
        "天": [day_record(d) for d in r.days],
        "排法的計算": copy.deepcopy(r.calc),
    }


def dropped_record(d: Dropped) -> dict:
    return {"第幾個": d.index, "第幾輪": d.round, "原因": d.why, "說明": d.reason, "節點": d.label,
            "LLM 的輸出": d.raw}


def write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(record) + "\n", encoding="utf-8")


def dumps(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, indent=1, default=str)
