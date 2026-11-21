"""④ 驗證計畫：格式不合法時重問、還不合法的拿掉；地點解析；去重複；數量上限；改了什麼。"""
import asyncio

import pytest

from tests.v4.rag import fakes
from tests.v4.rag.fakes import ANY, FakeGeocoder, around, at, day, move, new, nodes, on, point, ref, stop
from v4.extract.data_schema import Article, PlaceRef
from v4.rag import plan, validate
from v4.rag.data_schema import (DROP_DUPLICATE, DROP_FORMAT, DROP_OVER, DROP_UNRESOLVED, Question, UserDay,
                                UserStop)
from v4.rag.validate import Ctx, Resolver

ARTICLE = Article(id="M.1.A.AAA", title="東京行程", author="", posted_at="2025-10-01T10:00:00",
                  text="D1 淺草寺 → 晴空塔", comments=[])
QUESTION = Question(1, "淺草寺人多嗎", "點")


def place_ref(r, zh, ja, kind="景點"):
    return PlaceRef(r, zh, ja, "東京", kind, "是", "", f"去{zh}")


@pytest.fixture
def ids(world):
    return {"浅草寺": world.place("淺草寺", "浅草寺"), "スカイツリー": world.place("晴空塔", "スカイツリー"),
            "上野": world.area("上野", 35.71, 139.77, 0.01), "雷門": world.place("雷門", "雷門")}


@pytest.fixture
def geocoder(ids):
    return FakeGeocoder(ids)


@pytest.fixture
def ctx(ids, geocoder):
    """地點列表 p1 淺草寺、p2 晴空塔、p3 謎之店（解析不到）；行程 D1 p1→p2、D2 p3、D3 p1→p3。
    行程裡的站 ① 已經解析過，p4 上野不在行程裡、還沒解析。"""
    places = {p.ref: p for p in [place_ref("p1", "淺草寺", "浅草寺"), place_ref("p2", "晴空塔", "スカイツリー"),
                                 place_ref("p3", "謎之店", "なぞの店"), place_ref("p4", "上野", "上野", "區域")]}
    days = [UserDay("D1", "10", (UserStop("p1", "景點"), UserStop("p2", "景點"))),
            UserDay("D2", "10", (UserStop("p3", "景點"),)),
            UserDay("D3", "10", (UserStop("p1", "景點"), UserStop("p3", "景點")))]
    resolved = {"p1": ids["浅草寺"], "p2": ids["スカイツリー"], "p3": None}
    return Ctx(places, days, 2025, QUESTION.text, Resolver(geocoder, ARTICLE.id, places, resolved))


def accept(raws, ctx, existing=(), replan=False, round_no=0):
    return asyncio.run(validate.accept(list(enumerate(raws)), ctx, list(existing), replan=replan,
                                       round_no=round_no))


def initial(ctx):
    return asyncio.run(plan.initial(ARTICLE, QUESTION, ctx))


GOOD = point(at(ref("p1")))

BAD = {
    "種類": {**GOOD, "kind": "線"},
    "對象的寫法": point({"mode": "附近", "place": ref("p1"), "km": 0}),
    "地點編號": point(at(ref("p9"))),
    "新地點沒有日文名稱": point(at(new("某店", ""))),
    "新地點的種類": point(at(new("某店", "某店", kind="車站"))),
    "周圍的半徑": point(around(ref("p1"), 0)),
    "天的編號": day([on("D9")]),
    "排法一站寫兩個": day([{"day": "D1", "place": ref("p1")}]),
    "排法的站是空的": day([{"day": "", "place": fakes.EMPTY_PLACE}]),
    "年份不是四位數": point(at(ref("p1")), year="25"),
    "年份晚於發文": point(at(ref("p1")), year="2026"),
    "月份": point(at(ref("p1")), months=(13,)),
    "平日假日": point(at(ref("p1")), day_type="週末"),
    "時段超出 0–23": point(at(ref("p1")), hours=((22, 24),)),
    "面向沒填": point(at(ref("p1")), aspect=" "),
    "段兩端都不限": move(ANY, ANY),
    "段沒填反方向也要": move(at(ref("p1")), at(ref("p2")), both_ways=""),
    "天節點填時段": day([on("D1")], hours=((9, 12),)),
    "③ 填放寬": point(at(ref("p1")), relax_from="N1"),
}


class TestFormat:
    def test_good_nodes_pass(self, ctx):
        good = [GOOD, point(around(new("築地市場", "築地場外市場"), 1)), move(at(ref("p1")), ANY),
                day([on("D1"), stop(ref("p4"))], months=(10,), year="2024"), day([]),
                point(at(ref("p1")), hours=((18, 2),), day_type="假日")]
        assert validate.check_format(good, ctx) == []

    @pytest.mark.parametrize("name", list(BAD))
    def test_bad_node(self, ctx, name):
        problems = validate.check_format([GOOD, BAD[name]], ctx)
        assert problems and {p.index for p in problems} == {1}

    def test_relax_from_must_be_existing_node_without_evidence(self, ctx):
        raws = [point(at(ref("p1")), relax_from=r) for r in ("N1", "N2", "N9")]
        problems = validate.check_format(raws, ctx, known={"N1", "N2"}, relaxable={"N1"})
        assert {p.index for p in problems} == {1, 2}

    def test_reask_once_then_accept(self, ctx, use_llm):
        llm = use_llm(plan=[nodes(BAD["月份"]), nodes(GOOD)])
        batch = initial(ctx)
        assert [n.no for n in batch.accepted] == ["N1"] and batch.dropped == []
        first, second = llm.users("plan")
        assert "<problems>" not in first and "months 的 13 不是 1–12" in second

    def test_still_bad_after_reask_is_dropped_with_reason(self, ctx, use_llm):
        llm = use_llm(plan=[nodes(BAD["月份"], GOOD), nodes(BAD["月份"], GOOD)])
        batch = initial(ctx)
        assert len(llm.users("plan")) == 2
        assert len(batch.accepted) == 1
        [d] = batch.dropped
        assert d.why == DROP_FORMAT and d.index == 0 and "13" in d.reason


