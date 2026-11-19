"""寫入資料庫。"""
from v4 import place as place_table
from v4.extract import store
from v4.extract.data_schema import Article, Day, DayNote, Move, Parsed, PlaceNote, PlaceRef, TimeTag

ARTICLE = Article("M.1.A.AAA", "[問題] 東京五天行程", "poster", "2025-12-29T21:09:16", "主文", [])


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
        days=[Day("D1", "2", ["p1", "p2"]), Day("D2", "", ["p2"])],
        place_notes=[PlaceNote("p1", "早上人很少", [TimeTag("2025", "2", "平日", "5-10"), TimeTag(month="12")])],
        moves=[Move("p1", "p2", "走過去 20 分鐘", [TimeTag(time="18-19")])],
        day_notes=[DayNote("D1", "這天排太趕"), DayNote("", "整趟都在東京東邊")])

    assert store.write_article(conn, ARTICLE, p, {"p1": a, "p2": b}) == 0

    assert rows(conn, "SELECT id, title, posted_at FROM article") == [
        ("M.1.A.AAA", "[問題] 東京五天行程", "2025-12-29T21:09:16")]
    assert rows(conn, "SELECT id, article_id, month FROM day") == [(1, "M.1.A.AAA", "2"), (2, "M.1.A.AAA", None)]
    assert rows(conn, "SELECT day_id, seq, poi_id FROM day_place ORDER BY day_id, seq") == [(1, 1, a), (1, 2, b), (2, 1, b)]
    assert rows(conn, "SELECT article_id, poi_id, poi_name, content FROM place_note") == [
        ("M.1.A.AAA", a, "淺草寺", "早上人很少")]
    assert rows(conn, "SELECT note_id, year, month, day_type, time FROM place_note_time") == [
        (1, 2025, "2", "平日", "5-10"), (1, None, "12", None, None)]
    assert rows(conn, "SELECT article_id, from_id, to_id, from_name, to_name, content FROM move") == [
        ("M.1.A.AAA", a, b, "淺草寺", "晴空塔", "走過去 20 分鐘")]
    assert rows(conn, "SELECT move_id, year, month, day_type, time FROM move_time") == [
        (1, None, None, None, "18-19")]
    assert rows(conn, "SELECT day_id, content FROM day_note") == [
        (1, "這天排太趕"), (None, "整趟都在東京東邊")]


def test_blank_fields_are_stored_as_null_and_an_all_blank_time_is_not_written(conn):
    a, b = add_place(conn, 1, "浅草寺"), add_place(conn, 2, "東京スカイツリー")
    p = parsed(places=[ref(1, "淺草寺"), ref(2, "晴空塔")],
               place_notes=[PlaceNote("p1", "很好逛", [TimeTag()])],
               moves=[Move("p1", "p2", "很近", [])])

    store.write_article(conn, ARTICLE, p, {"p1": a, "p2": b})

    assert rows(conn, "SELECT * FROM place_note_time") == [] and rows(conn, "SELECT * FROM move_time") == []


def test_stop_left_out_is_skipped_and_seq_keeps_its_position_in_the_article(conn):
    a, c = add_place(conn, 1, "浅草寺"), add_place(conn, 3, "上野")
    p = parsed(places=[ref(1, "淺草寺"), ref(2, "餐廳"), ref(3, "上野")], days=[Day("D1", "", ["p1", "p2", "p3"])])

    dropped = store.write_article(conn, ARTICLE, p, {"p1": a, "p2": None, "p3": c})

    assert rows(conn, "SELECT seq, poi_id FROM day_place ORDER BY seq") == [(1, a), (3, c)]
    assert dropped == 1


def test_day_with_no_stops_left_is_not_written_and_neither_are_its_notes(conn):
    a = add_place(conn, 1, "浅草寺")
    p = parsed(places=[ref(1, "淺草寺"), ref(2, "餐廳")],
               days=[Day("D1", "", ["p2"]), Day("D2", "", ["p1"]), Day("D3", "", [])],
               day_notes=[DayNote("D1", "這天只有吃飯"), DayNote("D2", "這天去淺草"),
                          DayNote("D3", "這天還沒排"), DayNote("", "整趟的建議")])

    dropped = store.write_article(conn, ARTICLE, p, {"p1": a, "p2": None})

    assert rows(conn, "SELECT id FROM day") == [(1,)]
    assert rows(conn, "SELECT day_id, content FROM day_note") == [(1, "這天去淺草"), (None, "整趟的建議")]
    assert dropped == 3   # 一站、兩則 day_note


