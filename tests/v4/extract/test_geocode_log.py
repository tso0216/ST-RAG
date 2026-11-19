"""地點解析的 log 檔：每種 `result` 一行的欄位、中途出錯、結束時的統計。"""
import asyncio
import json
from datetime import datetime

import pytest

from service.gsi import GsiError
from service.llm import LLMError
from tests.v4.extract.fakes import FakeGsi, SENSOJI, plan, web
from v4 import place as place_table
from v4.extract import geocode_log
from v4.extract.data_schema import Context, Nearby, PlaceRef
from v4.extract.geocode_log import GeocodeLog
from v4.extract.geocoding import Geocoder

ARTICLE = "M.1.A.AAA"
FIELDS = ["article_id", "ref", "name_zh", "name_ja", "city", "kind", "located", "sentence", "same_day", "result",
          "fail_reason", "rounds", "web", "place", "llm_calls", "seconds"]
ROUND_FIELDS = ["round", "names", "area", "plan_reason", "area_error", "area_used", "widened", "skipped", "candidates",
                "choice", "pick_reason", "outcome"]
CANDIDATE_FIELDS = ["no", "source", "source_id", "name", "category", "pref", "municipality", "address", "lat", "lon",
                    "match", "similarity", "center_km", "nearest", "nearby"]


def ref(name_zh, name_ja, city="東京", located="是", kind="景點") -> PlaceRef:
    return PlaceRef("p1", name_zh, name_ja, city, kind, located, "", sentence=f"去{name_zh}")


@pytest.fixture
def log_one(conn, local_map, use_llm, tmp_path):
    """回傳函式：解析一個地點，回傳 log 檔裡它那一行（出錯時例外照樣往外丟，log 檔路徑放在 .path）。"""
    def run(p, gsi=None, same_day=(), **llm):
        use_llm(**llm)
        log = GeocodeLog.start(tmp_path / "logs")
        run.path = log.path
        geocoder = Geocoder(conn, local_map, gsi=gsi or FakeGsi(), log_file=log)
        try:
            asyncio.run(geocoder.resolve(p, Context(ARTICLE, same_day=list(same_day))))
        finally:
            log.close()
        [line] = geocode_log.read(log.path)
        return line
    return run


def test_found_in_round_one(log_one):
    line = log_one(ref("淺草寺", "浅草寺"), same_day=[Nearby("晴空塔", 35.7101, 139.8107), Nearby("還沒解析")])

    assert list(line) == FIELDS
    assert {k: line[k] for k in FIELDS[:8]} == {
        "article_id": ARTICLE, "ref": "p1", "name_zh": "淺草寺", "name_ja": "浅草寺", "city": "東京", "kind": "景點",
        "located": "是", "sentence": "去淺草寺"}
    assert line["same_day"] == [{"name": "晴空塔", "lat": 35.7101, "lon": 139.8107},
                                {"name": "還沒解析", "lat": None, "lon": None}]
    assert (line["result"], line["fail_reason"], line["web"], line["llm_calls"]) == ("地圖-第1輪", None, None, 2)
    assert isinstance(line["seconds"], float)
    [round_] = line["rounds"]
    assert list(round_) == ROUND_FIELDS
    assert {k: v for k, v in round_.items() if k != "candidates"} == {
        "round": 1, "names": ["浅草寺"], "area": {"pref": "東京"}, "plan_reason": "", "area_error": None,
        "area_used": {"pref": "東京都"}, "widened": False, "skipped": False, "choice": 1, "pick_reason": "",
        "outcome": "挑中"}
    first, second = round_["candidates"]
    assert list(first) == CANDIDATE_FIELDS
    assert first == {"no": 1, "source": "osm", "source_id": "way/1", "name": "浅草寺",
                     "category": "amenity=place_of_worship", "pref": "東京都", "municipality": "台東区",
                     "address": "浅草2-3-1", "lat": SENSOJI.lat, "lon": SENSOJI.lon, "match": "完全相同",
                     "similarity": 1.0, "center_km": None, "nearest": {"name": "晴空塔", "km": 1.37}, "nearby": 1}
    assert (second["no"], second["source"]) == (2, "overture")
    assert line["place"] == {"id": 1, "source": "osm", "source_id": "way/1", "name": "浅草寺",
                             "lat": SENSOJI.lat, "lon": SENSOJI.lon,
                             "google_maps": f"https://www.google.com/maps?q={SENSOJI.lat},{SENSOJI.lon}"}


