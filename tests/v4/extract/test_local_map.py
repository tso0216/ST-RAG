"""本機地圖：名稱正規化、模糊搜尋（比對結果、好幾個名稱、範圍、同城市、每個來源取幾筆）、縣市對到地圖的寫法、用來源 id 查一筆。"""
import pytest

from tests.v4.extract.fakes import SENSOJI, Spot, build_map
from v4.extract import config
from v4.extract.data_schema import Area
from v4.extract.local_map import AreaError, LocalMap, MissingLocalMap, city_key, normalize


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
    found = local_map.search("浅草寺")

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

    # 東京ソラマチ 只是開頭一樣，是模糊；有包含的就不列模糊的
    assert [(c.source_id, c.match) for c in found] == [("way/2", "包含")]


def test_fuzzy_matches_are_listed_only_when_nothing_has_the_same_name(small_map):
    m = small_map(SENSOJI, Spot("osm", "node/9", "浅草寺院", "東京都", "台東区", 35.7, 139.7))

    assert [(c.source_id, c.match) for c in m.search("浅草寺")] == [("way/1", "完全相同"), ("node/9", "包含")]
    assert [(c.source_id, c.match) for c in m.search("浅草寺の")] == [("way/1", "模糊"), ("node/9", "模糊")]


def test_similar_name_is_a_fuzzy_match(local_map):
    found = local_map.search("浅草寺院")

    assert [(c.source_id, c.match) for c in found] == [("way/1", "模糊"), ("ovt-sensoji", "模糊")]
    assert 0.8 <= found[0].similarity < 1


def test_query_containing_the_name_on_the_map_is_not_a_match(local_map):
    # 地圖上有「京都」，但只算「地圖上的名稱包含查詢」這個方向
    assert local_map.search("スターバックス京都三条大橋店") == []


def test_one_character_query_is_not_a_contains_match(small_map):
    m = small_map(SENSOJI)

    assert m.search("寺") == []


def test_names_are_normalized_before_matching(local_map):
    [c, _] = local_map.search("浅 草・寺")

    assert (c.source_id, c.match) == ("way/1", "完全相同")


def test_nothing_found(local_map):
    assert local_map.search("築地場外市場") == []
    assert local_map.search("") == []


def test_threshold_comes_from_the_setting(small_map):
    assert small_map(SENSOJI, threshold=0.99).search("浅草寺院") == []


# ---------- 排序 ----------

def test_better_match_then_higher_similarity_then_shorter_name(small_map):
    m = small_map(
        Spot("osm", "node/1", "浅草寺の裏の長い名前", "東京都", "台東区", 35.7, 139.7),
        Spot("osm", "node/2", "浅草寺の裏", "東京都", "台東区", 35.7, 139.7),
        Spot("osm", "node/3", "浅草神社", "東京都", "台東区", 35.7, 139.7),
        Spot("osm", "node/4", "浅草寺", "東京都", "台東区", 35.7, 139.7),
    )

    assert [(c.source_id, c.match) for c in m.search("浅草寺")] == [
        ("node/4", "完全相同"), ("node/2", "包含"), ("node/1", "包含")]
    assert [(c.source_id, c.match) for c in m.search("浅草寺社")] == [
        ("node/4", "模糊"), ("node/3", "模糊"), ("node/2", "模糊")]


def test_each_source_keeps_its_top_n(small_map):
    spots = [Spot(source, f"{source}-{i}", "同名" + "x" * i, "東京都", "台東区", 35.7, 139.7)
             for source in ("osm", "overture") for i in range(8)]
    m = small_map(*spots, per_source=5)

    found = m.search("同名")

    assert len(found) == 10
    assert sorted(c.source_id for c in found) == sorted(
        [f"osm-{i}" for i in range(5)] + [f"overture-{i}" for i in range(5)])
    # 合起來照同樣的順序排：完全相同的兩筆在最前面，之後名稱短的在前
    assert [c.source_id for c in found[:4]] == ["osm-0", "overture-0", "osm-1", "overture-1"]


def test_top_n_comes_from_the_setting(small_map, monkeypatch):
    monkeypatch.setattr(config, "CANDIDATES_PER_SOURCE", 3)
    m = small_map(*[Spot("osm", f"osm-{i}", "同名" + "x" * i, "東京都", "台東区", 35.7, 139.7) for i in range(5)])

    assert len(m.search("同名")) == 3


def test_same_place_from_both_sources_is_not_merged(local_map):
    assert {c.source for c in local_map.search("浅草寺")} == {"osm", "overture"}


# ---------- 好幾個名稱 ----------

def test_several_names_are_searched_together_and_each_place_takes_its_best_match(local_map):
    found = local_map.search(["スカイツリー", "浅草寺", "東京ソラ"])

    assert [(c.source_id, c.match) for c in found] == [
        ("way/1", "完全相同"), ("ovt-sensoji", "完全相同"), ("way/2", "完全相同"), ("way/4", "包含")]


