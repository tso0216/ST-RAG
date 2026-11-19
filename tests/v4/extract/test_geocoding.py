"""地點解析：一個地點從「找得到嗎」到有結果，或本機地圖三輪都沒有、上網查地址也沒有。

本機地圖的查詢記成（名稱用「、」接起來, 範圍, 排序用的城市）。
"""
import asyncio

import pytest

from tests.v4.extract.fakes import FakeGsi, KING_ASAKUSA, SENSOJI, SKYTREE, plan, web
from v4 import place as place_table
from v4.extract import config
from v4.extract import geocode_log as glog
from v4.extract.data_schema import Context, Day, Nearby, Parsed, PlaceNote, PlaceRef
from v4.extract.geocoding import Geocoder

ARTICLE = "M.1.A.AAA"


def ref(name_zh, name_ja, city="東京", located="是", ref="p1", kind="景點", reason="") -> PlaceRef:
    return PlaceRef(ref, name_zh, name_ja, city, kind, located, reason, sentence=f"去{name_zh}")


class Spy:
    """記下本機地圖的每一次查詢，其他照原本的做。"""

    def __init__(self, local_map):
        self.map = local_map
        self.searches: list[tuple[str, str]] = []

    def search(self, names, area=None):
        names = [names] if isinstance(names, str) else names
        self.searches.append(("、".join(names), area.label() if area else "全日本"))
        return self.map.search(names, area)

    def admin_area(self, pref="", municipality=""):
        return self.map.admin_area(pref, municipality)


@pytest.fixture
def setup(conn, local_map, use_llm):
    """回傳函式：建一個 Geocoder，連同記錄查詢的本機地圖、假的國土地理院、假的 llm 一起回傳。"""
    def build(gsi=None, **llm):
        spy, gsi = Spy(local_map), gsi or FakeGsi()
        return Geocoder(conn, spy, gsi=gsi), spy, use_llm(**llm), gsi
    return build


def resolve(geocoder, p, **ctx):
    return asyncio.run(geocoder.resolve(p, Context(ARTICLE, **ctx)))


def dropped(geocoder, p) -> str:
    """解析一個地點，確定被捨棄，回傳 log 記的原因。"""
    traces: list[glog.Trace] = []
    r = asyncio.run(geocoder.resolve(p, Context(ARTICLE), on_done=traces.append))
    [trace] = traces
    assert r.place_id is None and trace.result == glog.DROPPED
    return trace.fail_reason


def aliases(conn, table) -> list[tuple]:
    return conn.execute(f"SELECT id, alias FROM {table} ORDER BY id, alias").fetchall()


def source(conn, place_id) -> tuple[str, str]:
    return place_table.source_of(conn, place_id)


# ---------- 資料庫 ----------

def test_place_already_in_database_is_used_without_searching(conn, setup):
    pid = place_table.add(conn, source="osm", source_id="way/1", kind="景點", name_ja="浅草寺", name_zh="淺草寺",
                          city="東京", lat=35.7148, lon=139.7967)
    geocoder, spy, llm, gsi = setup()

    r = resolve(geocoder, ref("淺草寺", "あさくさでら"))

    assert r.place_id == pid
    assert spy.searches == [] and llm.calls == []


def test_several_database_matches_fall_through_to_the_map(conn, setup):
    place_table.add(conn, source="osm", source_id="way/21", kind="景點", name_ja="清水寺", name_zh="清水寺",
                    city="京都", lat=34.9949, lon=135.7850)
    other = place_table.add(conn, source="osm", source_id="way/99", kind="景點", name_ja="音羽山清水寺",
                            name_zh="音羽山清水寺", city="京都", lat=34.9, lon=135.7)
    place_table.add_alias(conn, other, name_zh="清水寺")
    geocoder, spy, llm, gsi = setup(pick=1)

    r = resolve(geocoder, ref("清水寺", "清水寺", city="京都"))

    assert spy.searches == [("清水寺", "京都府")]
    assert source(conn, r.place_id) == ("osm", "way/21")