def test_found_in_round_two(log_one):
    line = log_one(ref("晴空街道的某間店", "ソラマチのお店"),
                   plan=[plan("ソラマチのお店", pref="東京", reason="先查原名"), plan("東京ソラマチ", pref="東京")])

    assert line["result"] == "地圖-第2輪" and line["llm_calls"] == 3   # 搜尋計畫 2 次、挑 1 次
    first, second = line["rounds"]
    assert (first["candidates"], first["choice"], first["outcome"]) == ([], None, "沒有候選")
    assert (first["names"], first["plan_reason"]) == (["ソラマチのお店"], "先查原名")
    assert (second["names"], second["outcome"]) == (["東京ソラマチ"], "挑中")


def test_widened_area_and_distance_to_the_center(log_one):
    line = log_one(ref("淺草寺", "浅草寺"), plan=[plan("浅草寺", near=1, km=0.5)],
                   same_day=[Nearby("晴空塔", 35.7101, 139.8107)])

    [round_] = line["rounds"]
    assert round_["area"] == {"near": 1, "km": 0.5}
    assert round_["area_used"] == {"near": "晴空塔", "lat": 35.7101, "lon": 139.8107, "km": 1.5}
    assert round_["widened"] is True
    assert round_["candidates"][0]["center_km"] == 1.37


def test_area_that_cannot_be_used(log_one, monkeypatch):
    monkeypatch.setattr("v4.extract.config.RETRIES", 0)
    line = log_one(ref("淺草寺", "浅草寺"), plan=[plan("浅草寺", pref="大坂"), plan("浅草寺", pref="東京")])

    first, second = line["rounds"]
    assert (first["outcome"], first["area"], first["area_used"]) == ("範圍不能用", {"pref": "大坂"}, None)
    assert "沒有「大坂」這個都道府縣" in first["area_error"]
    assert second["outcome"] == "挑中"


def test_found_in_round_three_after_the_llm_picked_none(log_one):
    line = log_one(ref("淺草寺附近", "浅草寺"), pick=[0, 1],
                   plan=[plan("浅草寺", pref="東京"), plan("築地", pref="東京"), plan("スカイツリー", pref="東京")])

    assert line["result"] == "地圖-第3輪"
    assert [r["outcome"] for r in line["rounds"]] == ["AI 都不選", "沒有候選", "挑中"]
    assert line["rounds"][0]["choice"] == 0
    assert line["place"]["source_id"] == "way/2"


def test_found_on_the_web(log_one):
    line = log_one(ref("築地場外市場", "築地場外市場"), web=web("東京都中央区築地4-16-2", "https://www.tsukiji.or.jp/"))

    assert line["result"] == "上網" and line["fail_reason"] is None
    assert [r["outcome"] for r in line["rounds"]] == ["沒有候選", "名稱重複", "名稱重複"]
    assert line["rounds"][1]["skipped"] is True
    assert line["web"] == {"found": True, "address": "東京都中央区築地4-16-2", "source_url": "https://www.tsukiji.or.jp/",
                           "gsi_title": "東京都中央区築地四丁目", "lat": 35.6654, "lon": 139.7707}
    assert (line["place"]["source"], line["place"]["source_id"]) == ("gsi", "東京都中央区築地四丁目｜築地場外市場")
    assert line["llm_calls"] == 4   # 沒有候選所以沒問挑哪一筆；搜尋計畫 3 次、上網 1 次


@pytest.mark.parametrize("answer, reason", [
    (web("東京都中央区築地9-99"), "國土地理院查不到這個地址"),
    ({"found": False, "address": "", "source_url": ""}, "上網查不到地址"),
])
def test_dropped_when_the_web_fails(log_one, answer, reason):
    line = log_one(ref("築地場外市場", "築地場外市場"), web=answer)

    assert (line["result"], line["fail_reason"], line["place"]) == ("捨棄", reason, None)
    assert line["web"]["gsi_title"] is None


def test_database(conn, log_one):
    place_table.add(conn, source="osm", source_id="way/1", kind="景點", name_ja="浅草寺", name_zh="淺草寺",
                    city="東京", lat=SENSOJI.lat, lon=SENSOJI.lon)

    line = log_one(ref("淺草寺", "浅草寺"))

    assert (line["result"], line["rounds"], line["web"], line["llm_calls"]) == ("資料庫", [], None, 0)
    assert line["place"]["source_id"] == "way/1"


def test_place_that_cannot_be_guessed_is_dropped(log_one):
    line = log_one(ref("餐廳", "レストラン", located="猜不出來"))

    assert (line["result"], line["fail_reason"], line["place"], line["llm_calls"]) == ("捨棄", "猜不出來", None, 0)


