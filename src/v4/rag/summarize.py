"""整理重點（規格 12.6）：組輸入、呼叫 LLM、檢查輸出、重問；重問後還有問題的那幾則拿掉。

點、段、天節點有證據時，每個節點呼叫一次。輸入是問句、節點、所有證據（編號、地點、時間、年、內容），
天節點另外附挑出或列出的天。證據編號是「表名 id」；從天的站看出來的重點引用那一天（`day <id>`）。
"""
import asyncio
import logging

from v4.extract import feedback
from v4.extract.check import Problem

from . import config, trace
from .data_schema import (DAY, DAY_NOTE, MINUTE, MOVE_TIME, PLACE_KINDS, STAY_TIME, SUMMARY_SCHEMA, TODO_PLACE,
                          TODO_QUESTION, DayView, Evidence, Node, Summary)

log = logging.getLogger(__name__)

_KINDS = ("points", "numbers", "differences", "todo")


def describe_day(d: DayView, with_same: bool) -> str:
    """一天給 LLM 看的寫法。"""
    stops = " → ".join(f"{i}. {s['name']}（{s['type']}{'，相同' if s.get('same') else ''}）"
                       for i, s in enumerate(d.stops, 1))
    lines = [f'<day id="{DAY} {d.id}" article="{d.article_id}" title="{d.title}" posted="{d.posted_at[:10]}"'
             f' day_no="{d.no}" days_in_article="{d.days_in_article}" month="{d.month or "沒寫"}">',
             f"站：{stops or '（沒有站）'}"]
    if with_same:
        lines.append(f"相同的站（{d.same_count} 站）：{'、'.join(d.same) or '沒有'}")
    if d.unknown:
        lines.append(f"時間不明：{'、'.join(d.unknown)}")
    for n in d.notes:
        lines.append(f"這天的評語 [{DAY_NOTE} {n['id']}]：{n['content']}")
    for n in d.trip_notes:
        lines.append(f"整趟的評語 [{DAY_NOTE} {n['id']}]：{n['content']}")
    lines.append("</day>")
    return "\n".join(lines)


def describe_evidence(e: Evidence) -> str:
    flags = f"\n標記：{'、'.join(e.flags)}" if e.flags else ""
    return (f'<evidence id="{e.ref}">\n地點：{e.place}\n時間：{e.time or "沒寫"}\n年：{e.year or "沒寫"}{flags}\n'
            f"內容：{e.content}\n</evidence>")


def build_user(question: str, node: Node, evidence: list[Evidence], days: list[DayView]) -> str:
    parts = [f"<question>{question}</question>", f"<node>{node.describe()}</node>"]
    if days:
        parts.append("<days>\n" + "\n".join(describe_day(d, node.has_arrangement) for d in days) + "\n</days>")
    shown = [e for e in evidence if e.table != DAY]   # 天本身已經在 <days> 裡
    parts.append("<evidences>\n" + "\n".join(describe_evidence(e) for e in shown) + "\n</evidences>")
    return "\n\n".join(parts)


def valid_sources(evidence: list[Evidence], days: list[DayView]) -> set[str]:
    refs = {e.ref for e in evidence}
    for d in days:
        refs.add(f"{DAY} {d.id}")
        refs.update(f"{DAY_NOTE} {n['id']}" for n in [*d.notes, *d.trip_notes])
    return refs


def check(out: dict, sources: set[str]) -> list[Problem]:
    """程式檢查：引用的證據編號都在輸入裡；數字的最少 ≤ 最多、都不是負的，種類和單位不是空的
    （停留時間、移動時間的單位要是分鐘）；地點類的還沒查的事有日文名稱。"""
    problems: list[Problem] = []

    def source(kind: str, i: int, ref: str) -> None:
        if ref.strip() not in sources:
            problems.append(Problem(kind, i, f"證據編號「{ref}」不在輸入裡；要照 <evidence id> 或 <day id> 的寫法"))

    for i, p in enumerate(out.get("points", [])):
        if not p["text"].strip():
            problems.append(Problem("points", i, "text 是空的"))
        if not p["sources"]:
            problems.append(Problem("points", i, "sources 至少要有一筆證據編號"))
        for ref in p["sources"]:
            source("points", i, ref)
    for i, n in enumerate(out.get("numbers", [])):
        source("numbers", i, n["source"])
        if n["min"] < 0 or n["max"] < 0:
            problems.append(Problem("numbers", i, "min、max 不能是負的"))
        if n["min"] > n["max"]:
            problems.append(Problem("numbers", i, f"min {n['min']:g} 比 max {n['max']:g} 大"))
        if not n["kind"].strip():
            problems.append(Problem("numbers", i, "kind 是空的"))
        if not n["unit"].strip():
            problems.append(Problem("numbers", i, "unit 是空的"))
        elif n["kind"].strip() in (STAY_TIME, MOVE_TIME) and n["unit"].strip() != MINUTE:
            problems.append(Problem("numbers", i, f"{n['kind']} 的 unit 要是 {MINUTE}（換算成分鐘）"))
    for i, d in enumerate(out.get("differences", [])):
        source("differences", i, d["source"])
        if not d["what"].strip():
            problems.append(Problem("differences", i, "what 是空的"))
    for i, t in enumerate(out.get("todo", [])):
        source("todo", i, t["source"])
        if not t["text"].strip():
            problems.append(Problem("todo", i, "text 是空的"))
        if t["type"] not in (TODO_PLACE, TODO_QUESTION):
            problems.append(Problem("todo", i, f"type 要是 {TODO_PLACE} 或 {TODO_QUESTION}"))
        elif t["type"] == TODO_PLACE:
            if not t["name_ja"].strip():
                problems.append(Problem("todo", i, "地點要有 name_ja（日文名稱）"))
            if t["kind"] and t["kind"] not in PLACE_KINDS:
                problems.append(Problem("todo", i, f"kind 要是 {'或'.join(PLACE_KINDS)}"))
    return problems


async def summarize(question: str, node: Node, evidence: list[Evidence], days: list[DayView],
                    sem: asyncio.Semaphore) -> Summary:
    system = (config.PROMPT_DIR / "summarize.md").read_text()
    user = build_user(question, node, evidence, days)
    sources = valid_sources(evidence, days)
    label = f"整理重點 {node.no}"
    async with sem:
        with trace.step(label, config.SUMMARIZE_MODEL):
            out, problems = await feedback.ask(
                system, user, SUMMARY_SCHEMA, lambda o: check(o, sources), label, "summarize.md",
                model=config.SUMMARIZE_MODEL, effort=config.SUMMARIZE_EFFORT, retries=config.RETRIES)
    bad = {(p.kind, p.index) for p in problems}
    kept = {k: [x for i, x in enumerate(out.get(k, [])) if (k, i) not in bad] for k in _KINDS}
    dropped = [str(p) for p in problems]
    if dropped:
        log.info("整理重點 %s：重問後還有問題，拿掉 %d 則", node.no, len(bad))
    return Summary(points=kept["points"], numbers=kept["numbers"], differences=kept["differences"],
                   todo=kept["todo"], dropped=dropped)