def test_same_name_in_another_city_is_not_a_database_match(conn, setup):
    place_table.add(conn, source="osm", source_id="way/21", kind="景點", name_ja="清水寺", name_zh="清水寺",
                    city="京都", lat=34.9949, lon=135.7850)
    geocoder, spy, llm, gsi = setup(pick=1)

    r = resolve(geocoder, ref("清水寺", "清水寺", city="東京"))

    assert spy.searches == [("清水寺", "東京都")]
    assert source(conn, r.place_id) == ("osm", "way/22")


# ---------- 搜尋計畫 ----------

def test_plan_is_asked_with_the_place_and_numbered_same_day_places(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1)

    resolve(geocoder, ref("淺草寺", "浅草寺"), same_day=[Nearby("晴空塔", SKYTREE.lat, SKYTREE.lon), Nearby("還沒解析")])

    [user] = llm.users("plan")
    assert "<place>淺草寺｜浅草寺｜東京｜景點</place>" in user and "<sentence>去淺草寺</sentence>" in user
    assert "1. 晴空塔（35.7101, 139.8107）" in user and "2. 還沒解析（還沒定位，不能當中心）" in user
    assert "<history>" not in user and "<error>" not in user
    assert llm.tools["plan"] == []


def test_several_names_and_a_municipality(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1, plan=[plan("せんそうじ", "浅草寺", pref="東京", municipality="台東区")])

    r = resolve(geocoder, ref("淺草寺", "せんそうじ"))

    assert spy.searches == [("せんそうじ、浅草寺", "東京都 台東区")]
    assert source(conn, r.place_id) == ("osm", SENSOJI.source_id)
    assert "<area>東京都 台東区</area>" in llm.users("pick")[0]


def test_area_the_map_does_not_know_is_sent_back_to_the_llm(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1, plan=[plan("浅草寺", pref="大坂"), plan("浅草寺", pref="東京")])

    r = resolve(geocoder, ref("淺草寺", "浅草寺"))

    first, second = llm.users("plan")
    assert "<error>" not in first and "<error>沒有「大坂」這個都道府縣" in second
    assert spy.searches == [("浅草寺", "東京都")]
    assert source(conn, r.place_id) == ("osm", SENSOJI.source_id)


def test_area_still_unusable_after_retries_skips_the_round(conn, setup, monkeypatch):
    monkeypatch.setattr(config, "RETRIES", 1)
    geocoder, spy, llm, gsi = setup(pick=1, plan=[plan("浅草寺", pref="大坂"), plan("浅草寺", pref="大坂"),
                                                  plan("浅草寺", pref="東京")])

    r = resolve(geocoder, ref("淺草寺", "浅草寺"))

    assert spy.searches == [("浅草寺", "東京都")]
    assert "第 1 輪：名稱 浅草寺｜範圍 {'pref': '大坂'}｜範圍不能用：沒有「大坂」" in llm.users("plan")[2]
    assert source(conn, r.place_id) == ("osm", SENSOJI.source_id)


def test_circle_around_a_same_day_place(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1, plan=[plan("焼肉きんぐ", near=1, km=1)])
    same_day = [Nearby("淺草寺", SENSOJI.lat, SENSOJI.lon)]

    r = resolve(geocoder, ref("燒肉 KING", "焼肉きんぐ", located="猜的"), same_day=same_day)

    # 新宿店在 9 公里外，不在範圍內
    assert spy.searches == [("焼肉きんぐ", "「淺草寺」方圓 1 公里")]
    assert source(conn, r.place_id) == ("overture", KING_ASAKUSA.source_id)
    [user] = llm.users("pick")
    assert "<area>「淺草寺」方圓 1 公里</area>" in user and "離中心「淺草寺」0.4 公里" in user


