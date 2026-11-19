"""地點解析：一個地點從「找得到嗎」到有結果，或本機地圖三輪都沒有、上網查地址也沒有。"""
import asyncio

import pytest

from tests.v4.extract.fakes import FakeGsi, KING_ASAKUSA, SENSOJI, SKYTREE, web
from v4 import place as place_table
from v4.extract import unresolved
from v4.extract.data_schema import NO_GUESS, NOT_FOUND, Context, Day, Nearby, Parsed, PlaceNote, PlaceRef, Unresolved
from v4.extract.geocoding import Geocoder

ARTICLE = "M.1.A.AAA"


def ref(name_zh, name_ja, city="東京", located="是", ref="p1", kind="景點", reason="") -> PlaceRef:
    return PlaceRef(ref, name_zh, name_ja, city, kind, located, reason, sentence=f"去{name_zh}")


def answers(*rows: dict) -> unresolved.Answers:
    blank = dict.fromkeys(unresolved.COLUMNS, "")
    return unresolved.Answers([{**blank, **r} for r in rows])


class Spy:
    """記下本機地圖的每一次查詢，其他照原本的做。"""

    def __init__(self, local_map):
        self.map = local_map
        self.searches: list[tuple[str, str]] = []
        self.lookups: list[tuple[str, str]] = []

    def search(self, name, city=""):
        self.searches.append((name, city))
        return self.map.search(name, city)

    def lookup(self, source, source_id):
        self.lookups.append((source, source_id))
        return self.map.lookup(source, source_id)


@pytest.fixture
def setup(conn, local_map, use_llm):
    """回傳函式：建一個 Geocoder，連同記錄查詢的本機地圖、假的國土地理院、假的 llm 一起回傳。"""
    def build(answer_rows=(), gsi=None, **llm):
        spy, gsi = Spy(local_map), gsi or FakeGsi()
        return Geocoder(conn, spy, answers(*answer_rows), gsi=gsi), spy, use_llm(**llm), gsi
    return build


def resolve(geocoder, p, in_itinerary=True, **ctx):
    return asyncio.run(geocoder.resolve(p, Context(ARTICLE, in_itinerary=in_itinerary, **ctx)))


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

    assert spy.searches == [("清水寺", "京都")]
    assert source(conn, r.place_id) == ("osm", "way/21")


def test_same_name_in_another_city_is_not_a_database_match(conn, setup):
    place_table.add(conn, source="osm", source_id="way/21", kind="景點", name_ja="清水寺", name_zh="清水寺",
                    city="京都", lat=34.9949, lon=135.7850)
    geocoder, spy, llm, gsi = setup(pick=1)

    r = resolve(geocoder, ref("清水寺", "清水寺", city="東京"))

    assert spy.searches == [("清水寺", "東京")]
    assert source(conn, r.place_id) == ("osm", "way/22")   # 同城市的排第一


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
    assert "1. 東京スカイツリー｜OSM｜tourism=attraction｜東京都 墨田区" in user
    assert gsi.queries == [] and llm.users("web") == []


def test_candidates_show_source_category_address_and_distance(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=2)

    r = resolve(geocoder, ref("淺草寺", "浅草寺"), same_day=[Nearby("晴空塔", SKYTREE.lat, SKYTREE.lon)])

    user = llm.users("pick")[0]
    assert "1. 浅草寺｜OSM｜amenity=place_of_worship｜東京都 台東区 浅草2-3-1｜離同一天的「晴空塔」約 1.4 公里" in user
    assert "2. 浅草寺｜Overture｜buddhist_temple｜東京都 台東区 東京都台東区浅草2-3-1｜離同一天的「晴空塔」約 1.4 公里" in user
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


def test_found_in_round_two_after_the_llm_gives_another_name(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1, new_name=[("東京ソラマチ", "東京")])

    r = resolve(geocoder, ref("晴空街道的某間店", "ソラマチのお店"))

    assert spy.searches == [("ソラマチのお店", "東京"), ("東京ソラマチ", "東京")]
    [user] = llm.users("new_name")
    assert "<tried>ソラマチのお店</tried>" in user and "<last>沒有結果</last>" in user
    p = place_table.get(conn, r.place_id)
    assert p.name_ja == "東京ソラマチ"
    assert aliases(conn, "place_alias_zh") == [(p.id, "晴空街道的某間店")]
    assert aliases(conn, "place_alias_ja") == [(p.id, "ソラマチのお店"), (p.id, "東京ソラマチ")]


def test_llm_provides_another_name_when_none_of_the_candidates_is_right(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=[0, 1], new_name=[("浅草寺", "東京")])

    r = resolve(geocoder, ref("淺草的清水寺", "清水寺"))

    assert "<last>有結果，但都不是這個地點</last>" in llm.users("new_name")[0]
    assert source(conn, r.place_id) == ("osm", SENSOJI.source_id)


