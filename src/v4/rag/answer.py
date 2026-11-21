"""⑧ 合成（規格 15）：組輸入、呼叫 LLM 寫答案；其他題的答案（15.3）；合併整篇（15.2）。

點、段、行程題用 `answer.md`，其他題用 `general.md`，輸入一樣：問句、題型、使用者的行程、查詢狀態表、
每個節點（條件、狀態、整理重點、改了什麼、其他條件下的筆數、天、排法的計算）、被重點引用的證據原文、
解析不到的地點和超過上限沒查的項目、為什麼停和還沒查的事。
"""
import json
import logging

from v4.extract import llm_log

from . import config, plan, status, trace
from .data_schema import DAY, DROP_OVER, DROP_UNRESOLVED, NODE_DAY, Evidence, Node, NodeResult, Question, QState
from .summarize import describe_day
from .validate import Ctx

log = logging.getLogger(__name__)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


def has_evidence(state: QState) -> bool:
    return any(r.found for r in state.results.values())


def cited(state: QState) -> list[Evidence]:
    """被整理重點引用的證據（天本身除外，天在節點的 <days> 裡），照第一次被引用的順序，同一筆只列一次。"""
    by_ref: dict[str, Evidence] = {}
    for _, result in state.pairs():
        for e in result.evidence:
            by_ref.setdefault(e.ref, e)
    refs: list[str] = []
    for _, result in state.pairs():
        s = result.summary
        if s is None:
            continue
        for ref in [*(r for p in s.points for r in p["sources"]), *(n["source"] for n in s.numbers),
                    *(d["source"] for d in s.differences), *(t["source"] for t in s.todo)]:
            ref = ref.strip()
            if ref in by_ref and by_ref[ref].table != DAY and ref not in refs:
                refs.append(ref)
    return [by_ref[r] for r in refs]


def describe_evidence(e: Evidence) -> str:
    flags = "、".join(e.flags) or "沒有"
    return (f'<evidence id="{e.ref}" article="{e.article_id}">\n地點：{e.place}\n時間：{e.time or "沒寫"}\n'
            f"年：{e.year or '沒寫'}\n標記：{flags}\n內容：{e.content}\n</evidence>")


def node_block(node: Node, result: NodeResult | None) -> str:
    lines = [f'<node no="{node.no}">', f"條件：{node.describe()}", f"理由：{node.reason}"]
    if node.relax_from:
        lines.append(f"從 {node.relax_from} 放寬，改了什麼：{'；'.join(node.changes) or '條件一樣'}")
    if result is None:
        lines += ["狀態：還沒查", "</node>"]
        return "\n".join(lines)
    lines.append(f"狀態：{result.status}")
    if node.kind == NODE_DAY and node.has_arrangement:
        lines.append(f"至少一站相同、時間相符的天數（取前 {config.DAY_TOP} 天之前）：{result.same_days}")
    if result.groups:
        lines.append("依地點分組：" + "；".join(f"{name}（{len(refs)} 筆）" for name, refs in result.groups))
    if result.days:
        lines.append("<days>\n" + "\n".join(describe_day(d, node.has_arrangement) for d in result.days) + "\n</days>")
    if result.summary is not None:
        s = result.summary
        lines.append("整理重點：\n" + _json({"重點": s.points, "數字": s.numbers, "情況不同": s.differences}))
    if result.calc:
        lines.append("排法的計算：\n" + _json(result.calc))
    if not result.found:
        lines.append("其他條件下的筆數（都沒有做相關判斷）：\n" + _json(result.other_counts))
    lines.append("</node>")
    return "\n".join(lines)


def build_user(question: Question, ctx: Ctx, state: QState, stop_reason: str) -> str:
    """15.1 的輸入。"""
    evidence = cited(state)
    dropped = [f"- {d.label}：{d.reason}" for d in state.dropped if d.why in (DROP_UNRESOLVED, DROP_OVER)]
    todo = status.todos(state)
    nodes = "\n".join(node_block(n, state.results.get(n.no)) for n in state.nodes)
    return "\n\n".join([
        plan.question_block(question),
        plan.itinerary_block(ctx.days, ctx.places),
        "<status_table>\n" + (_json(status.table(state)) if state.items else "（沒有建任何節點）") + "\n</status_table>",
        "<nodes>\n" + (nodes or "（沒有）") + "\n</nodes>",
        "<evidences>\n" + ("\n".join(describe_evidence(e) for e in evidence) or "（沒有）") + "\n</evidences>",
        "<dropped>\n解析不到的地點、超過上限沒查的項目：\n" + ("\n".join(dropped) or "（沒有）") + "\n</dropped>",
        f"<stop>\n為什麼停：{stop_reason}\n還沒查的事：" + (_json(todo) if todo else "（沒有）") + "\n</stop>",
    ])


async def answer(question: Question, ctx: Ctx, state: QState, stop_reason: str, *,
                 general: bool) -> tuple[str, dict]:
    """寫這一題的答案。回傳（答案，紀錄）。點、段、行程題整題完全沒有證據、答案又沒照規定開頭時，程式補上開頭那句。"""
    prompt = "general.md" if general else "answer.md"
    system = (config.PROMPT_DIR / prompt).read_text()
    user = build_user(question, ctx, state, stop_reason)
    label = f"第 {question.no} 題 合成"
    with trace.step(label, config.MODEL):
        text = await llm_log.call(system, user, None, label, prompt, model=config.MODEL, effort=config.EFFORT)
    text = str(text).strip()
    record = {"提示詞": prompt, "輸入": user}
    if not general and not has_evidence(state) and not text.startswith(config.NO_EVIDENCE_OPENING):
        text = f"{config.NO_EVIDENCE_OPENING}。\n\n{text}"
        record["補上開頭"] = True
    return text, record


def merge(parts: list[tuple[Question, str]]) -> str:
    """15.2：照拆出的順序合在一起，每題前面加「### 第 N 題：{問句}」。"""
    return "\n\n".join(f"### 第 {q.no} 題：{q.text}\n\n{a.strip()}" for q, a in parts)