@pytest.mark.parametrize("given, used", [(0, 0.3), (0.1, 0.3), (2, 2), (50, 10)])
def test_circle_radius_is_kept_within_the_setting(conn, setup, given, used):
    geocoder, spy, llm, gsi = setup(pick=0, plan=[plan("どこか", near=1, km=given)] * 3)

    resolve(geocoder, ref("某處", "どこか"), same_day=[Nearby("淺草寺", SENSOJI.lat, SENSOJI.lon)])

    assert spy.searches[0][1] == f"「淺草寺」方圓 {used:g} 公里"


@pytest.mark.parametrize("near, message", [
    (3, "near 要是 <same_day> 裡的編號（1～2）"),
    (2, "「還沒解析」還沒定位，不能當中心"),
])
def test_circle_needs_a_same_day_place_that_is_already_located(conn, setup, near, message):
    geocoder, spy, llm, gsi = setup(pick=1, plan=[plan("浅草寺", near=near, km=1), plan("浅草寺", pref="東京")])

    resolve(geocoder, ref("淺草寺", "浅草寺"), same_day=[Nearby("晴空塔", SKYTREE.lat, SKYTREE.lon), Nearby("還沒解析")])

    assert message in llm.users("plan")[1]
    assert spy.searches == [("浅草寺", "東京都")]


def test_plan_without_names_is_sent_back(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1, plan=[plan(" ", pref="東京"), plan("浅草寺", pref="東京")])

    resolve(geocoder, ref("淺草寺", "浅草寺"))

    assert "names 至少要給一個日文名稱" in llm.users("plan")[1]


# ---------- 自動放寬 ----------

def test_municipality_without_the_same_name_widens_to_the_prefecture(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1, plan=[plan("浅草寺", pref="東京", municipality="墨田区")])

    r = resolve(geocoder, ref("淺草寺", "浅草寺"))

    assert spy.searches == [("浅草寺", "東京都 墨田区"), ("浅草寺", "東京都")]
    assert source(conn, r.place_id) == ("osm", SENSOJI.source_id)
    assert "<area>東京都（原本是 東京都 墨田区，裡面沒有同名的，自動放寬）</area>" in llm.users("pick")[0]


def test_circle_without_the_same_name_widens_its_radius(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1, plan=[plan("焼肉きんぐ", near=1, km=0.3)])

    resolve(geocoder, ref("燒肉 KING", "焼肉きんぐ", located="猜的"), same_day=[Nearby("淺草寺", SENSOJI.lat, SENSOJI.lon)])

    assert [area for _, area in spy.searches] == ["「淺草寺」方圓 0.3 公里", "「淺草寺」方圓 0.9 公里"]


def test_prefecture_is_not_widened(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1)

    resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert spy.searches == [("築地場外市場", "東京都")]


def test_widened_area_without_the_same_name_keeps_the_original_results(conn, setup):
    # 墨田区裡「浅草寺院」只有模糊的（東京ソラマチ不像），放寬到東京都也只有模糊的：用原本範圍的結果
    geocoder, spy, llm, gsi = setup(pick=0, plan=[plan("浅草寺院", pref="東京", municipality="台東区")] * 3)

    resolve(geocoder, ref("淺草寺院", "浅草寺院"))

    assert spy.searches[:2] == [("浅草寺院", "東京都 台東区"), ("浅草寺院", "東京都")]
    assert "<area>東京都 台東区</area>" in llm.users("pick")[0]


# ---------- 本機地圖和 llm ----------

def test_found_in_round_one_is_added_with_its_source_and_aliases(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1)

    r = resolve(geocoder, ref("晴空塔", "スカイツリー"), same_day=[])

    p = place_table.get(conn, r.place_id)
    assert (p.kind, p.name_ja, p.name_zh, p.city) == ("景點", "東京スカイツリー", "晴空塔", "東京")
    assert (p.lat, p.lon) == (SKYTREE.lat, SKYTREE.lon)
    assert source(conn, p.id) == ("osm", "way/2")
    assert aliases(conn, "place_alias_zh") == [(p.id, "晴空塔")]
    assert aliases(conn, "place_alias_ja") == [(p.id, "スカイツリー")]
    [user] = llm.users("pick")
    assert "晴空塔｜スカイツリー｜東京｜景點" in user and "去晴空塔" in user
    assert "1. 東京スカイツリー｜OSM｜tourism=attraction｜東京都 墨田区｜35.7101, 139.8107｜300 公尺內其他候選 0 筆" in user
    assert gsi.queries == [] and llm.users("web") == []