def test_out_of_range_choice_counts_as_none(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=[9, 1], new_name=[("スカイツリー", "東京")])

    r = resolve(geocoder, ref("淺草寺", "浅草寺"))

    assert spy.searches == [("浅草寺", "東京"), ("スカイツリー", "東京")]
    assert source(conn, r.place_id) == ("osm", SKYTREE.source_id)


def test_a_name_already_tried_is_not_searched_again(conn, setup):
    geocoder, spy, llm, gsi = setup(new_name=[("築地場外市場", "東京"), ("", "")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert spy.searches == [("築地場外市場", "東京")]
    assert r.unresolved.tried == ["築地場外市場"]


# ---------- 三輪都沒有：上網查地址 ----------

def test_three_rounds_without_a_result_then_web_finds_nothing(conn, setup):
    geocoder, spy, llm, gsi = setup(new_name=[("築地市場", "東京"), ("築地", "")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"), where="D1 第 1 站")

    assert r.place_id is None and not r.excluded
    assert r.unresolved == Unresolved(
        NOT_FOUND, ARTICLE, "築地場外市場", "築地場外市場", "東京", guessed=False, sentence="去築地場外市場",
        tried=["築地場外市場", "築地市場", "築地"], reason="", where="D1 第 1 站")
    assert spy.searches == [("築地場外市場", "東京"), ("築地市場", "東京"), ("築地", "")]
    assert len(llm.users("new_name")) == 2
    [user] = llm.users("web")
    assert "<place>築地場外市場｜築地場外市場｜東京｜景點</place>" in user
    assert "<tried>築地場外市場、築地市場、築地</tried>" in user
    assert llm.tools["web"] == ["WebSearch", "WebFetch"] and llm.tools["new_name"] == []
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

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert r.unresolved is not None and gsi.queries == []


def test_address_gsi_cannot_find_is_left_for_the_user_with_the_address_in_the_note(conn, setup):
    geocoder, spy, llm, gsi = setup(web=web("東京都中央区築地9-99", "https://example.com/tsukiji"))

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert gsi.queries == ["東京都中央区築地9-99"]
    assert r.unresolved.type == NOT_FOUND and r.unresolved.tried == ["築地場外市場"]
    assert "東京都中央区築地9-99" in r.unresolved.note and "https://example.com/tsukiji" in r.unresolved.note
    assert "國土地理院查不到這個地址" in r.unresolved.note


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

    assert spy.searches == [("焼肉きんぐ", "東京")]
    assert r.place_id != asakusa


def test_guessed_branch_is_resolved_after_the_others_and_shown_how_far_it_is(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1)
    parsed = Parsed(
        places=[ref("燒肉 KING", "焼肉きんぐ", located="猜的", ref="p1"), ref("淺草寺", "浅草寺", ref="p2"),
                ref("沒人提到的地方", "どこか", ref="p3")],
        days=[Day("D1", "2", ["p1", "p2"])], place_notes=[], moves=[], day_notes=[])

    place_ids, undecided = asyncio.run(geocoder.resolve_article(ARTICLE, parsed))

    assert undecided == [] and set(place_ids) == {"p1", "p2"}
    assert spy.searches == [("浅草寺", "東京"), ("焼肉きんぐ", "東京")]
    king = llm.users("pick")[1]
    assert "<same_day>淺草寺</same_day>" in king
    assert "焼肉きんぐ 浅草店" in king and "離同一天的「淺草寺」約 0.4 公里" in king
    assert "離同一天的「淺草寺」約 9.2 公里" in king


# ---------- 猜不出來 ----------

def test_place_that_cannot_be_guessed_and_is_not_a_stop_is_left_out(conn, setup):
    geocoder, spy, llm, gsi = setup()

    r = resolve(geocoder, ref("餐廳", "レストラン", located="猜不出來"), in_itinerary=False)

    assert r.excluded and r.unresolved is None
    assert spy.searches == [] and llm.calls == []


def test_place_that_cannot_be_guessed_and_is_a_stop_is_left_for_the_user(conn, setup):
    geocoder, spy, llm, gsi = setup()

    r = resolve(geocoder, ref("飯店", "ホテル", located="猜不出來", reason="沒寫是哪一間"), where="D1 第 3 站")

    assert r.unresolved.type == NO_GUESS
    assert (r.unresolved.name_zh, r.unresolved.reason, r.unresolved.where) == ("飯店", "沒寫是哪一間", "D1 第 3 站")
    assert spy.searches == [] and llm.calls == []


def test_answer_for_place_that_cannot_be_guessed_is_used_without_aliasing_its_generic_name(conn, setup):
    rows = [{"type": "猜不出來", "name_zh": "飯店", "articles": ARTICLE, "answer": "東京スカイツリー"},
            {"type": "猜不出來", "name_zh": "飯店", "articles": "M.2.A.BBB", "answer": "不收"}]
    geocoder, spy, llm, gsi = setup(rows)

    r = resolve(geocoder, ref("飯店", "ホテル", located="猜不出來"))

    p = place_table.get(conn, r.place_id)
    assert (source(conn, p.id), p.name_zh) == (("osm", SKYTREE.source_id), None)
    assert aliases(conn, "place_alias_zh") == []
    assert aliases(conn, "place_alias_ja") == [(p.id, "東京スカイツリー")]
    assert llm.calls == []


# ---------- 使用者填的答案 ----------

def not_found_row(answer, name_zh="築地場外市場", city="東京") -> dict:
    return {"type": "解析不到", "name_zh": name_zh, "city": city, "articles": "M.9.A.ZZZ", "answer": answer}


def test_answer_with_osm_url_is_looked_up_in_the_local_map(conn, setup):
    geocoder, spy, llm, gsi = setup([not_found_row("https://www.openstreetmap.org/way/3")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert spy.lookups == [("osm", "way/3")] and spy.searches == [] and llm.calls == []
    p = place_table.get(conn, r.place_id)
    assert (source(conn, p.id), p.name_zh) == (("osm", "way/3"), "築地場外市場")
    assert aliases(conn, "place_alias_zh") == [(p.id, "築地場外市場")]


def test_answer_with_japanese_name_takes_the_first_fuzzy_search_result(conn, setup):
    geocoder, spy, llm, gsi = setup([not_found_row("清水寺")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert spy.searches == [("清水寺", "東京")] and llm.calls == []
    p = place_table.get(conn, r.place_id)
    assert source(conn, p.id) == ("osm", "way/22")   # 同城市的排第一
    assert aliases(conn, "place_alias_ja") == [(p.id, "清水寺"), (p.id, "築地場外市場")]


def test_answer_with_coordinates_adds_a_manual_place(conn, setup):
    geocoder, spy, llm, gsi = setup([not_found_row("35.6655, 139.7707")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    p = place_table.get(conn, r.place_id)
    assert source(conn, p.id) == ("manual", "35.6655,139.7707") and p.kind == "景點"
    assert (p.name_ja, p.name_zh, p.lat, p.lon) == ("築地場外市場", "築地場外市場", 35.6655, 139.7707)
    assert spy.searches == [] and spy.lookups == []
    # 之後別篇出現同樣的寫法，資料庫就查得到
    assert resolve(geocoder, ref("築地場外市場", "別的日文寫法")).place_id == p.id


def test_answer_to_skip_leaves_the_place_out(conn, setup):
    geocoder, spy, llm, gsi = setup([not_found_row("不收")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert r.excluded and r.place_id is None and r.unresolved is None


@pytest.mark.parametrize("answer", ["沒有這個名字", "https://www.openstreetmap.org/way/999"])
def test_answer_that_the_local_map_cannot_find_is_reported_again(conn, setup, answer):
    geocoder, spy, llm, gsi = setup([not_found_row(answer)])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert r.unresolved.type == NOT_FOUND
    assert f"填的答案「{answer}」在本機地圖查不到，請改填座標" in r.unresolved.note


def test_answer_does_not_apply_to_a_different_city(conn, setup):
    geocoder, spy, llm, gsi = setup([not_found_row("不收", city="大阪")], pick=0)

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert r.unresolved is not None and spy.searches[0] == ("築地場外市場", "東京")


# ---------- 一篇文章 ----------

def test_only_places_used_by_the_itinerary_notes_or_moves_are_resolved(conn, setup):
    geocoder, spy, llm, gsi = setup(pick=1)
    parsed = Parsed(
        places=[ref("淺草寺", "浅草寺", ref="p1"), ref("晴空塔", "スカイツリー", ref="p2"),
                ref("阿美橫町", "アメ横商店街", ref="p3"), ref("餐廳", "レストラン", located="猜不出來", ref="p4"),
                ref("飯店", "ホテル", located="猜不出來", ref="p5")],
        days=[Day("D1", "2", ["p1", "p5"])],
        place_notes=[PlaceNote("p2", "晚上很美", [])], moves=[], day_notes=[])

    place_ids, undecided = asyncio.run(geocoder.resolve_article(ARTICLE, parsed))

    assert spy.searches == [("浅草寺", "東京"), ("スカイツリー", "東京")]
    assert set(place_ids) == {"p1", "p2"} and all(place_ids.values())
    [hotel] = undecided
    assert (hotel.type, hotel.name_zh, hotel.where, hotel.same_day) == (NO_GUESS, "飯店", "D1 第 2 站", ["淺草寺"])
