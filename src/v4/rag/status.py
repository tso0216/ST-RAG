"""⑥ 查詢狀態表（規格 13）：每一項有沒有證據、還沒查的事、查無資料清單、要不要補規劃。每一輪跑完之後重建。

項目：③ 輸出的每個節點各一項（包括 ④ 因為地點解析不到、超過節點上限而拿掉的），
加上 ⑦ 加的、不是從別的節點放寬來的節點。放寬來的節點（可以放寬好幾次）一路追回原本的項目。
"""
from . import config
from .data_schema import (DROP_OVER, DROP_UNRESOLVED, HAS_NO_NOTE, NODE_DAY, ST_HAS, ST_NONE, ST_OVER, ST_RELAXED,
                          ST_UNRESOLVED, TODO_PLACE, Batch, Dropped, Node, NodeResult, QState)
from .validate import diff


def add_batch(state: QState, batch: Batch, *, initial: bool) -> None:
    """把 ③ 或 ⑦ 一輪的結果加進這一題的狀態。

    項目（13.1）：③ 輸出的每個節點各一項，包括因為地點解析不到、超過節點上限而拿掉的；
    ⑦ 加的節點只有不是從別的節點放寬來的才是新的一項。
    """
    state.nodes.extend(batch.accepted)
    state.dropped.extend(batch.dropped)
    for kind, value, _ in batch.order:
        if kind == "node":
            if initial or not state.node(value).relax_from:
                state.items.append(("node", value))
        elif initial and value.why in (DROP_UNRESOLVED, DROP_OVER):
            state.items.append(("drop", value))


def root_of(state: QState, node: Node) -> Node:
    """一路照「從哪個節點放寬」追回原本的節點。"""
    seen = set()
    while node.relax_from and node.relax_from not in seen:
        seen.add(node.relax_from)
        node = state.node(node.relax_from)
    return node


def descendants(state: QState, root: Node) -> list[Node]:
    """從 root 放寬來的節點（含放寬好幾次的），照編號順序。"""
    return [n for n in state.nodes if n.no != root.no and n.relax_from and root_of(state, n).no == root.no]


def _count(state: QState, node: Node) -> str:
    r = state.results[node.no]
    extra = "，沒有評語" if r.status == HAS_NO_NOTE else ""
    return f"{len(r.evidence)} 筆{extra}"


def table(state: QState) -> list[dict]:
    """查詢狀態表：一項一列。"""
    rows = []
    for kind, value in state.items:
        if kind == "drop":
            d: Dropped = value
            status = ST_UNRESOLVED if d.why == DROP_UNRESOLVED else ST_OVER
            rows.append({"項目": d.label, "狀態": status, "說明": d.reason})
            continue
        root = state.node(value)
        result = state.results.get(root.no)
        row: dict = {"項目": root.describe(), "節點": root.no}
        if result is None:
            row["狀態"] = "還沒查"
        elif result.found:
            row["狀態"] = f"{ST_HAS}（{_count(state, root)}）"
        else:
            relaxed = [n for n in descendants(state, root) if n.no in state.results and state.results[n.no].found]
            if relaxed:
                best = max(relaxed, key=lambda n: (len(state.results[n.no].evidence), -int(n.no[1:])))
                changes = "、".join(diff(root, best)) or "條件一樣"
                row["狀態"] = f"{ST_RELAXED}（{_count(state, best)}；{changes}）"
                row["用的節點"] = best.no
            else:
                row["狀態"] = ST_NONE
                row["其他條件下的筆數"] = {n.no: state.results[n.no].other_counts
                                   for n in [root, *descendants(state, root)] if n.no in state.results}
        if root.kind == NODE_DAY and root.has_arrangement and result is not None and result.calc:
            row["排法的計算缺"] = result.calc["缺"]
        rows.append(row)
    return rows


