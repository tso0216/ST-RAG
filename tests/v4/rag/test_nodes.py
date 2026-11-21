"""⑤ 執行節點：點、段、天節點撈到哪些證據、其他條件下的筆數，和排法的計算。"""
import asyncio

import pytest

from tests.v4.rag import fakes
from tests.v4.rag.fakes import NEAR, RELEVANT
from v4.rag import config, days, nodes, run
from v4.rag.data_schema import (ANYWHERE, AROUND, AT, DAY, DAY_NOTE, FLAG_BY_NAME, FLAG_REVERSED, FLAG_UNKNOWN, HAS,
                                HAS_NO_NOTE, NODE_DAY, NODE_MOVE, NODE_POINT, NONE, STAY, Evidence, Node, NodeResult,
                                QState, Scope, Stop, Summary, TimeCond, Where)

R = RELEVANT


def where(conn, pid, name=None):
    if pid is None:
        return Where(name_zh=name)
    row = conn.execute("SELECT name_zh FROM place WHERE id = ?", (pid,)).fetchone()
    return Where(name_zh=row[0], place_id=pid)


def at(conn, pid):
    return Scope(AT, where(conn, pid))


def point(target, time=TimeCond(), aspect="人潮", no="N1"):
    return Node(no, NODE_POINT, aspect, "", time=time, target=target)


def move(origin, dest, both_ways=False, time=TimeCond(), no="N1"):
    return Node(no, NODE_MOVE, "移動時間", "", time=time, origin=origin, dest=dest, both_ways=both_ways)


def day(conn, places, time=TimeCond(), no="N1"):
    """天節點；places 是排法的站（place id，或解析不到的名稱）。沒有站是沒有排法。"""
    stops = tuple(Stop(where(conn, p) if isinstance(p, int) else where(conn, None, p)) for p in places)
    return Node(no, NODE_DAY, "怎麼排", "", time=time, arrangement=tuple(s.where.label for s in stops), stops=stops)


def go(node, env):
    return asyncio.run(run.run_node(node, env))


def ids(result):
    return [e.id for e in result.evidence]


@pytest.fixture
def asakusa(world):
    """淺草（區域）裡有淺草寺、雷門；晴空塔在範圍外、離淺草寺 1.4 公里。"""
    class P:
        area = world.area("淺草", 35.712, 139.795, 0.008)
        temple = world.place("淺草寺", "浅草寺", lat=35.7148, lon=139.7967, aliases=("淺草觀音寺",))
        gate = world.place("雷門", lat=35.7111, lon=139.7964)
        tower = world.place("晴空塔", lat=35.7101, lon=139.8107)
        a = world.article(1)
    return P


# ---------- 點節點 ----------

