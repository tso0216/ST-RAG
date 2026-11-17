"""地點解析：一個地點從「找得到嗎」到有結果或三次無結果。"""
import asyncio

import pytest

from tests.v4.extract.fakes import FakeNominatim, KING_ASAKUSA, SENSOJI, SKYTREE
from v4 import place as place_table
from v4.extract import unresolved
from v4.extract.data_schema import NO_GUESS, NOT_FOUND, Context, Day, Parsed, PlaceNote, PlaceRef, Unresolved
from v4.extract.geocoding import Geocoder
from v4.extract.osm_search import OsmSearch

ARTICLE = "M.1.A.AAA"


def ref(name_zh, name_ja, city="東京", located="是", ref="p1", kind="景點", reason="") -> PlaceRef:
    return PlaceRef(ref, name_zh, name_ja, city, kind, located, reason, sentence=f"去{name_zh}")


def answers(*rows: dict) -> unresolved.Answers:
    blank = dict.fromkeys(unresolved.COLUMNS, "")
    return unresolved.Answers([{**blank, **r} for r in rows])


@pytest.fixture
def setup(conn, use_llm):
    """回傳函式：建一個 Geocoder，連同假的 Nominatim 和假的 llm 一起回傳。"""
    def build(answer_rows=(), world=None, **llm):
        osm = FakeNominatim(*([world] if world is not None else []))
        return Geocoder(conn, OsmSearch(osm), answers(*answer_rows)), osm, use_llm(**llm)
    return build


def resolve(geocoder, p, in_itinerary=True, **ctx):
    return asyncio.run(geocoder.resolve(p, Context(ARTICLE, in_itinerary=in_itinerary, **ctx)))


def aliases(conn, table) -> list[tuple]:
    return conn.execute(f"SELECT id, alias FROM {table} ORDER BY id, alias").fetchall()


# ---------- 資料庫 ----------

def test_place_already_in_database_is_used_without_asking_osm(conn, setup):
    pid = place_table.add(conn, osm_type="way", osm_id=1, kind="景點", name_ja="浅草寺", name_zh="淺草寺",
                          city="東京", lat=35.7148, lon=139.7967)
    geocoder, osm, llm = setup()

    r = resolve(geocoder, ref("淺草寺", "あさくさでら"))

    assert r.place_id == pid
    assert osm.searches == [] and llm.calls == []


def test_several_database_matches_fall_through_to_osm(conn, setup):
    place_table.add(conn, osm_type="way", osm_id=21, kind="景點", name_ja="清水寺", name_zh="清水寺",
                    city="京都", lat=34.9949, lon=135.7850)
    other = place_table.add(conn, osm_type="way", osm_id=99, kind="景點", name_ja="音羽山清水寺", name_zh="音羽山清水寺",
                            city="京都", lat=34.9, lon=135.7)
    place_table.add_alias(conn, other, name_zh="清水寺")
    geocoder, osm, llm = setup(pick=1)

    r = resolve(geocoder, ref("清水寺", "清水寺", city="京都"))

    assert osm.searches == [("清水寺", "京都")]
    assert place_table.get(conn, r.place_id).osm_id == 21


def test_same_name_in_another_city_is_not_a_database_match(conn, setup):
    place_table.add(conn, osm_type="way", osm_id=21, kind="景點", name_ja="清水寺", name_zh="清水寺",
                    city="京都", lat=34.9949, lon=135.7850)
    geocoder, osm, llm = setup(pick=2)

    r = resolve(geocoder, ref("清水寺", "清水寺", city="東京"))

    assert osm.searches == [("清水寺", "東京")]
    assert place_table.get(conn, r.place_id).osm_id == 22


# ---------- OSM 和 llm ----------