def test_candidates_show_coordinates_distances_and_how_many_others_are_nearby(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=2)

    r = resolve(geocoder, ref("淺草寺", "浅草寺"), same_day=[Nearby("晴空塔", SKYTREE.lat, SKYTREE.lon)])

    user = llm.users("pick")[0]
    assert ("1. 浅草寺｜OSM｜amenity=place_of_worship｜東京都 台東区 浅草2-3-1｜35.7148, 139.7967｜"
            "離同一天的「晴空塔」約 1.4 公里｜300 公尺內其他候選 1 筆") in user
    assert ("2. 浅草寺｜Overture｜buddhist_temple｜東京都 台東区 東京都台東区浅草2-3-1｜35.7148, 139.7966｜"
            "離同一天的「晴空塔」約 1.4 公里｜300 公尺內其他候選 1 筆") in user
    # 兩份地圖的同一個地方，挑哪一筆都可以：挑了 Overture 那一筆
    assert source(conn, r.place_id) == ("overture", "ovt-sensoji")


def test_area_is_stored_with_its_boundary(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1)

    r = resolve(geocoder, ref("上野", "上野", kind="景點"))

    p = place_table.get(conn, r.place_id)
    assert p.kind == "區域" and p.boundary["type"] == "Polygon"


def test_same_map_object_written_twice_is_one_row(conn, setup):
    pid = place_table.add(conn, source="osm", source_id="way/2", kind="景點", name_ja="東京スカイツリー",
                          name_zh="東京晴空塔", city="東京", lat=SKYTREE.lat, lon=SKYTREE.lon)
    geocoder, spy, llm, gsi = setup(pick=1)

    r = resolve(geocoder, ref("天空樹", "スカイツリー"))

    assert r.place_id == pid
    assert conn.execute("SELECT COUNT(*) FROM place").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM place_source").fetchone() == (1,)
    assert place_table.get(conn, pid).name_zh == "東京晴空塔"
    assert aliases(conn, "place_alias_zh") == [(pid, "天空樹")]
    assert aliases(conn, "place_alias_ja") == [(pid, "スカイツリー")]


def test_found_in_round_two_with_the_next_plan(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1, plan=[plan("ソラマチのお店", pref="東京"), plan("東京ソラマチ", pref="東京")])

    r = resolve(geocoder, ref("晴空街道的某間店", "ソラマチのお店"))

    assert spy.searches == [("ソラマチのお店", "東京都"), ("東京ソラマチ", "東京都")]
    second = llm.users("plan")[1]
    assert "<history>\n第 1 輪：名稱 ソラマチのお店｜範圍 東京都｜沒有候選\n</history>" in second
    p = place_table.get(conn, r.place_id)
    assert p.name_ja == "東京ソラマチ"
    # 只加抽取時的日文名稱；llm 列的其他寫法可能很籠統，不加成別名
    assert aliases(conn, "place_alias_zh") == [(p.id, "晴空街道的某間店")]
    assert aliases(conn, "place_alias_ja") == [(p.id, "ソラマチのお店")]


def test_next_plan_sees_why_none_of_the_candidates_was_picked(conn, setup, use_llm):
    geocoder, spy, llm, gsi = setup(pick=[0, 1], plan=[plan("清水寺", pref="東京"), plan("浅草寺", pref="東京")])

    r = resolve(geocoder, ref("淺草的清水寺", "清水寺"))

    assert "第 1 輪：名稱 清水寺｜範圍 東京都｜1 筆候選，AI 都不選：" in llm.users("plan")[1]
    assert source(conn, r.place_id) == ("osm", SENSOJI.source_id)