class TestPlaces:
    def test_unresolved_scope_place_dropped_without_reask(self, ctx, use_llm):
        llm = use_llm(plan=nodes(point(at(new("謎之店", "なぞの店"))), GOOD))
        batch = initial(ctx)
        assert len(llm.users("plan")) == 1
        [d] = batch.dropped
        assert d.why == DROP_UNRESOLVED and "謎之店" in d.reason
        assert [n.target.where.place_id for n in batch.accepted] == [ctx.resolver.cache[("ref", "p1")]]

    def test_arrangement_kept_if_any_stop_resolves(self, ctx):
        batch = accept([day([on("D3")]), day([on("D2")])], ctx)
        [kept] = batch.accepted
        assert [s.resolved for s in kept.stops] == [True, False]
        assert [s.day for s in kept.stops] == ["D3", "D3"]
        [d] = batch.dropped
        assert d.why == DROP_UNRESOLVED

    def test_resolved_places_not_resolved_again(self, ctx, geocoder, ids):
        batch = accept([point(at(ref("p1"))), day([on("D1")]), point(at(ref("p4"))),
                        point(at(ref("p4")), aspect="景色"), point(at(new("雷門", "雷門"))),
                        move(at(new("雷門", "雷門")), at(ref("p2")))], ctx)
        assert len(batch.accepted) == 6
        assert geocoder.asked == ["上野", "雷門"]
        assert batch.accepted[2].target.where.place_id == ids["上野"]


class TestDuplicates:
    def test_same_conditions_dropped(self, ctx):
        batch = accept([GOOD, point(at(ref("p1")), aspect=" 人潮 "), point(at(ref("p1")), months=(10,))], ctx)
        assert [n.no for n in batch.accepted] == ["N1", "N2"]
        [d] = batch.dropped
        assert d.why == DROP_DUPLICATE and d.index == 1 and "N1" in d.reason

    def test_both_ways_ignores_direction(self, ctx):
        batch = accept([move(at(ref("p1")), at(ref("p2")), "是"), move(at(ref("p2")), at(ref("p1")), "是"),
                        move(at(ref("p1")), at(ref("p2"))), move(at(ref("p2")), at(ref("p1")))], ctx)
        assert len(batch.accepted) == 3 and [d.index for d in batch.dropped] == [1]

    def test_arrangement_compared_after_expanding_days(self, ctx):
        batch = accept([day([on("D1")]), day([stop(ref("p1")), stop(ref("p2"))])], ctx)
        assert len(batch.accepted) == 1 and batch.dropped[0].why == DROP_DUPLICATE

    def test_no_data_condition_cannot_be_queried_again(self, ctx):
        first = accept([GOOD], ctx).accepted
        batch = accept([point(at(ref("p1")), relax_from="N1")], ctx, first, replan=True, round_no=1)
        assert batch.accepted == [] and batch.dropped[0].why == DROP_DUPLICATE


class TestLimits:
    def test_replan_keeps_first_five(self, ctx):
        raws = [point(at(ref("p1")), aspect=f"面向 {i}") for i in range(7)]
        batch = accept(raws, ctx, accept([GOOD], ctx).accepted, replan=True, round_no=1)
        assert [n.no for n in batch.accepted] == ["N2", "N3", "N4", "N5", "N6"]
        assert [(d.index, d.why) for d in batch.dropped] == [(5, DROP_OVER), (6, DROP_OVER)]

    def test_initial_over_forty_dropped_from_the_back(self, ctx):
        raws = [point(at(ref("p1")), aspect=f"面向 {i}") for i in range(42)]
        batch = accept(raws, ctx)
        assert len(batch.accepted) == 40
        assert [(d.index, d.why) for d in batch.dropped] == [(40, DROP_OVER), (41, DROP_OVER)]
        assert "40" in batch.dropped[0].reason

    def test_question_never_over_forty(self, ctx):
        existing = accept([point(at(ref("p1")), aspect=f"面向 {i}") for i in range(38)], ctx).accepted
        batch = accept([point(at(ref("p2")), aspect=f"面向 {i}") for i in range(5)], ctx, existing, replan=True)
        assert len(existing) + len(batch.accepted) == 40
        assert len(batch.dropped) == 3


class TestChanges:
    def test_changes_computed_and_not_blocked(self, ctx):
        first = accept([point(at(ref("p1")), months=(10,))], ctx).accepted
        batch = accept([point(at(ref("p1")), months=(10, 11), relax_from="N1"),
                        point(at(ref("p4")), months=(10,), relax_from="N1"),
                        point(at(ref("p1")), months=(10,), hours=((9, 11),), relax_from="N1")],
                       ctx, first, replan=True, round_no=1)
        assert [n.changes for n in batch.accepted] == [
            ("月份 10 → 10、11",), ("地點 淺草寺 → 上野",), ("時段 不限 → 9–11",)]
        assert all(n.round == 1 for n in batch.accepted)