def test_llm_error_writes_a_stopped_line_then_raises(log_one):
    def pick(user):
        raise LLMError("額度用完")

    with pytest.raises(LLMError):
        log_one(ref("淺草寺", "浅草寺"), pick=pick)

    [line] = geocode_log.read(log_one.path)
    assert line["result"] == "中斷" and "額度用完" in line["fail_reason"]
    assert line["rounds"][0]["outcome"] is None and line["llm_calls"] == 2


def test_gsi_error_writes_a_stopped_line_then_raises(log_one):
    with pytest.raises(GsiError):
        log_one(ref("築地場外市場", "築地場外市場"), web=web("東京都中央区築地4-16-2"),
                gsi=FakeGsi(fail=GsiError("國土地理院 HTTP 503")))

    [line] = geocode_log.read(log_one.path)
    assert line["result"] == "中斷" and "國土地理院 HTTP 503" in line["fail_reason"]
    assert line["web"]["address"] == "東京都中央区築地4-16-2"


# ---------- 檔案 ----------

def test_file_name_is_the_start_time_and_lines_are_flushed_as_written(tmp_path):
    log = GeocodeLog.start(tmp_path / "沒有的資料夾", datetime(2026, 11, 19, 8, 5, 3))
    log.write({"name_zh": "淺草寺"})

    assert log.path == tmp_path / "沒有的資料夾" / "geocode_20261119-080503.jsonl"
    assert log.path.read_text(encoding="utf-8") == '{"name_zh": "淺草寺"}\n'   # 還沒關也已經寫進去，中文不跳脫
    log.close()
    again = GeocodeLog.start(tmp_path / "沒有的資料夾", datetime(2026, 11, 19, 8, 5, 3))
    assert again.path.name == "geocode_20261119-080503-2.jsonl"
    again.close()


def test_summary_is_counted_from_the_log(tmp_path):
    path = tmp_path / "geocode.jsonl"
    rows = [
        {"result": "資料庫", "fail_reason": None, "rounds": []},
        {"result": "地圖-第1輪", "fail_reason": None, "rounds": [{"outcome": "挑中"}]},
        {"result": "地圖-第3輪", "fail_reason": None,
         "rounds": [{"outcome": "沒有候選"}, {"outcome": "AI 都不選"}, {"outcome": "挑中"}]},
        {"result": "上網", "fail_reason": None, "rounds": [{"outcome": "沒有候選"}] * 3},
        {"result": "捨棄", "fail_reason": "上網查不到地址", "rounds": [{"outcome": "AI 都不選"}] * 3},
        {"result": "捨棄", "fail_reason": "國土地理院查不到這個地址", "rounds": []},
        {"result": "捨棄", "fail_reason": "猜不出來", "rounds": []},
        {"result": "捨棄", "fail_reason": "猜不出來", "rounds": []},
    ]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    assert geocode_log.summary_lines(path, "data/logs/geocode_x.jsonl") == [
        "地點解析：共 8 個（log：data/logs/geocode_x.jsonl）",
        "  資料庫 1、地圖-第1輪 1、地圖-第2輪 0、地圖-第3輪 1、上網 1、捨棄 4",
        "  捨棄的原因：上網查不到地址 1、國土地理院查不到這個地址 1、猜不出來 2",
        "  本機地圖每一輪的失敗：沒有候選 4、AI 都不選 4",
    ]


def test_dropped_places_are_listed_from_the_log(tmp_path):
    path = tmp_path / "geocode.jsonl"
    rows = [
        {"article_id": "M.1", "name_zh": "淺草寺", "name_ja": "浅草寺", "result": "地圖-第1輪", "fail_reason": None},
        {"article_id": "M.1", "name_zh": "飯店", "name_ja": "ホテル", "result": "捨棄", "fail_reason": "猜不出來"},
        {"article_id": "M.2", "name_zh": "築地", "name_ja": "築地", "result": "捨棄", "fail_reason": "上網查不到地址"},
    ]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    assert geocode_log.dropped_lines(path) == [
        "捨棄的地點（2 個，用到它的那一天、place_note、move 都沒寫入）：",
        "  M.1 飯店（ホテル）：猜不出來",
        "  M.2 築地（築地）：上網查不到地址",
    ]
    path.write_text(json.dumps(rows[0], ensure_ascii=False) + "\n", encoding="utf-8")
    assert geocode_log.dropped_lines(path) == []
