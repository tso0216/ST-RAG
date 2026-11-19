"""`place`、`place_source` 和別名表：查正名、別名，新增 place，新增別名。"""
from v4 import place


def add_sensoji(conn, city="東京") -> int:
    return place.add(conn, source="osm", source_id="way/1", kind=place.SPOT, name_ja="浅草寺", name_zh="淺草寺",
                     city=city, lat=35.7148, lon=139.7967)


def test_find_by_official_name_in_either_language(conn):
    pid = add_sensoji(conn)

    assert place.find(conn, name_ja="浅草寺", city="東京") == pid
    assert place.find(conn, name_zh="淺草寺", city="東京") == pid
    assert place.find(conn, name_ja="沒有這個", name_zh="淺草寺", city="東京") == pid


def test_find_by_alias_in_either_language(conn):
    pid = add_sensoji(conn)
    place.add_alias(conn, pid, name_ja="金龍山浅草寺", name_zh="淺草觀音寺")

    assert place.find(conn, name_ja="金龍山浅草寺", city="東京") == pid
    assert place.find(conn, name_zh="淺草觀音寺", city="東京") == pid


def test_find_requires_the_same_city(conn):
    pid = add_sensoji(conn)
    place.add_alias(conn, pid, name_zh="淺草觀音寺")

    assert place.find(conn, name_ja="浅草寺", city="京都") is None
    assert place.find(conn, name_zh="淺草觀音寺", city="") is None


def test_find_returns_nothing_when_several_places_match(conn):
    add_sensoji(conn)
    other = place.add(conn, source="osm", source_id="way/2", kind=place.SPOT, name_ja="浅草神社", name_zh="淺草神社",
                      city="東京", lat=35.715, lon=139.797)
    place.add_alias(conn, other, name_zh="淺草寺")

    assert place.matches(conn, name_zh="淺草寺", city="東京") == [1, 2]
    assert place.find(conn, name_zh="淺草寺", city="東京") is None
    # 日文名稱和繁中名稱各對到不同的地點，也算查到多個
    assert place.find(conn, name_ja="浅草神社", name_zh="淺草寺", city="東京") is None


def test_names_not_given_are_not_searched(conn):
    add_sensoji(conn)

    assert place.find(conn, name_ja="別的名字", name_zh=None, city="東京") is None
    assert place.find(conn, city="東京") is None


def test_same_osm_object_is_not_added_twice(conn):
    first = add_sensoji(conn)

    again = place.add(conn, source="osm", source_id="way/1", kind=place.SPOT, name_ja="別的寫法", name_zh="別的寫法",
                      city="台東区", lat=1, lon=2)

    assert again == first
    assert conn.execute("SELECT name_ja, city FROM place").fetchall() == [("浅草寺", "東京")]
    assert conn.execute("SELECT place_id, source, source_id FROM place_source").fetchall() == [(first, "osm", "way/1")]


def test_place_and_its_source_are_written_together(conn):
    pid = add_sensoji(conn)

    assert place.by_source(conn, "osm", "way/1").id == pid
    assert place.source_of(conn, pid) == ("osm", "way/1")
    assert place.by_source(conn, "overture", "way/1") is None


def test_gsi_places_with_the_same_address_but_different_names_are_two_rows(conn):
    a = place.add(conn, source=place.GSI, source_id="東京都中央区築地四丁目｜すし大", kind=place.SPOT,
                  name_ja="すし大", name_zh=None, city="東京", lat=35.6654, lon=139.7707)
    b = place.add(conn, source=place.GSI, source_id="東京都中央区築地四丁目｜きつねや", kind=place.SPOT,
                  name_ja="きつねや", name_zh=None, city="東京", lat=35.6654, lon=139.7707)

    assert a != b
    assert conn.execute("SELECT COUNT(*) FROM place").fetchone() == (2,)


def test_alias_is_not_duplicated(conn):
    pid = add_sensoji(conn)

    for _ in range(2):
        place.add_alias(conn, pid, name_ja="金龍山浅草寺", name_zh="淺草觀音寺")

    assert conn.execute("SELECT alias FROM place_alias_ja").fetchall() == [("金龍山浅草寺",)]
    assert conn.execute("SELECT alias FROM place_alias_zh").fetchall() == [("淺草觀音寺",)]


def test_first_chinese_alias_becomes_display_name_when_missing(conn):
    pid = place.add(conn, source="overture", source_id="ovt-9", kind=place.SPOT, name_ja="焼肉きんぐ 浅草店", name_zh=None,
                    city="東京", lat=35.71, lon=139.79)

    place.add_alias(conn, pid, name_zh="燒肉 KING 淺草店")
    place.add_alias(conn, pid, name_zh="燒肉王淺草")

    assert place.get(conn, pid).name_zh == "燒肉 KING 淺草店"


def test_area_keeps_its_boundary(conn):
    boundary = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}

    pid = place.add(conn, source="osm", source_id="relation/100", kind=place.AREA, name_ja="上野", name_zh="上野",
                    city="東京", lat=35.71, lon=139.77, boundary=boundary)

    assert place.get(conn, pid).boundary == boundary
    assert place.by_source(conn, "osm", "relation/100").kind == place.AREA


def test_manual_places_use_the_coordinates_as_source_id(conn):
    a = place.add_manual(conn, name_ja="民宿A", name_zh="民宿 A", city="京都", lat=35.0, lon=135.7)
    b = place.add_manual(conn, name_ja="民宿B", name_zh=None, city="京都", lat=35.1, lon=135.8)
    same = place.add_manual(conn, name_ja="民宿A 別名", name_zh=None, city="京都", lat=35.0, lon=135.7)

    assert same == a and b != a
    assert [place.source_of(conn, x) for x in (a, b)] == [("manual", "35.0,135.7"), ("manual", "35.1,135.8")]
    assert {place.get(conn, x).kind for x in (a, b)} == {"景點"}