class TestPoint:
    def test_spot_does_not_take_its_area(self, asakusa, world, conn, env):
        mine = world.note(asakusa.a, asakusa.temple, f"人很多{R}")
        world.note(asakusa.a, asakusa.area, f"淺草整區人很多{R}")
        r = go(point(at(conn, asakusa.temple)), env)
        assert r.status == HAS and ids(r) == [mine]

    def test_area_takes_everything_in_range(self, asakusa, world, conn, env):
        a = world.note(asakusa.a, asakusa.area, f"淺草整區人很多{R}")
        b = world.note(asakusa.a, asakusa.temple, f"人很多{R}")
        c = world.note(asakusa.a, asakusa.gate, f"拍照要排隊{R}")
        world.note(asakusa.a, asakusa.tower, f"晴空塔人很多{R}")
        r = go(point(at(conn, asakusa.area)), env)
        assert sorted(ids(r)) == sorted([a, b, c])
        assert {name for name, _ in r.groups} == {"淺草", "淺草寺", "雷門"}

    def test_around_takes_only_within_radius(self, asakusa, world, conn, env):
        a = world.note(asakusa.a, asakusa.temple, f"人很多{R}")
        b = world.note(asakusa.a, asakusa.gate, f"人很多{R}")
        world.note(asakusa.a, asakusa.tower, f"人很多{R}")
        r = go(point(Scope(AROUND, where(conn, asakusa.temple), 1.0)), env)
        assert sorted(ids(r)) == sorted([a, b])

    def test_anywhere_sends_only_embedding_top_to_judge(self, asakusa, world, conn, env, jev):
        near = []
        for i in range(60):
            text = f"第 {i} 筆{R}" + (NEAR if i >= 50 else "")
            note = world.note(asakusa.a, asakusa.temple, text)
            if i >= 50:
                near.append(note)
        r = go(point(Scope(ANYWHERE), aspect=f"人潮{NEAR}"), env)
        assert r.embedded == config.EMBED_TOP and r.judged == config.EMBED_TOP and len(jev.passages) == 50
        assert set(near) <= set(ids(r))
        assert r.time_matched == 60

    def test_anywhere_under_limit_skips_embedding(self, asakusa, world, env, embedder):
        world.note(asakusa.a, asakusa.temple, f"人很多{R}")
        r = go(point(Scope(ANYWHERE)), env)
        assert r.embedded is None and embedder.texts == []

    def test_unresolved_note_matched_by_zh_name(self, asakusa, world, conn, env):
        by_alias = world.note(asakusa.a, "淺草觀音寺", f"人很多{R}")
        world.note(asakusa.a, "浅草寺", f"日文名稱不比{R}")
        r = go(point(at(conn, asakusa.temple)), env)
        assert ids(r) == [by_alias]
        assert FLAG_BY_NAME in r.evidence[0].flags

    def test_failed_relevance_is_not_evidence(self, asakusa, world, conn, env):
        ok = world.note(asakusa.a, asakusa.temple, f"人很多{R}")
        world.note(asakusa.a, asakusa.temple, "附近有賣仙貝")
        r = go(point(at(conn, asakusa.temple)), env)
        assert ids(r) == [ok] and r.judged == 2 and r.passed == 1

    def test_unknown_time_flag(self, asakusa, world, conn, env):
        world.note(asakusa.a, asakusa.temple, f"人很多{R}", times=[(None, "10", None, None)])
        r = go(point(at(conn, asakusa.temple), TimeCond(months=(10,), hours=((9, 11),))), env)
        assert r.evidence[0].flags == [f"{FLAG_UNKNOWN}（時段）"]

    def test_other_counts_per_dimension(self, asakusa, world, conn, env):
        world.note(asakusa.a, asakusa.temple, "人很多", times=[(2024, "11", "假日", "15")])
        world.note(asakusa.a, asakusa.temple, "人很多", times=[(None, "11", "平日", "9-10")])
        world.note(asakusa.a, asakusa.temple, "人很多", times=[(None, "3", None, None)])
        r = go(point(at(conn, asakusa.temple), TimeCond(months=(10,))), env)
        c = r.other_counts
        assert r.status == NONE and c["時間相符"] == 0 and c["全部"] == 3
        assert c["年份"] == {"2024": 1, "不明": 2}
        assert c["月份"]["11"] == 2 and c["月份"]["3"] == 1 and c["月份"]["10"] == 0
        assert c["鐘點"]["15"] == 1 and c["鐘點"]["9"] == 1 and c["鐘點"]["10"] == 1 and c["鐘點"]["不明"] == 1
        assert c["平日／假日"] == {"平日": 1, "假日": 1, "不明": 1}

    def test_time_matched_but_all_failed_relevance(self, asakusa, world, conn, env):
        world.note(asakusa.a, asakusa.temple, "附近有賣仙貝", times=[(None, "10", None, None)])
        r = go(point(at(conn, asakusa.temple), TimeCond(months=(10,))), env)
        assert r.status == NONE and r.time_matched == 1 and r.passed == 0
        assert r.other_counts["時間相符"] == 1

    def test_no_time_match_at_all(self, asakusa, world, conn, env):
        world.note(asakusa.a, asakusa.temple, f"人很多{R}", times=[(None, "11", None, None)])
        r = go(point(at(conn, asakusa.temple), TimeCond(months=(10,))), env)
        assert r.status == NONE and r.time_matched == 0 and r.other_counts["時間相符"] == 0


