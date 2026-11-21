"""⑥ 查詢狀態表：每一項的狀態、放寬追回原本的項目、還沒查的事去重複、要不要補規劃。"""
from tests.v4.rag import fakes
from v4.rag import config, status
from v4.rag.data_schema import (AT, DROP_DUPLICATE, DROP_FORMAT, DROP_OVER, DROP_UNRESOLVED, HAS, NODE_POINT, NONE,
                                ST_HAS, ST_NONE, ST_OVER, ST_RELAXED, ST_UNRESOLVED, TODO_PLACE, Batch, Dropped,
                                Evidence, Node, NodeResult, QState, Scope, Summary, TimeCond, Where)


def node(no, months=(10,), relax_from="", name="淺草寺", pid=1, round_no=0):
    return Node(no, NODE_POINT, "人潮", "理由", time=TimeCond(months=months),
                target=Scope(AT, Where(name_zh=name, place_id=pid)), relax_from=relax_from, round=round_no)


def found(n=1, todo=()):
    ev = [Evidence("place_note", f"1-{i}", "M.1.A.AAA", "人很多", "淺草寺") for i in range(n)]
    return NodeResult(HAS, evidence=ev, summary=Summary(todo=list(todo)))


def nothing():
    return NodeResult(NONE, other_counts={"時間相符": 0, "全部": 3})


def batch(*items):
    """items：Node 或 Dropped，照輸出順序。"""
    b = Batch()
    for i, x in enumerate(items):
        if isinstance(x, Node):
            b.accepted.append(x)
            b.order.append(("node", x.no, i))
        else:
            b.dropped.append(x)
            b.order.append(("drop", x, i))
    return b


def drop(i, why, label="點｜謎之店｜面向：人潮"):
    return Dropped(i, why, f"原因 {why}", label, {})


def states(state):
    return [row["狀態"].split("（")[0] for row in status.table(state)]


def test_items_and_statuses():
    state = QState()
    status.add_batch(state, batch(node("N1"), drop(1, DROP_UNRESOLVED), node("N2", name="雷門", pid=2),
                                  drop(3, DROP_OVER), drop(4, DROP_FORMAT), drop(5, DROP_DUPLICATE)), initial=True)
    state.results = {"N1": found(2), "N2": nothing()}
    rows = status.table(state)
    assert [r["狀態"] for r in rows] == [f"{ST_HAS}（2 筆）", ST_UNRESOLVED, ST_NONE, ST_OVER]
    assert rows[2]["其他條件下的筆數"] == {"N2": {"時間相符": 0, "全部": 3}}


def test_relaxed_traced_back_to_original_item():
    state = QState()
    status.add_batch(state, batch(node("N1")), initial=True)
    state.results["N1"] = nothing()
    status.add_batch(state, batch(node("N2", months=(10, 11), relax_from="N1", round_no=1)), initial=False)
    state.results["N2"] = nothing()
    status.add_batch(state, batch(node("N3", months=(), relax_from="N2", round_no=2),
                                  node("N4", name="雷門", pid=2, round_no=2)), initial=False)
    state.results.update({"N3": found(3), "N4": nothing()})
    rows = status.table(state)
    assert [r.get("節點") for r in rows] == ["N1", "N4"]           # 放寬來的不是新項目，新的一件事是
    assert rows[0]["狀態"] == f"{ST_RELAXED}（3 筆；月份 10 → 不限）"
    assert rows[0]["用的節點"] == "N3"
    assert states(state) == [ST_RELAXED, ST_NONE]


def test_relaxed_but_still_nothing_lists_all_counts():
    state = QState()
    status.add_batch(state, batch(node("N1")), initial=True)
    status.add_batch(state, batch(node("N2", months=(11,), relax_from="N1")), initial=False)
    state.results = {"N1": nothing(), "N2": nothing()}
    [row] = status.table(state)
    assert row["狀態"] == ST_NONE and set(row["其他條件下的筆數"]) == {"N1", "N2"}


def test_dropped_in_replan_is_not_an_item():
    state = QState()
    status.add_batch(state, batch(node("N1")), initial=True)
    status.add_batch(state, batch(drop(0, DROP_UNRESOLVED)), initial=False)
    assert len(status.table(state)) == 1 and len(state.dropped) == 1


def test_todos_deduplicated():
    place = {"type": TODO_PLACE, "text": "大家順路去仲見世", "source": "place_note 1-0", "name_zh": "仲見世",
             "name_ja": "仲見世通り", "city": "東京", "kind": "景點"}
    state = QState(nodes=[node("N1"), node("N2", name="雷門", pid=2, round_no=1)])
    state.results = {
        "N1": found(todo=[place, fakes.todo_question("週一休館嗎", "place_note 1-0")]),
        "N2": found(todo=[{**place, "text": "換個說法", "name_zh": "仲見世商店街"},
                          fakes.todo_question(" 週一休館嗎 ", "place_note 1-0"),
                          fakes.todo_question("要預約嗎", "place_note 1-0")]),
    }
    todos = status.todos(state)
    assert [(t["說明"], t["節點"]) for t in todos] == [("大家順路去仲見世", "N1"), ("週一休館嗎", "N1"), ("要預約嗎", "N2")]


def test_no_data_lists_nodes_without_evidence():
    state = QState(nodes=[node("N1"), node("N2", name="雷門", pid=2)])
    state.results = {"N1": found(), "N2": nothing()}
    assert [x["節點"] for x in status.no_data(state)] == ["N2"]


def test_should_replan():
    state = QState(nodes=[node(f"N{i}") for i in range(1, 4)])
    assert status.should_replan(state)
    state.replans = config.REPLAN_ROUNDS
    assert not status.should_replan(state)
    full = QState(nodes=[node(f"N{i}") for i in range(1, config.MAX_NODES + 1)])
    assert not status.should_replan(full)
