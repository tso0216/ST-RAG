"""寫入資料庫，和看資料用的 view。"""
import json

from v4 import place as place_table
from tests.v4.extract.fakes import stops
from v4.extract import store
from v4.extract.data_schema import Article, Day, DayNote, Move, Parsed, PlaceNote, PlaceRef, TimeTag

A = "M.1767020760.A.E80"
ARTICLE = Article(A, "[問題] 東京五天行程", "poster", "2025-12-29T21:09:16", "主文", [])


def add_place(conn, osm_id: int, name: str) -> int:
    return place_table.add(conn, source="osm", source_id=f"way/{osm_id}", kind="景點", name_ja=name, name_zh=name,
                           city="東京", lat=35.7, lon=139.7)


def ref(n: int, name_zh: str) -> PlaceRef:
    return PlaceRef(f"p{n}", name_zh, name_zh, "東京", "景點", "是")


def parsed(places=(), days=(), place_notes=(), moves=(), day_notes=(), dropped=0) -> Parsed:
    return Parsed(list(places), list(days), list(place_notes), list(moves), list(day_notes), dropped)


def rows(conn, sql: str) -> list[tuple]:
    return conn.execute(sql).fetchall()


def test_article_is_written_with_its_days_stops_notes_and_moves(conn):
    a, b = add_place(conn, 1, "浅草寺"), add_place(conn, 2, "東京スカイツリー")
    p = parsed(
        places=[ref(1, "淺草寺"), ref(2, "晴空塔")],
        days=[Day("D1", "2", stops("p1", "p2")), Day("D2", "", stops("p2"))],
        place_notes=[PlaceNote("p1", "早上人很少", [TimeTag("2025", "2", "平日", "5-10"), TimeTag(month="12")])],
        moves=[Move("p1", "p2", "走過去 20 分鐘", [TimeTag(time="18-19")])],
        day_notes=[DayNote([1], "這天排太趕"), DayNote([], "整趟都在東京東邊"), DayNote([1, 2], "兩天都很順")])

    assert store.write_article(conn, ARTICLE, p, {"p1": a, "p2": b}) == []

    assert rows(conn, "SELECT id, title, posted_at FROM article") == [
        (A, "[問題] 東京五天行程", "2025-12-29T21:09:16")]
    assert rows(conn, "SELECT id, article_id, month FROM day") == [("1767020760-1", A, "2"), ("1767020760-2", A, None)]
    assert rows(conn, "SELECT day_id, seq, poi_id FROM day_place ORDER BY day_id, seq") == [
        ("1767020760-1", 1, a), ("1767020760-1", 2, b), ("1767020760-2", 1, b)]
    assert rows(conn, "SELECT id, article_id, poi_id, poi_name, content FROM place_note") == [
        ("1767020760-1", A, a, "淺草寺", "早上人很少")]
    assert rows(conn, "SELECT note_id, year, month, day_type, time FROM place_note_time") == [
        ("1767020760-1", 2025, "2", "平日", "5-10"), ("1767020760-1", None, "12", None, None)]
    assert rows(conn, "SELECT id, article_id, from_id, to_id, from_name, to_name, content FROM move") == [
        ("1767020760-1", A, a, b, "淺草寺", "晴空塔", "走過去 20 分鐘")]
    assert rows(conn, "SELECT move_id, year, month, day_type, time FROM move_time") == [
        ("1767020760-1", None, None, None, "18-19")]
    assert rows(conn, "SELECT id, article_id, content FROM day_note") == [
        ("1767020760-1", A, "這天排太趕"), ("1767020760-2", A, "整趟都在東京東邊"), ("1767020760-3", A, "兩天都很順")]
    assert rows(conn, "SELECT note_id, day_id FROM day_note_day ORDER BY note_id, day_id") == [
        ("1767020760-1", "1767020760-1"), ("1767020760-3", "1767020760-1"), ("1767020760-3", "1767020760-2")]


def test_ids_of_another_article_start_from_its_own_number(conn):
    a = add_place(conn, 1, "浅草寺")
    p = parsed(places=[ref(1, "淺草寺")], days=[Day("D1", "", stops("p1"))], place_notes=[PlaceNote("p1", "人很多", [])])
    store.write_article(conn, ARTICLE, p, {"p1": a})

    store.write_article(conn, Article("M.1767035787.A.C5F", "另一篇", "poster", "2025-12-30T01:00:00", "主文", []),
                        p, {"p1": a})

    assert rows(conn, "SELECT id FROM day ORDER BY id") == [("1767020760-1",), ("1767035787-1",)]
    assert rows(conn, "SELECT id FROM place_note ORDER BY id") == [("1767020760-1",), ("1767035787-1",)]