def test_note_on_a_place_left_out_is_not_written(conn):
    a = add_place(conn, 1, "浅草寺")
    p = parsed(places=[ref(1, "淺草寺"), ref(2, "餐廳")],
               place_notes=[PlaceNote("p2", "很好吃", []), PlaceNote("p1", "人很多", [])])

    dropped = store.write_article(conn, ARTICLE, p, {"p1": a, "p2": None})

    assert rows(conn, "SELECT poi_id, content FROM place_note") == [(a, "人很多")]
    assert dropped == 1


def test_move_with_one_end_left_out_becomes_a_note_on_the_other_end(conn):
    a = add_place(conn, 1, "成田空港")
    p = parsed(places=[ref(1, "成田機場"), ref(2, "飯店")],
               moves=[Move("p1", "p2", "搭 skyliner 到飯店約 1 小時", [TimeTag(time="18-4")]),
                      Move("p2", "p1", "回程搭利木津巴士", [])])

    dropped = store.write_article(conn, ARTICLE, p, {"p1": a})   # p2 不在裡面，也是不收入

    assert rows(conn, "SELECT * FROM move") == []
    assert rows(conn, "SELECT poi_id, poi_name, content FROM place_note") == [
        (a, "成田機場", "搭 skyliner 到飯店約 1 小時"), (a, "成田機場", "回程搭利木津巴士")]
    assert rows(conn, "SELECT note_id, time FROM place_note_time") == [(1, "18-4")]
    assert dropped == 0


def test_move_with_both_ends_left_out_is_not_written(conn):
    p = parsed(places=[ref(1, "車站"), ref(2, "飯店")], moves=[Move("p1", "p2", "走路五分鐘", [])])

    dropped = store.write_article(conn, ARTICLE, p, {"p1": None, "p2": None})

    assert rows(conn, "SELECT * FROM move") == [] and rows(conn, "SELECT * FROM place_note") == []
    assert dropped == 1
    assert store.has_article(conn, ARTICLE.id)


def test_move_whose_ends_are_the_same_place_becomes_a_note(conn):
    a = add_place(conn, 1, "東京スカイツリー")
    p = parsed(places=[ref(1, "晴空塔"), ref(2, "天空樹")], moves=[Move("p1", "p2", "塔下面逛一圈", [])])

    store.write_article(conn, ARTICLE, p, {"p1": a, "p2": a})

    assert rows(conn, "SELECT * FROM move") == []
    assert rows(conn, "SELECT poi_id, poi_name, content FROM place_note") == [(a, "晴空塔", "塔下面逛一圈")]


def test_article_already_in_database_is_not_written_again(conn):
    a = add_place(conn, 1, "浅草寺")
    p = parsed(places=[ref(1, "淺草寺")], days=[Day("D1", "", ["p1"])], place_notes=[PlaceNote("p1", "人很多", [])])
    assert not store.has_article(conn, ARTICLE.id)
    store.write_article(conn, ARTICLE, p, {"p1": a})

    assert store.write_article(conn, ARTICLE, p, {"p1": a}) is None

    assert rows(conn, "SELECT COUNT(*) FROM article") == [(1,)]
    assert rows(conn, "SELECT COUNT(*) FROM day_place") == [(1,)]
    assert rows(conn, "SELECT COUNT(*) FROM place_note") == [(1,)]


def test_article_is_written_whole_or_not_at_all(conn):
    a = add_place(conn, 1, "浅草寺")
    p = parsed(places=[ref(1, "淺草寺"), ref(2, "不存在")], days=[Day("D1", "", ["p1"])],
               place_notes=[PlaceNote("p2", "指到資料庫沒有的 place", [])])

    try:
        store.write_article(conn, ARTICLE, p, {"p1": a, "p2": 999})
    except Exception:
        pass

    assert not store.has_article(conn, ARTICLE.id)
    assert rows(conn, "SELECT COUNT(*) FROM day") == [(0,)]