def todos(state: QState) -> list[dict]:
    """還沒查的事：所有節點整理重點列出的，合在一起。地點用日文名稱加城市去重複，問題用文字去重複。
    每則記下第一次出現在哪一輪、出自哪個節點的哪筆證據。"""
    out: list[dict] = []
    seen: set[tuple] = set()
    for node in sorted(state.nodes, key=lambda n: (n.round, int(n.no[1:]))):
        result = state.results.get(node.no)
        if result is None or result.summary is None:
            continue
        for t in result.summary.todo:
            if t["type"] == TODO_PLACE:
                key = (TODO_PLACE, t["name_ja"].strip(), t["city"].strip())
            else:
                key = (t["type"], t["text"].strip())
            if key in seen:
                continue
            seen.add(key)
            entry = {"種類": t["type"], "說明": t["text"].strip(), "第一次出現": node.round, "節點": node.no,
                     "證據": t["source"].strip()}
            if t["type"] == TODO_PLACE:
                entry.update({"繁中名稱": t["name_zh"].strip(), "日文名稱": t["name_ja"].strip(),
                              "城市": t["city"].strip(), "景點或區域": t["kind"]})
            out.append(entry)
    return out


def no_data(state: QState) -> list[dict]:
    """查無資料清單：每個沒有證據的節點，附上它的條件和其他條件下的筆數。"""
    return [{"節點": n.no, "條件": n.describe(), "其他條件下的筆數": state.results[n.no].other_counts}
            for n in state.nodes if n.no in state.results and not state.results[n.no].found]


def should_replan(state: QState) -> bool:
    """13.4：補規劃還沒滿 2 輪、整題的節點還沒到 40 個就呼叫 ⑦。"""
    return state.replans < config.REPLAN_ROUNDS and len(state.nodes) < config.MAX_NODES


def snapshot(state: QState) -> dict:
    """一輪結束時記進紀錄的：查詢狀態表、還沒查的事、查無資料清單。"""
    return {"查詢狀態表": table(state), "還沒查的事": todos(state), "查無資料清單": no_data(state)}


def result_brief(node: Node, result: NodeResult | None) -> dict:
    """一個節點的條件和結果摘要（給 ⑦ 看）。"""
    out: dict = {"節點": node.no, "條件": node.describe(), "理由": node.reason, "第幾輪加的": node.round}
    if result is None:
        out["狀態"] = "還沒查"
        return out
    out["狀態"] = result.status
    if node.kind == NODE_DAY and node.has_arrangement:
        out["至少一站相同的天數（不看時間）"] = result.candidates
        out["至少一站相同、時間相符的天數"] = result.time_matched
        out["挑出的天"] = [f"day {d.id}（{d.title}，第 {d.no} 天，{d.month or '沒寫'} 月，相同 {d.same_count} 站："
                         f"{'、'.join(d.same)}）" for d in result.days]
    else:
        out["候選筆數"] = result.candidates
        out["時間相符筆數"] = result.time_matched
        if result.embedded is not None:
            out["embedding 先取的筆數"] = result.embedded
        out["通過相關判斷筆數"] = result.passed
        if result.days:
            out["帶出的天"] = [f"day {d.id}（{d.title}，第 {d.no} 天）" for d in result.days]
    if result.summary is not None:
        out["重點"] = [f"{p['text']}（{'、'.join(p['sources'])}）" for p in result.summary.points]
        out["數字"] = [f"{n['kind']} {n['min']:g}–{n['max']:g} {n['unit']}（{n['source']}）"
                     for n in result.summary.numbers]
        if result.summary.differences:
            out["情況不同"] = [f"{d['source']}：{d['what']}" for d in result.summary.differences]
    if result.calc:
        out["排法的計算"] = {"合計": result.calc["合計"], "直線距離加總": result.calc["直線距離加總"],
                        "缺": result.calc["缺"]}
    if not result.found:
        out["其他條件下的筆數"] = result.other_counts
    return out
