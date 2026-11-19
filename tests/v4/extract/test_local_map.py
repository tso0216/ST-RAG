"""本機地圖：名稱正規化、模糊搜尋（比對結果、同城市、每個來源取幾筆）、用來源 id 查一筆。"""
import pytest

from tests.v4.extract.fakes import KIYOMIZU_KYOTO, KIYOMIZU_TOKYO, SENSOJI, Spot, build_map
from v4.extract.local_map import LocalMap, MissingLocalMap, city_key, normalize


@pytest.fixture
def small_map(tmp_path):
    """回傳函式：用給的幾筆建一個本機地圖並開啟。"""
    opened = []

    def build(*spots, **kw) -> LocalMap:
        m = LocalMap(build_map(tmp_path / f"map{len(opened)}.duckdb", spots), **kw)
        opened.append(m)
        return m
    yield build
    for m in opened:
        m.close()


# ---------- 名稱正規化 ----------

@pytest.mark.parametrize("raw, normalized", [
    ("ＡＢＣ　Ｃａｆé", "abccafé"),                 # NFKC、英文轉小寫、去掉全形空白
    ("アメ横 商店街", "アメ横商店街"),               # 去掉空白
    ("サンシャイン・シティ", "サンシャインシティ"),     # 去掉 ・
    ("A-1‐2－3", "a123"),                           # 去掉 - ‐ －
    ("スカイツリー", "スカイツリー"),                 # 長音保留
    ("霞ヶ関", "霞ケ関"),
    ("一ヵ所", "一カ所"),
    ("ｽｶｲﾂﾘｰ", "スカイツリー"),                      # 半形假名經 NFKC 變全形
])
def test_normalize(raw, normalized):
    assert normalize(raw) == normalized


@pytest.mark.parametrize("raw, key", [
    ("東京都", "東京"), ("京都府", "京都"), ("京都市", "京都"), ("台東区", "台東"), ("東京", "東京"),
    ("京都", "京都"),   # 去掉字尾只剩一個字時不去
    ("北区", "北区"), ("", ""),
])
def test_city_key_drops_one_administrative_suffix(raw, key):
    assert city_key(raw) == key


# ---------- 比對結果 ----------

def test_exact_match(local_map):
    found = local_map.search("浅草寺", "東京")

    assert [(c.source, c.source_id, c.match, c.similarity) for c in found] == [
        ("osm", "way/1", "完全相同", 1.0), ("overture", "ovt-sensoji", "完全相同", 1.0)]
    c = found[0]
    assert (c.name, c.category, c.kind, c.pref, c.municipality, c.address) == \
        ("浅草寺", "amenity=place_of_worship", "景點", "東京都", "台東区", "浅草2-3-1")
    assert (c.lat, c.lon, c.boundary) == (SENSOJI.lat, SENSOJI.lon, None)


def test_exact_match_on_another_name(local_map):
    [c] = local_map.search("スカイツリー")

    assert (c.name, c.match) == ("東京スカイツリー", "完全相同")


def test_name_on_the_map_containing_the_query_is_a_contains_match(local_map):
    found = local_map.search("東京スカイ")

    assert (found[0].source_id, found[0].match) == ("way/2", "包含")
    # 東京ソラマチ 只是開頭一樣，是模糊，排在包含後面
    assert [(c.source_id, c.match) for c in found[1:]] == [("way/4", "模糊")]


def test_similar_name_is_a_fuzzy_match(local_map):
    found = local_map.search("浅草寺院")

    assert [(c.source_id, c.match) for c in found] == [("way/1", "模糊"), ("ovt-sensoji", "模糊")]
    assert 0.8 <= found[0].similarity < 1


def test_query_containing_the_name_on_the_map_is_not_a_match(local_map):
    # 地圖上有「京都」，但只算「地圖上的名稱包含查詢」這個方向
    assert local_map.search("スターバックス京都三条大橋店", "京都") == []


def test_one_character_query_is_not_a_contains_match(small_map):
    m = small_map(SENSOJI)

    assert m.search("寺") == []


def test_names_are_normalized_before_matching(local_map):
    [c, _] = local_map.search("浅 草・寺")

    assert (c.source_id, c.match) == ("way/1", "完全相同")


def test_nothing_found(local_map):
    assert local_map.search("築地場外市場", "東京") == []
    assert local_map.search("") == []


def test_threshold_comes_from_the_setting(small_map):
    assert small_map(SENSOJI, threshold=0.99).search("浅草寺院") == []


# ---------- 排序 ----------

def test_same_city_comes_first(local_map):
    assert [c.source_id for c in local_map.search("清水寺", "東京")] == ["way/22", "way/21"]
    assert [c.source_id for c in local_map.search("清水寺", "京都")] == ["way/21", "way/22"]
    assert [c.source_id for c in local_map.search("清水寺", "京都市")] == ["way/21", "way/22"]


def test_tokyo_is_not_the_same_city_as_kyoto(small_map):
    # 東京的排在前面（id 小），城市寫「京都」時只有京都府的算同城市
    m = small_map(KIYOMIZU_TOKYO, KIYOMIZU_KYOTO)

    assert [c.pref for c in m.search("清水寺", "京都")] == ["京都府", "東京都"]
    assert [c.pref for c in m.search("清水寺", "")] == ["東京都", "京都府"]


def test_better_match_then_higher_similarity_then_shorter_name(small_map):
    m = small_map(
        Spot("osm", "node/1", "浅草寺の裏の長い名前", "東京都", "台東区", 35.7, 139.7),
        Spot("osm", "node/2", "浅草寺の裏", "東京都", "台東区", 35.7, 139.7),
        Spot("osm", "node/3", "浅草神社", "東京都", "台東区", 35.7, 139.7),
        Spot("osm", "node/4", "浅草寺", "東京都", "台東区", 35.7, 139.7),
    )

    assert [(c.source_id, c.match) for c in m.search("浅草寺")] == [
        ("node/4", "完全相同"), ("node/2", "包含"), ("node/1", "包含"), ("node/3", "模糊")]


def test_each_source_keeps_its_top_five(small_map):
    spots = [Spot(source, f"{source}-{i}", "同名" + "x" * i, "東京都", "台東区", 35.7, 139.7)
             for source in ("osm", "overture") for i in range(8)]
    m = small_map(*spots)

    found = m.search("同名")

    assert len(found) == 10
    assert sorted(c.source_id for c in found) == sorted(
        [f"osm-{i}" for i in range(5)] + [f"overture-{i}" for i in range(5)])
    # 合起來照同樣的順序排：完全相同的兩筆在最前面，之後名稱短的在前
    assert [c.source_id for c in found[:4]] == ["osm-0", "overture-0", "osm-1", "overture-1"]


def test_same_place_from_both_sources_is_not_merged(local_map):
    assert {c.source for c in local_map.search("浅草寺")} == {"osm", "overture"}


# ---------- 用來源 id 查一筆 ----------

def test_lookup_by_source_id(local_map):
    ueno = local_map.lookup("osm", "relation/100")

    assert (ueno.name, ueno.kind, ueno.match) == ("上野", "區域", "")
    assert ueno.boundary["type"] == "Polygon"
    assert local_map.lookup("osm", "way/999") is None
    assert local_map.lookup("overture", "way/1") is None


def test_missing_map_file(tmp_path):
    with pytest.raises(MissingLocalMap, match="scripts/geo/build_japan.py"):
        LocalMap(tmp_path / "沒有.duckdb")