def test_blank_names_are_ignored(local_map):
    assert [c.source_id for c in local_map.search(["", " ", "浅草寺"])] == ["way/1", "ovt-sensoji"]
    assert local_map.search(["", " "]) == []


# ---------- 範圍 ----------

def test_prefecture_area_leaves_out_other_prefectures(local_map):
    assert [c.source_id for c in local_map.search("清水寺", area=Area(pref="京都府"))] == ["way/21"]
    assert [c.source_id for c in local_map.search("清水寺", area=Area(pref="東京都"))] == ["way/22"]


def test_municipality_area(local_map):
    taito = Area(pref="東京都", municipality="台東区")
    sumida = Area(pref="東京都", municipality="墨田区")

    assert [c.source_id for c in local_map.search("浅草寺", area=taito)] == ["way/1", "ovt-sensoji"]
    assert local_map.search("浅草寺", area=sumida) == []


def test_circle_area_keeps_places_within_the_radius_and_puts_nearer_ones_first(small_map):
    near = Spot("overture", "near", "焼肉きんぐ", "東京都", "台東区", 35.7160, 139.7967)    # 浅草寺北邊約 130 公尺
    far = Spot("overture", "far", "焼肉きんぐ", "東京都", "台東区", 35.7238, 139.7967)      # 約 1 公里
    away = Spot("overture", "away", "焼肉きんぐ", "東京都", "新宿区", 35.6900, 139.7000)   # 9 公里
    m = small_map(far, away, near)
    around = Area(anchor="淺草寺", lat=SENSOJI.lat, lon=SENSOJI.lon, km=0.5)

    assert [c.source_id for c in m.search("焼肉きんぐ", area=around)] == ["near"]
    wider = Area(anchor="淺草寺", lat=SENSOJI.lat, lon=SENSOJI.lon, km=2)
    assert [c.source_id for c in m.search("焼肉きんぐ", area=wider)] == ["near", "far"]


def test_no_area_is_all_of_japan(local_map):
    assert len(local_map.search("清水寺", area=Area())) == 2


# ---------- 縣市對到地圖的寫法 ----------

@pytest.fixture
def admin_map(small_map):
    return small_map(
        Spot("osm", "n/1", "a", "東京都", "台東区", 35.7, 139.7),
        Spot("osm", "n/2", "a", "東京都", "府中市", 35.6, 139.4),
        Spot("osm", "n/3", "a", "広島県", "府中市", 34.5, 133.2),
        Spot("osm", "n/4", "a", "新潟県", "湯沢町", 36.9, 138.8),
        Spot("osm", "n/5", "a", "秋田県", "湯沢市", 39.1, 140.5),
        Spot("osm", "n/6", "a", "京都府", "京都市", 35.0, 135.7),
        Spot("osm", "n/7", "a", "埼玉県", "さいたま市", 35.9, 139.6),
    )


@pytest.mark.parametrize("pref, municipality, area", [
    ("東京", "", Area(pref="東京都")),
    ("東京都", "台東区", Area(pref="東京都", municipality="台東区")),
    ("", "台東区", Area(pref="東京都", municipality="台東区")),
    ("", "台東", Area(pref="東京都", municipality="台東区")),          # 去掉字尾也對得上
    ("京都", "京都", Area(pref="京都府", municipality="京都市")),
    ("", "湯沢町", Area(pref="新潟県", municipality="湯沢町")),       # 完全相同的優先，不會對到秋田的湯沢市
    ("", "湯沢市", Area(pref="秋田県", municipality="湯沢市")),
    ("広島", "府中市", Area(pref="広島県", municipality="府中市")),
    (" 東京 ", "台東 区", Area(pref="東京都", municipality="台東区")),  # 空白不算
    ("", "", Area()),
])
def test_admin_area_is_matched_to_how_the_map_writes_it(admin_map, pref, municipality, area):
    assert admin_map.admin_area(pref, municipality) == area


@pytest.mark.parametrize("pref, municipality, message", [
    ("大坂", "", "沒有「大坂」這個都道府縣"),
    ("埼玉県", "大宮区", "「埼玉県」裡沒有「大宮区」這個市區町村"),
    ("埼玉県", "大宮区", "政令指定都市只到「市」"),
    ("", "府中市", "「府中市」有好幾個：広島県 府中市、東京都 府中市。請寫上都道府縣（pref）"),
    ("東京", "湯沢町", "「東京都」裡沒有「湯沢町」"),
])
def test_admin_area_that_cannot_be_matched_tells_why(admin_map, pref, municipality, message):
    with pytest.raises(AreaError, match=message):
        admin_map.admin_area(pref, municipality)


def test_unmatched_municipality_lists_similar_ones(admin_map):
    with pytest.raises(AreaError, match="名稱相近的：湯沢市、湯沢町"):
        admin_map.admin_area("", "越後湯沢町")


def test_missing_map_file(tmp_path):
    with pytest.raises(MissingLocalMap, match="scripts/geo/build_japan.py"):
        LocalMap(tmp_path / "沒有.duckdb")