def test_article_id_that_is_not_a_ptt_id_is_used_whole():
    assert store.row_id("M.1767020760.A.E80", 3) == "1767020760-3"
    assert store.row_id("test-article", 3) == "test-article-3"


def test_blank_fields_are_stored_as_null_and_an_all_blank_time_is_not_written(conn):
    a, b = add_place(conn, 1, "浅草寺"), add_place(conn, 2, "東京スカイツリー")
    p = parsed(places=[ref(1, "淺草寺"), ref(2, "晴空塔")],
               place_notes=[PlaceNote("p1", "很好逛", [TimeTag()])],
               moves=[Move("p1", "p2", "很近", [])])

    store.write_article(conn, ARTICLE, p, {"p1": a, "p2": b})

    assert rows(conn, "SELECT * FROM place_note_time") == [] and rows(conn, "SELECT * FROM move_time") == []


def test_unresolved_stop_is_written_without_a_place(conn):
    a, c = add_place(conn, 1, "浅草寺"), add_place(conn, 3, "上野")
    p = parsed(places=[ref(1, "淺草寺"), ref(2, "D1 的飯店"), ref(3, "上野")],
               days=[Day("D1", "", stops("p1", "p3", ("p2", "住宿"))), Day("D2", "", stops("p1"))],
               day_notes=[DayNote([1], "這天很趕"), DayNote([1, 2], "兩天都很趕")])

    assert store.write_article(conn, ARTICLE, p, {"p1": a, "p2": None, "p3": c}) == []

    assert rows(conn, "SELECT id FROM day") == [("1767020760-1",), ("1767020760-2",)]
    assert rows(conn, "SELECT day_id, seq, poi_id, poi_name, type FROM day_place ORDER BY day_id, seq") == [
        ("1767020760-1", 1, a, "淺草寺", "景點"), ("1767020760-1", 2, c, "上野", "景點"),
        ("1767020760-1", 3, None, "D1 的飯店", "住宿"), ("1767020760-2", 1, a, "淺草寺", "景點")]
    assert rows(conn, "SELECT note_id, day_id FROM day_note_day ORDER BY note_id, day_id") == [
        ("1767020760-1", "1767020760-1"), ("1767020760-2", "1767020760-1"), ("1767020760-2", "1767020760-2")]


def test_day_without_stops_is_not_written_and_neither_are_its_notes(conn):
    a = add_place(conn, 1, "浅草寺")
    p = parsed(places=[ref(1, "淺草寺")],
               days=[Day("D1", "", stops("p1")), Day("D2", "", stops()), Day("D3", "", stops("p1"))],
               day_notes=[DayNote([2], "這天還沒排"), DayNote([1, 2], "兩天都很鬆"), DayNote([], "整趟的建議")])

    dropped = store.write_article(conn, ARTICLE, p, {"p1": a})

    assert rows(conn, "SELECT id FROM day") == [("1767020760-1",), ("1767020760-3",)]   # 編號是第幾天，D2 跳過
    assert rows(conn, "SELECT content FROM day_note") == [("整趟的建議",)]
    assert [d.line() for d in dropped] == [
        "M.1767020760.A.E80 day_note 「這天還沒排」：講 D2，D2 沒有站",
        "M.1767020760.A.E80 day_note 「兩天都很鬆」：講 D1、D2，D2 沒有站"]


def test_note_on_an_unresolved_place_is_written_without_a_place(conn):
    a = add_place(conn, 1, "浅草寺")
    p = parsed(places=[ref(1, "淺草寺"), ref(2, "餐廳")],
               place_notes=[PlaceNote("p2", "很好吃", [TimeTag(month="3")]), PlaceNote("p1", "人很多", [])])

    assert store.write_article(conn, ARTICLE, p, {"p1": a, "p2": None}) == []

    assert rows(conn, "SELECT poi_id, poi_name, content FROM place_note") == [(None, "餐廳", "很好吃"), (a, "淺草寺", "人很多")]
    assert rows(conn, "SELECT note_id, month FROM place_note_time") == [("1767020760-1", "3")]


def test_move_with_an_unresolved_end_is_written_without_a_place(conn):
    a = add_place(conn, 1, "成田空港")
    p = parsed(places=[ref(1, "成田機場"), ref(2, "飯店"), ref(3, "車站")],
               moves=[Move("p1", "p2", "搭 skyliner 到飯店約 1 小時", [TimeTag(time="18-4")]),
                      Move("p3", "p2", "走路五分鐘", [])])

    assert store.write_article(conn, ARTICLE, p, {"p1": a, "p2": None, "p3": None}) == []

    assert rows(conn, "SELECT from_id, to_id, from_name, to_name, content FROM move") == [
        (a, None, "成田機場", "飯店", "搭 skyliner 到飯店約 1 小時"), (None, None, "車站", "飯店", "走路五分鐘")]
    assert rows(conn, "SELECT * FROM place_note") == []   # 兩端都解析不到不算同一個地點
    assert rows(conn, "SELECT move_id, time FROM move_time") == [("1767020760-1", "18-4")]