def test_llm_picks_one_of_the_osm_results_and_it_is_added_with_aliases(conn, setup):
    geocoder, osm, llm = setup(pick=1)

    r = resolve(geocoder, ref("晴空塔", "スカイツリー"), same_day=[])

    p = place_table.get(conn, r.place_id)
    assert (p.osm_type, p.osm_id, p.kind, p.name_ja, p.name_zh, p.city) == \
        ("way", 2, "景點", "東京スカイツリー", "晴空塔", "東京")
    assert (p.lat, p.lon) == (SKYTREE.lat, SKYTREE.lon)
    assert aliases(conn, "place_alias_zh") == [(p.id, "晴空塔")]
    assert aliases(conn, "place_alias_ja") == [(p.id, "スカイツリー")]
    [user] = llm.users("pick")
    assert "晴空塔｜スカイツリー｜東京｜景點" in user and "去晴空塔" in user
    assert "1. 東京スカイツリー｜tourism:yes｜東京スカイツリー, 墨田区, 東京都, 日本" in user


def test_area_is_stored_with_its_boundary(conn, setup):
    geocoder, osm, llm = setup(pick=1)

    r = resolve(geocoder, ref("上野", "上野", kind="景點"))

    p = place_table.get(conn, r.place_id)
    assert p.kind == "區域" and p.boundary["type"] == "Polygon"


def test_osm_object_already_in_database_only_gets_new_aliases(conn, setup):
    pid = place_table.add(conn, osm_type="way", osm_id=2, kind="景點", name_ja="東京スカイツリー", name_zh="東京晴空塔",
                          city="東京", lat=SKYTREE.lat, lon=SKYTREE.lon)
    geocoder, osm, llm = setup(pick=1)

    r = resolve(geocoder, ref("天空樹", "スカイツリー"))

    assert r.place_id == pid
    assert conn.execute("SELECT COUNT(*) FROM place").fetchone() == (1,)
    assert place_table.get(conn, pid).name_zh == "東京晴空塔"
    assert aliases(conn, "place_alias_zh") == [(pid, "天空樹")]
    assert aliases(conn, "place_alias_ja") == [(pid, "スカイツリー")]


def test_llm_provides_another_name_when_osm_has_nothing(conn, setup):
    geocoder, osm, llm = setup(pick=1, new_name=[("東京ソラマチ", "東京")])

    r = resolve(geocoder, ref("晴空街道的某間店", "ソラマチのお店"))

    assert osm.searches == [("ソラマチのお店", "東京"), ("東京ソラマチ", "東京")]
    [user] = llm.users("new_name")
    assert "<tried>ソラマチのお店</tried>" in user and "<last>沒有結果</last>" in user
    p = place_table.get(conn, r.place_id)
    assert p.name_ja == "東京ソラマチ"
    assert aliases(conn, "place_alias_zh") == [(p.id, "晴空街道的某間店")]
    assert aliases(conn, "place_alias_ja") == [(p.id, "ソラマチのお店"), (p.id, "東京ソラマチ")]


def test_llm_provides_another_name_when_none_of_the_results_is_right(conn, setup):
    geocoder, osm, llm = setup(pick=[0, 1], new_name=[("浅草寺", "東京")])

    r = resolve(geocoder, ref("淺草的清水寺", "清水寺"))

    assert "<last>有結果，但都不是這個地點</last>" in llm.users("new_name")[0]
    assert place_table.get(conn, r.place_id).osm_id == SENSOJI.osm_id