# ---------- 段節點 ----------

class TestMove:
    def test_both_ways_takes_reverse_and_flags_it(self, asakusa, world, conn, env):
        fwd = world.move(asakusa.a, asakusa.temple, asakusa.tower, f"走路 20 分鐘{R}")
        back = world.move(asakusa.a, asakusa.tower, asakusa.temple, f"搭公車 10 分鐘{R}")
        r = go(move(at(conn, asakusa.temple), at(conn, asakusa.tower), both_ways=True), env)
        assert ids(r) == [fwd, back]
        assert FLAG_REVERSED in r.evidence[1].flags and FLAG_REVERSED not in r.evidence[0].flags

    def test_one_way_only_counts_reverse(self, asakusa, world, conn, env):
        world.move(asakusa.a, asakusa.tower, asakusa.temple, f"搭公車 10 分鐘{R}")
        r = go(move(at(conn, asakusa.temple), at(conn, asakusa.tower)), env)
        assert r.status == NONE and r.candidates == 0 and r.other_counts["反方向"] == 1

    def test_area_end_contains_places_in_range(self, asakusa, world, conn, env):
        m = world.move(asakusa.a, asakusa.gate, asakusa.tower, f"走路 25 分鐘{R}")
        r = go(move(at(conn, asakusa.area), at(conn, asakusa.tower)), env)
        assert ids(r) == [m]

    def test_anywhere_end_groups_by_that_end(self, asakusa, world, conn, env):
        a = world.move(asakusa.a, asakusa.temple, asakusa.tower, f"走路 20 分鐘{R}")
        b = world.move(asakusa.a, asakusa.temple, asakusa.gate, f"走路 5 分鐘{R}")
        c = world.move(asakusa.a, asakusa.temple, "上野", f"搭電車{R}")
        world.move(asakusa.a, asakusa.gate, asakusa.tower, f"不是從淺草寺出發{R}")
        r = go(move(at(conn, asakusa.temple), Scope(ANYWHERE)), env)
        assert sorted(ids(r)) == sorted([a, b, c])
        assert {name for name, _ in r.groups} == {"晴空塔", "雷門", "上野"}


# ---------- 天節點 ----------

@pytest.fixture
def ueno(world):
    class P:
        area = world.area("上野", 35.712, 139.777, 0.008)
        park = world.place("上野公園", lat=35.715, lon=139.773)
    return P