def test_out_of_range_choice_counts_as_none(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=[9, 1], plan=[plan("浅草寺", pref="東京"), plan("スカイツリー", pref="東京")])

    r = resolve(geocoder, ref("淺草寺", "浅草寺"))

    assert spy.searches == [("浅草寺", "東京都"), ("スカイツリー", "東京都")]
    assert source(conn, r.place_id) == ("osm", SKYTREE.source_id)


def test_same_names_and_area_are_not_searched_again(conn, setup):
    geocoder, spy, llm, gsi = setup(plan=[plan("築地場外市場", pref="東京"), plan("築地場外市場", pref="東京都"),
                                          plan(" 築地場外市場", pref="東京")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert spy.searches == [("築地場外市場", "東京都")]
    assert "第 2 輪：名稱 築地場外市場｜範圍 東京都｜跟之前的名稱和範圍一樣，沒有查" in llm.users("plan")[2]
    assert r.place_id is None and "<tried>築地場外市場</tried>" in llm.users("web")[0]


# ---------- 三輪都沒有：上網查地址 ----------

def test_three_rounds_without_a_result_then_web_finds_nothing_so_it_is_dropped(conn, setup):
    geocoder, spy, llm, gsi = setup(plan=[plan("築地場外市場", pref="東京"), plan("築地市場", "築地場外", pref="東京"),
                                          plan("築地")])

    assert dropped(geocoder, ref("築地場外市場", "築地場外市場")) == glog.WEB_NOT_FOUND
    assert spy.searches == [("築地場外市場", "東京都"), ("築地市場、築地場外", "東京都"), ("築地", "全日本")]
    assert len(llm.users("plan")) == 3
    [user] = llm.users("web")
    assert "<place>築地場外市場｜築地場外市場｜東京｜景點</place>" in user
    assert "<tried>築地場外市場、築地市場、築地場外、築地</tried>" in user
    assert llm.tools["web"] == ["WebSearch", "WebFetch"] and llm.tools["plan"] == []
    assert gsi.queries == []
    assert conn.execute("SELECT COUNT(*) FROM place").fetchone() == (0,)


def test_web_address_is_turned_into_coordinates_by_gsi(conn, setup):
    geocoder, spy, llm, gsi = setup(web=web("東京都中央区築地4-16-2", "https://www.tsukiji.or.jp/"))

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場", kind="區域"))

    assert gsi.queries == ["東京都中央区築地4-16-2"]
    p = place_table.get(conn, r.place_id)
    assert (p.kind, p.name_ja, p.name_zh, p.city, p.lat, p.lon, p.boundary) == \
        ("區域", "築地場外市場", "築地場外市場", "東京", 35.6654, 139.7707, None)
    assert source(conn, p.id) == ("gsi", "東京都中央区築地四丁目｜築地場外市場")
    assert aliases(conn, "place_alias_zh") == [(p.id, "築地場外市場")]
    assert aliases(conn, "place_alias_ja") == [(p.id, "築地場外市場")]


def test_found_true_with_an_empty_address_counts_as_not_found(conn, setup):
    geocoder, spy, llm, gsi = setup(web={"found": True, "address": " ", "source_url": ""})

    assert dropped(geocoder, ref("築地場外市場", "築地場外市場")) == glog.WEB_NOT_FOUND
    assert gsi.queries == []


def test_address_gsi_cannot_find_is_dropped(conn, setup):
    geocoder, spy, llm, gsi = setup(web=web("東京都中央区築地9-99", "https://example.com/tsukiji"))

    assert dropped(geocoder, ref("築地場外市場", "築地場外市場")) == glog.GSI_NOT_FOUND
    assert gsi.queries == ["東京都中央区築地9-99"]


def test_same_gsi_address_with_different_names_is_two_places(conn, setup):
    geocoder, spy, llm, gsi = setup(web=[web("東京都中央区築地4-16-2"), web("東京都中央区築地4-10-5")])

    a = resolve(geocoder, ref("壽司大", "すし大"))
    b = resolve(geocoder, ref("狐狸屋", "きつねや"))

    assert a.place_id != b.place_id
    assert [source(conn, x.place_id) for x in (a, b)] == [
        ("gsi", "東京都中央区築地四丁目｜すし大"), ("gsi", "東京都中央区築地四丁目｜きつねや")]


# ---------- 猜的 ----------

def test_guessed_branch_does_not_get_a_chinese_alias(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1)

    r = resolve(geocoder, ref("燒肉 KING", "焼肉きんぐ 浅草店", located="猜的", reason="當天都在淺草"))

    p = place_table.get(conn, r.place_id)
    assert (source(conn, p.id), p.name_zh) == (("overture", KING_ASAKUSA.source_id), None)
    assert aliases(conn, "place_alias_zh") == []
    assert aliases(conn, "place_alias_ja") == [(p.id, "焼肉きんぐ 浅草店")]
    assert "<guessed>是</guessed>" in llm.users("pick")[0]


def test_guessed_branch_is_not_looked_up_by_its_chinese_name(conn, setup):
    asakusa = place_table.add(conn, source="overture", source_id="ovt-king-asakusa", kind="景點",
                              name_ja="焼肉きんぐ 浅草店", name_zh="燒肉 KING", city="東京", lat=35.712, lon=139.794)
    geocoder, spy, llm, gsi = setup(pick=2)

    r = resolve(geocoder, ref("燒肉 KING", "焼肉きんぐ", located="猜的"))

    assert spy.searches == [("焼肉きんぐ", "東京都")]
    assert r.place_id != asakusa


def test_guessed_branch_is_resolved_after_the_others_and_shown_how_far_it_is(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1)
    parsed = Parsed(
        places=[ref("燒肉 KING", "焼肉きんぐ", located="猜的", ref="p1"), ref("淺草寺", "浅草寺", ref="p2"),
                ref("沒人提到的地方", "どこか", ref="p3")],
        days=[Day("D1", "2", ["p1", "p2"])], place_notes=[], moves=[], day_notes=[])

    place_ids = asyncio.run(geocoder.resolve_article(ARTICLE, parsed))

    assert set(place_ids) == {"p1", "p2"} and all(place_ids.values())
    assert spy.searches == [("浅草寺", "東京都"), ("焼肉きんぐ", "東京都")]
    assert "1. 淺草寺（35.7148, 139.7967）" in llm.users("plan")[1]
    king = llm.users("pick")[1]
    assert "<same_day>淺草寺</same_day>" in king
    assert "焼肉きんぐ 浅草店" in king and "離同一天的「淺草寺」約 0.4 公里" in king
    assert "離同一天的「淺草寺」約 9.2 公里" in king


# ---------- 猜不出來 ----------

@pytest.mark.parametrize("p", [ref("餐廳", "レストラン", located="猜不出來"),
                               ref("飯店", "ホテル", located="猜不出來", reason="沒寫是哪一間")])
def test_place_that_cannot_be_guessed_is_dropped_without_searching(conn, setup, p):
    geocoder, spy, llm, gsi = setup()

    assert dropped(geocoder, p) == glog.NO_GUESS
    assert spy.searches == [] and llm.calls == []


# ---------- 一篇文章 ----------

def test_only_places_used_by_the_itinerary_notes_or_moves_are_resolved(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1)
    parsed = Parsed(
        places=[ref("淺草寺", "浅草寺", ref="p1"), ref("晴空塔", "スカイツリー", ref="p2"),
                ref("阿美橫町", "アメ横商店街", ref="p3"), ref("餐廳", "レストラン", located="猜不出來", ref="p4"),
                ref("飯店", "ホテル", located="猜不出來", ref="p5")],
        days=[Day("D1", "2", ["p1", "p5"])],
        place_notes=[PlaceNote("p2", "晚上很美", [])], moves=[], day_notes=[])

    place_ids = asyncio.run(geocoder.resolve_article(ARTICLE, parsed))

    assert spy.searches == [("浅草寺", "東京都"), ("スカイツリー", "東京都")]
    assert set(place_ids) == {"p1", "p2", "p5"} and place_ids["p1"] and place_ids["p2"] and place_ids["p5"] is None