def test_three_rounds_without_a_result_is_left_for_the_user(conn, setup):
    geocoder, osm, llm = setup(new_name=[("築地市場", "東京"), ("築地", "")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"), where="D1 第 1 站")

    assert r.place_id is None and not r.excluded
    assert r.unresolved == Unresolved(
        NOT_FOUND, ARTICLE, "築地場外市場", "築地場外市場", "東京", guessed=False, sentence="去築地場外市場",
        tried=["築地場外市場", "築地市場", "築地"], reason="", where="D1 第 1 站")
    assert osm.searches == [("築地場外市場", "東京"), ("築地市場", "東京"), ("築地", "")]
    assert len(llm.users("new_name")) == 2
    assert conn.execute("SELECT COUNT(*) FROM place").fetchone() == (0,)


def test_a_name_already_tried_is_not_searched_again(conn, setup):
    geocoder, osm, llm = setup(new_name=[("築地場外市場", "東京"), ("", "")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert osm.searches == [("築地場外市場", "東京")]
    assert r.unresolved.tried == ["築地場外市場"]


# ---------- 猜的 ----------

def test_guessed_branch_does_not_get_a_chinese_alias(conn, setup):
    geocoder, osm, llm = setup(pick=1)

    r = resolve(geocoder, ref("燒肉 KING", "焼肉きんぐ 浅草店", located="猜的", reason="當天都在淺草"))

    p = place_table.get(conn, r.place_id)
    assert (p.osm_id, p.name_zh) == (KING_ASAKUSA.osm_id, None)
    assert aliases(conn, "place_alias_zh") == []
    assert aliases(conn, "place_alias_ja") == [(p.id, "焼肉きんぐ 浅草店")]
    assert "<guessed>是</guessed>" in llm.users("pick")[0]


def test_guessed_branch_is_not_looked_up_by_its_chinese_name(conn, setup):
    asakusa = place_table.add(conn, osm_type="node", osm_id=11, kind="景點", name_ja="焼肉きんぐ 浅草店",
                              name_zh="燒肉 KING", city="東京", lat=35.712, lon=139.794)
    geocoder, osm, llm = setup(pick=2)

    r = resolve(geocoder, ref("燒肉 KING", "焼肉きんぐ", located="猜的"))

    assert osm.searches == [("焼肉きんぐ", "東京")]
    assert r.place_id != asakusa


def test_guessed_branch_is_resolved_after_the_others_and_shown_how_far_it_is(conn, setup):
    geocoder, osm, llm = setup(pick=1)
    parsed = Parsed(
        places=[ref("燒肉 KING", "焼肉きんぐ", located="猜的", ref="p1"), ref("淺草寺", "浅草寺", ref="p2"),
                ref("沒人提到的地方", "どこか", ref="p3")],
        days=[Day("D1", "2", ["p1", "p2"])], place_notes=[], moves=[], day_notes=[])

    place_ids, undecided = asyncio.run(geocoder.resolve_article(ARTICLE, parsed))

    assert undecided == [] and set(place_ids) == {"p1", "p2"}
    assert osm.searches == [("浅草寺", "東京"), ("焼肉きんぐ", "東京")]
    king = llm.users("pick")[1]
    assert "<same_day>淺草寺</same_day>" in king
    assert "焼肉きんぐ 浅草店" in king and "離同一天的「淺草寺」約 0.4 公里" in king
    assert "離同一天的「淺草寺」約 9.2 公里" in king


# ---------- 猜不出來 ----------

def test_place_that_cannot_be_guessed_and_is_not_a_stop_is_left_out(conn, setup):
    geocoder, osm, llm = setup()

    r = resolve(geocoder, ref("餐廳", "レストラン", located="猜不出來"), in_itinerary=False)

    assert r.excluded and r.unresolved is None
    assert osm.searches == [] and llm.calls == []


def test_place_that_cannot_be_guessed_and_is_a_stop_is_left_for_the_user(conn, setup):
    geocoder, osm, llm = setup()

    r = resolve(geocoder, ref("飯店", "ホテル", located="猜不出來", reason="沒寫是哪一間"), where="D1 第 3 站")

    assert r.unresolved.type == NO_GUESS
    assert (r.unresolved.name_zh, r.unresolved.reason, r.unresolved.where) == ("飯店", "沒寫是哪一間", "D1 第 3 站")
    assert osm.searches == [] and llm.calls == []


def test_answer_for_place_that_cannot_be_guessed_is_used_without_aliasing_its_generic_name(conn, setup):
    rows = [{"type": "猜不出來", "name_zh": "飯店", "articles": ARTICLE, "answer": "東京スカイツリー"},
            {"type": "猜不出來", "name_zh": "飯店", "articles": "M.2.A.BBB", "answer": "不收"}]
    geocoder, osm, llm = setup(rows)

    r = resolve(geocoder, ref("飯店", "ホテル", located="猜不出來"))

    p = place_table.get(conn, r.place_id)
    assert (p.osm_id, p.name_zh) == (SKYTREE.osm_id, None)
    assert aliases(conn, "place_alias_zh") == []
    assert aliases(conn, "place_alias_ja") == [(p.id, "東京スカイツリー")]
    assert llm.calls == []


# ---------- 使用者填的答案 ----------

def not_found_row(answer, name_zh="築地場外市場", city="東京") -> dict:
    return {"type": "解析不到", "name_zh": name_zh, "city": city, "articles": "M.9.A.ZZZ", "answer": answer}


def test_answer_with_osm_url_uses_that_object(conn, setup):
    geocoder, osm, llm = setup([not_found_row("https://www.openstreetmap.org/way/3")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert osm.lookups == [("way", 3)] and osm.searches == [] and llm.calls == []
    p = place_table.get(conn, r.place_id)
    assert (p.osm_type, p.osm_id, p.name_zh) == ("way", 3, "築地場外市場")
    assert aliases(conn, "place_alias_zh") == [(p.id, "築地場外市場")]


def test_answer_with_japanese_name_takes_the_first_osm_result(conn, setup):
    geocoder, osm, llm = setup([not_found_row("清水寺")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert osm.searches == [("清水寺", "東京")] and llm.calls == []
    p = place_table.get(conn, r.place_id)
    assert p.osm_id == 21
    assert aliases(conn, "place_alias_ja") == [(p.id, "清水寺"), (p.id, "築地場外市場")]


def test_answer_with_coordinates_adds_a_manual_place(conn, setup):
    geocoder, osm, llm = setup([not_found_row("35.6655, 139.7707")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    p = place_table.get(conn, r.place_id)
    assert (p.osm_type, p.osm_id, p.kind) == ("manual", 1, "景點")
    assert (p.name_ja, p.name_zh, p.lat, p.lon) == ("築地場外市場", "築地場外市場", 35.6655, 139.7707)
    assert osm.searches == [] and osm.lookups == []
    # 之後別篇出現同樣的寫法，資料庫就查得到
    assert resolve(geocoder, ref("築地場外市場", "別的日文寫法")).place_id == p.id


def test_answer_to_skip_leaves_the_place_out(conn, setup):
    geocoder, osm, llm = setup([not_found_row("不收")])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert r.excluded and r.place_id is None and r.unresolved is None


@pytest.mark.parametrize("answer", ["沒有這個名字", "https://www.openstreetmap.org/way/999"])
def test_answer_that_osm_cannot_find_is_reported_again(conn, setup, answer):
    geocoder, osm, llm = setup([not_found_row(answer)])

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert r.unresolved.type == NOT_FOUND
    assert f"填的答案「{answer}」在 OSM 查不到" in r.unresolved.note


def test_answer_does_not_apply_to_a_different_city(conn, setup):
    geocoder, osm, llm = setup([not_found_row("不收", city="大阪")], pick=0)

    r = resolve(geocoder, ref("築地場外市場", "築地場外市場"))

    assert r.unresolved is not None and osm.searches[0] == ("築地場外市場", "東京")


# ---------- 一篇文章 ----------

def test_only_places_used_by_the_itinerary_notes_or_moves_are_resolved(conn, setup):
    geocoder, osm, llm = setup(pick=1)
    parsed = Parsed(
        places=[ref("淺草寺", "浅草寺", ref="p1"), ref("晴空塔", "スカイツリー", ref="p2"),
                ref("阿美橫町", "アメ横商店街", ref="p3"), ref("餐廳", "レストラン", located="猜不出來", ref="p4"),
                ref("飯店", "ホテル", located="猜不出來", ref="p5")],
        days=[Day("D1", "2", ["p1", "p5"])],
        place_notes=[PlaceNote("p2", "晚上很美", [])], moves=[], day_notes=[])

    place_ids, undecided = asyncio.run(geocoder.resolve_article(ARTICLE, parsed))

    assert osm.searches == [("浅草寺", "東京"), ("スカイツリー", "東京")]
    assert set(place_ids) == {"p1", "p2"} and all(place_ids.values())
    [hotel] = undecided
    assert (hotel.type, hotel.name_zh, hotel.where, hotel.same_day) == (NO_GUESS, "飯店", "D1 第 2 站", ["淺草寺"])