class TestArrangedDay:
    def test_same_stop_rules(self, asakusa, ueno, world, conn, env):
        a = asakusa.a
        by_id = world.day(a, 1, [asakusa.temple], month="10")
        in_area = world.day(a, 2, [ueno.park], month="10")
        by_name = world.day(a, 3, ["淺草觀音寺"], month="10")
        world.day(a, 4, [asakusa.gate], month="10")   # 離淺草寺很近，但不是同一個地點
        r = go(day(conn, [asakusa.temple, ueno.area]), env)
        assert {d.id for d in r.days} == {by_id, in_area, by_name}
        assert r.candidates == 3 and r.same_days == 3

    def test_ranking_and_top(self, asakusa, ueno, world, conn, env, monkeypatch):
        monkeypatch.setattr(config, "DAY_TOP", 3)
        old = world.article(2, posted_at="2024-05-01T10:00:00")
        new = world.article(3, posted_at="2025-12-01T10:00:00")
        both_old = world.day(old, 1, [asakusa.temple, ueno.park])
        one_new_2 = world.day(new, 2, [asakusa.temple])
        one_new_1 = world.day(new, 1, [ueno.park])
        world.day(old, 2, [asakusa.temple])
        r = go(day(conn, [asakusa.temple, ueno.area]), env)
        assert [d.id for d in r.days] == [both_old, one_new_1, one_new_2]
        assert r.same_days == 4
        assert r.days[0].same == ["淺草寺", "上野"]

    def test_other_days_of_same_article(self, asakusa, world, conn, env):
        d1 = world.day(asakusa.a, 1, [asakusa.temple])
        d2 = world.day(asakusa.a, 2, [asakusa.temple, asakusa.tower])
        r = go(day(conn, [asakusa.temple]), env)
        assert {d.id for d in r.days} == {d1, d2}
        assert r.days[0].days_in_article == 2

    def test_unresolved_stop_not_compared_and_place_counted_once(self, asakusa, world, conn, env):
        d = world.day(asakusa.a, 1, [asakusa.temple])
        r = go(day(conn, [asakusa.temple, "沒解析到的店", asakusa.temple]), env)
        assert [x.id for x in r.days] == [d] and r.days[0].same == ["淺草寺"]

    def test_day_notes_of_picked_days_are_evidence(self, asakusa, world, conn, env):
        d1 = world.day(asakusa.a, 1, [asakusa.temple, (asakusa.tower, STAY)], month="10")
        world.day(asakusa.a, 2, [asakusa.gate])
        n1 = world.day_note(asakusa.a, "這天很順", days=[d1])
        trip = world.day_note(asakusa.a, "整趟太趕")
        r = go(day(conn, [asakusa.temple]), env)
        assert r.status == HAS
        assert [(e.table, e.id) for e in r.evidence] == [(DAY, d1), (DAY_NOTE, n1), (DAY_NOTE, trip)]
        assert r.days[0].stops[1]["type"] == STAY

    def test_day_without_notes(self, asakusa, world, conn, env):
        world.day(asakusa.a, 1, [asakusa.temple])
        r = go(day(conn, [asakusa.temple]), env)
        assert r.status == HAS_NO_NOTE

    def test_month_uses_day_month(self, asakusa, world, conn, env):
        world.day(asakusa.a, 1, [asakusa.temple], month="9")
        d2 = world.day(asakusa.a, 2, [asakusa.temple], month="10")
        r = go(day(conn, [asakusa.temple], TimeCond(months=(10,))), env)
        assert [d.id for d in r.days] == [d2] and r.candidates == 2

    def test_no_days_counts_each_stop(self, asakusa, ueno, world, conn, env):
        world.day(asakusa.a, 1, [asakusa.temple, ueno.park], month="9")
        world.day(asakusa.a, 2, [asakusa.temple], month="11")
        r = go(day(conn, [asakusa.temple, ueno.area, "沒解析到的店"], TimeCond(months=(10,))), env)
        c = r.other_counts
        assert r.status == NONE and r.days == []
        assert c["至少一站相同的天數（不看時間）"] == 2
        assert c["月份"] == {"9": 1, "11": 1}
        assert c["每一站單獨去過的天數"] == {"淺草寺": 2, "上野": 1, "沒解析到的店": "解析不到，沒比"}


class TestFreeDay:
    def test_relevant_day_notes_bring_out_days(self, asakusa, world, conn, env):
        d1 = world.day(asakusa.a, 1, [asakusa.temple], month="10")
        d2 = world.day(asakusa.a, 2, [asakusa.tower], month="10")
        n1 = world.day_note(asakusa.a, f"這天走太多路{R}", days=[d1])
        world.day_note(asakusa.a, "這天很普通", days=[d2])
        trip = world.day_note(asakusa.a, "整趟還好")
        r = go(day(conn, []), env)
        assert r.status == HAS and ids(r) == [n1]
        assert [d.id for d in r.days] == [d1]
        assert r.days[0].trip_notes == [{"id": trip, "content": "整趟還好"}]

    def test_trip_note_uses_months_of_all_days(self, asakusa, world, conn, env):
        world.day(asakusa.a, 1, [asakusa.temple], month="9")
        world.day(asakusa.a, 2, [asakusa.tower], month="10")
        b = world.article(2)
        d = world.day(b, 1, [asakusa.temple], month="9")
        trip = world.day_note(asakusa.a, f"整趟太趕{R}")
        world.day_note(b, f"這天太趕{R}", days=[d])
        r = go(day(conn, [], TimeCond(months=(10,))), env)
        assert ids(r) == [trip] and len(r.days) == 2

    def test_nothing_relevant(self, asakusa, world, conn, env):
        d = world.day(asakusa.a, 1, [asakusa.temple])
        world.day_note(asakusa.a, "普通", days=[d])
        r = go(day(conn, []), env)
        assert r.status == NONE and r.other_counts == {"時間相符": 1}