def test_move_whose_ends_are_the_same_place_becomes_a_note(conn):
    a = add_place(conn, 1, "東京スカイツリー")
    p = parsed(places=[ref(1, "晴空塔"), ref(2, "天空樹")], moves=[Move("p1", "p2", "塔下面逛一圈", [])])

    store.write_article(conn, ARTICLE, p, {"p1": a, "p2": a})

    assert rows(conn, "SELECT * FROM move") == []
    assert rows(conn, "SELECT poi_id, poi_name, content FROM place_note") == [(a, "晴空塔", "塔下面逛一圈")]


def test_article_already_in_database_is_not_written_again(conn):
    a = add_place(conn, 1, "浅草寺")
    p = parsed(places=[ref(1, "淺草寺")], days=[Day("D1", "", stops("p1"))], place_notes=[PlaceNote("p1", "人很多", [])])
    assert not store.has_article(conn, ARTICLE.id)
    store.write_article(conn, ARTICLE, p, {"p1": a})

    assert store.write_article(conn, ARTICLE, p, {"p1": a}) is None

    assert rows(conn, "SELECT COUNT(*) FROM article") == [(1,)]
    assert rows(conn, "SELECT COUNT(*) FROM day_place") == [(1,)]
    assert rows(conn, "SELECT COUNT(*) FROM place_note") == [(1,)]


def test_article_is_written_whole_or_not_at_all(conn):
    a = add_place(conn, 1, "浅草寺")
    p = parsed(places=[ref(1, "淺草寺"), ref(2, "不存在")], days=[Day("D1", "", stops("p1"))],
               place_notes=[PlaceNote("p2", "指到資料庫沒有的 place", [])])

    try:
        store.write_article(conn, ARTICLE, p, {"p1": a, "p2": 999})
    except Exception:
        pass

    assert not store.has_article(conn, ARTICLE.id)
    assert rows(conn, "SELECT COUNT(*) FROM day") == [(0,)]


# ---------- 看資料用的 view ----------

def test_place_note_view_puts_the_times_of_a_note_in_one_array(conn):
    a = add_place(conn, 1, "浅草寺")
    p = parsed(places=[ref(1, "淺草寺"), ref(2, "築地場外市場")], days=[Day("D1", "", stops("p1"))],
               place_notes=[PlaceNote("p1", "早上人很少", [TimeTag("2025", "2", "平日", "5-10"), TimeTag(month="12")]),
                            PlaceNote("p2", "海鮮丼很讚", [])])
    store.write_article(conn, ARTICLE, p, {"p1": a, "p2": None})

    got = [(i, name, content, json.loads(times))
           for i, name, content, times in rows(conn, "SELECT id, poi_name, content, times FROM place_note_view ORDER BY id")]

    assert got == [
        ("1767020760-1", "浅草寺", "早上人很少", [{"year": 2025, "month": "2", "day_type": "平日", "time": "5-10"},
                                               {"year": None, "month": "12", "day_type": None, "time": None}]),
        ("1767020760-2", "築地場外市場", "海鮮丼很讚", []),   # 解析不到的用原文名稱；沒有時間是空陣列
    ]


def test_day_note_view_has_the_days_it_talks_about_with_their_stops(conn):
    a = add_place(conn, 1, "浅草寺")
    days = [Day(f"D{i}", "2", stops("p1", ("p2", "住宿"))) for i in range(1, 11)]
    p = parsed(places=[ref(1, "淺草寺"), ref(2, "飯店")], days=days,
               day_notes=[DayNote([10, 2], "這兩天很趕"), DayNote([], "整趟都在東京")])
    store.write_article(conn, ARTICLE, p, {"p1": a, "p2": None})

    got = {content: json.loads(d) for content, d in rows(conn, "SELECT content, days FROM day_note_view")}

    stops_ = [{"seq": 1, "poi_id": a, "name": "浅草寺", "type": "景點"},
              {"seq": 2, "poi_id": None, "name": "飯店", "type": "住宿"}]
    assert got == {
        "這兩天很趕": [{"day_id": "1767020760-2", "month": "2", "stops": stops_},     # 照第幾天排，不是照字串
                    {"day_id": "1767020760-10", "month": "2", "stops": stops_}],
        "整趟都在東京": [],
    }