# ---------- 排法的計算 ----------

def _result(node, evidence, numbers):
    return node, NodeResult(HAS, evidence=evidence, summary=Summary(numbers=numbers))


def _ev(table, item_id, places):
    return Evidence(table, item_id, "M.1.A.AAA", "內容", "地點", places=places)


class TestCalc:
    def test_numbers_total_and_missing(self, asakusa, ueno, conn):
        node = day(conn, [asakusa.temple, asakusa.tower, ueno.area])
        stay = _ev("place_note", "1-1", (asakusa.temple,))
        mv = _ev("move", "1-2", (asakusa.temple, asakusa.tower))
        results = [
            _result(point(at(conn, asakusa.temple), no="N2"), [stay],
                    [fakes.number("停留時間", 30, 60, "place_note 1-1"), fakes.number("排隊時間", 10, 10, "place_note 1-1")]),
            _result(move(at(conn, asakusa.temple), at(conn, asakusa.tower), no="N3"), [mv],
                    [fakes.number("移動時間", 20, 25, "move 1-2")]),
        ]
        c = nodes.calc(node, results, conn)
        assert c["站數"] == 3
        assert c["每段"][0]["直線距離"] == "1.4 公里"
        assert c["每段"][1]["直線距離"] == "區域，不算距離"
        assert c["直線距離加總"] == "1.4 公里"
        assert c["每站"][0]["停留時間"][0]["最少"] == 30
        assert c["每站"][0]["其他數字"][0]["種類"] == "排隊時間"
        assert c["每段"][0]["移動時間"][0]["出處"] == "move 1-2"
        assert (c["合計最少"], c["合計最多"]) == (50, 85)
        assert "部分加總" in c["合計"]
        assert c["缺"] == ["停留時間：晴空塔", "停留時間：上野", "移動時間：晴空塔 → 上野"]

    def test_needs_two_resolved_stops(self, asakusa, conn):
        assert nodes.calc(day(conn, [asakusa.temple, "沒解析到的店"]), [], conn) is None

    def test_consecutive_same_stop_is_not_a_segment(self, asakusa, conn):
        c = nodes.calc(day(conn, [asakusa.temple, asakusa.temple, asakusa.tower]), [], conn)
        assert [s["段"] for s in c["每段"]] == ["淺草寺 → 晴空塔"]

    def test_recalculated_each_round_even_without_days(self, asakusa, world, conn, env, fake_llm):
        """挑不到天也算；下一輪加了點節點，跑完那一輪用新的數字重算。"""
        note = world.note(asakusa.a, asakusa.temple, f"待了一小時{R}")
        fake_llm.sources["summary"] = fakes.summary(numbers=[fakes.number("停留時間", 60, 60, f"place_note {note}")])
        state = QState()
        d = day(conn, [asakusa.temple, asakusa.tower], no="N1")
        p = point(at(conn, asakusa.temple), aspect="停留時間", no="N2")

        async def rounds():
            state.nodes.append(d)
            await run.run_round([d], env, state)
            first = dict(state.results["N1"].calc)
            state.nodes.append(p)
            await run.run_round([p], env, state)
            return first

        first = asyncio.run(rounds())
        assert state.results["N1"].days == []
        assert "停留時間：淺草寺" in first["缺"]
        assert "停留時間：淺草寺" not in state.results["N1"].calc["缺"]
        assert state.results["N1"].calc["合計最少"] == 60
