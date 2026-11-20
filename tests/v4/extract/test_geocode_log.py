"""地點解析的 log 檔：每個地點記了什麼、寫成什麼樣子、中途出錯、結束時的統計。"""
import asyncio
import logging
from datetime import datetime

import pytest

from service.gsi import GsiError
from service.llm import LLMError
from tests.v4.extract.fakes import FakeGsi, SENSOJI, plan, stops, web
from v4 import place as place_table
from v4.extract import geocode_log
from v4.extract.data_schema import Context, Day, Nearby, Parsed, PlaceRef
from v4.extract.geocode_log import GeocodeLog, Trace
from v4.extract.geocoding import Geocoder

ARTICLE = "M.1.A.AAA"


def ref(name_zh, name_ja, city="東京", located="是", kind="景點", ref="p1") -> PlaceRef:
    return PlaceRef(ref, name_zh, name_ja, city, kind, located, "", sentence=f"去{name_zh}")


@pytest.fixture
def log_one(conn, local_map, use_llm, tmp_path):
    """回傳函式：解析一個地點，回傳（它的 Trace, 這篇 log 檔的內容）。出錯時例外照樣往外丟，log 放在 .log。"""
    def run(p, gsi=None, same_day=(), **llm):
        use_llm(**llm)
        log = GeocodeLog.start(tmp_path / "logs")
        run.log = log
        geocoder = Geocoder(conn, local_map, gsi=gsi or FakeGsi(), log_file=log)
        asyncio.run(geocoder.resolve(p, Context(ARTICLE, same_day=list(same_day))))
        [trace] = log.traces
        return trace, log.article_path(ARTICLE).read_text(encoding="utf-8")
    return run


def test_found_in_round_one(log_one):
    trace, text = log_one(ref("淺草寺", "浅草寺"), same_day=[Nearby("晴空塔", 35.7101, 139.8107), Nearby("還沒解析")])

    assert (trace.result, trace.fail_reason, trace.web, trace.llm_calls) == ("地圖-第1輪", None, None, 2)
    [round_] = trace.rounds
    assert {k: v for k, v in round_.items() if k != "candidates"} == {
        "round": 1, "names": ["浅草寺"], "area": "東京", "plan_reason": "", "area_error": None,
        "area_used": "東京都", "widened": False, "choice": 1, "pick_reason": "", "outcome": "挑中"}
    assert trace.used == {"id": 1, "source": "osm", "source_id": "way/1", "name": "浅草寺",
                          "lat": SENSOJI.lat, "lon": SENSOJI.lon,
                          "google_maps": f"https://www.google.com/maps?q={SENSOJI.lat},{SENSOJI.lon}"}
    assert text == f"""========== {ARTICLE} ==========

一批 1 個：淺草寺（是）
[p1] 淺草寺｜浅草寺｜東京｜景點｜是　→　地圖-第1輪：浅草寺
  句子：去淺草寺
  同一天：晴空塔（35.7101, 139.8107）、還沒解析（還沒定位）
  第 1 輪：名稱 浅草寺｜範圍 東京｜挑中
    實際查：東京都，2 筆候選
    → 1. 浅草寺｜OSM｜amenity=place_of_worship｜台東区 浅草2-3-1｜離「晴空塔」1.4 公里｜35.7148, 139.7967｜way/1｜完全相同｜重要度 0.00
      2. 浅草寺｜Overture｜buddhist_temple｜台東区 東京都台東区浅草2-3-1｜離「晴空塔」1.4 公里｜35.7148, 139.7966｜ovt-sensoji｜完全相同｜重要度 0.00
    AI 挑第 1 筆：
  用：place 1 浅草寺（osm way/1）https://www.google.com/maps?q={SENSOJI.lat},{SENSOJI.lon}
  llm：2 次

"""


def test_found_in_round_two(log_one):
    trace, text = log_one(ref("晴空街道的某間店", "ソラマチのお店"),
                          plan=[plan("ソラマチのお店", pref="東京", reason="先查原名"), plan("東京ソラマチ", pref="東京")])

    assert trace.result == "地圖-第2輪" and trace.llm_calls == 3   # 搜尋計畫 2 次、挑 1 次
    first, second = trace.rounds
    assert (first["candidates"], first["choice"], first["outcome"]) == ([], None, "沒有候選")
    assert (first["names"], first["plan_reason"]) == (["ソラマチのお店"], "先查原名")
    assert (second["names"], second["outcome"]) == (["東京ソラマチ"], "挑中")
    assert "  第 1 輪：名稱 ソラマチのお店｜範圍 東京｜沒有候選\n    理由：先查原名\n    實際查：東京都，0 筆候選\n" in text


def test_widened_area_and_distance_to_the_center(log_one):
    trace, text = log_one(ref("淺草寺", "浅草寺"), plan=[plan("浅草寺", near=1, km=0.5)],
                          same_day=[Nearby("晴空塔", 35.7101, 139.8107)])

    [round_] = trace.rounds
    assert round_["area"] == "同一天第 1 個「晴空塔」方圓 0.5 公里"
    assert round_["area_used"] == "「晴空塔」方圓 1.5 公里" and round_["widened"] is True
    assert "離中心 1.4 公里" in round_["candidates"][0]
    assert "實際查：「晴空塔」方圓 1.5 公里（放寬），2 筆候選" in text


def test_area_that_cannot_be_used(log_one, monkeypatch):
    monkeypatch.setattr("v4.extract.config.RETRIES", 0)
    trace, text = log_one(ref("淺草寺", "浅草寺"), plan=[plan("浅草寺", pref="大坂"), plan("浅草寺", pref="東京")])

    first, second = trace.rounds
    assert (first["outcome"], first["area"], first["area_used"]) == ("範圍不能用", "大坂", None)
    assert "沒有「大坂」這個都道府縣" in first["area_error"]
    assert second["outcome"] == "挑中"
    assert "    範圍不能用：沒有「大坂」這個都道府縣" in text


def test_found_in_round_two_after_the_llm_picked_none(log_one):
    trace, text = log_one(ref("淺草寺附近", "浅草寺"), pick=[0, 1],
                          plan=[plan("浅草寺", pref="東京"), plan("スカイツリー", pref="東京")])

    assert trace.result == "地圖-第2輪"
    assert [r["outcome"] for r in trace.rounds] == ["AI 都不選", "挑中"]
    assert trace.rounds[0]["choice"] == 0 and "    AI 都不選：" in text
    assert trace.used["source_id"] == "way/2"


def test_found_on_the_web(log_one):
    trace, text = log_one(ref("築地場外市場", "築地場外市場"), web=web("東京都中央区築地4-16-2", "https://www.tsukiji.or.jp/"))

    assert trace.result == "上網" and trace.fail_reason is None
    assert [r["outcome"] for r in trace.rounds] == ["沒有候選", "名稱重複"]
    assert trace.web == {"found": True, "address": "東京都中央区築地4-16-2", "source_url": "https://www.tsukiji.or.jp/",
                         "gsi_title": "東京都中央区築地四丁目", "lat": 35.6654, "lon": 139.7707}
    assert (trace.used["source"], trace.used["source_id"]) == ("gsi", "東京都中央区築地四丁目｜築地場外市場")
    assert trace.llm_calls == 3   # 沒有候選所以沒問挑哪一筆；搜尋計畫 2 次、上網 1 次
    assert "  上網查地址：東京都中央区築地4-16-2（https://www.tsukiji.or.jp/）\n" in text
    assert "  國土地理院：東京都中央区築地四丁目（35.6654, 139.7707）\n" in text


@pytest.mark.parametrize("answer, reason", [
    (web("東京都中央区築地9-99"), "國土地理院查不到這個地址"),
    ({"found": False, "address": "", "source_url": ""}, "上網查不到地址"),
])
def test_dropped_when_the_web_fails(log_one, answer, reason):
    trace, text = log_one(ref("築地場外市場", "築地場外市場"), web=answer)

    assert (trace.result, trace.fail_reason, trace.used) == ("解析不到", reason, None)
    assert trace.web["gsi_title"] is None
    assert f"　→　解析不到（{reason}）" in text


def test_database(conn, log_one):
    place_table.add(conn, source="osm", source_id="way/1", kind="景點", name_ja="浅草寺", name_zh="淺草寺",
                    city="東京", lat=SENSOJI.lat, lon=SENSOJI.lon)

    trace, text = log_one(ref("淺草寺", "浅草寺"))

    assert (trace.result, trace.rounds, trace.web, trace.llm_calls) == ("資料庫", [], None, 0)
    assert trace.used["source_id"] == "way/1"
    assert "　→　資料庫：浅草寺" in text and "  llm：0 次" in text


def test_place_that_cannot_be_guessed_is_dropped(log_one):
    trace, text = log_one(ref("餐廳", "レストラン", located="猜不出來"))

    assert (trace.result, trace.fail_reason, trace.used, trace.llm_calls) == ("解析不到", "猜不出來", None, 0)
    assert "一批" not in text   # 沒有要問 llm 的就不寫


def test_llm_error_writes_stopped_then_raises(log_one):
    def pick(user):
        raise LLMError("額度用完")

    with pytest.raises(LLMError):
        log_one(ref("淺草寺", "浅草寺"), pick=pick)

    [trace] = log_one.log.traces
    assert trace.result == "中斷" and "額度用完" in trace.fail_reason
    assert trace.rounds[0]["outcome"] is None and trace.llm_calls == 2
    assert "  第 1 輪：名稱 浅草寺｜範圍 東京｜（還沒做完）" in log_one.log.article_path(ARTICLE).read_text(encoding="utf-8")


def test_gsi_error_writes_stopped_then_raises(log_one):
    with pytest.raises(GsiError):
        log_one(ref("築地場外市場", "築地場外市場"), web=web("東京都中央区築地4-16-2"),
                gsi=FakeGsi(fail=GsiError("國土地理院 HTTP 503")))

    [trace] = log_one.log.traces
    assert trace.result == "中斷" and "國土地理院 HTTP 503" in trace.fail_reason
    assert trace.web["address"] == "東京都中央区築地4-16-2"


def test_each_article_gets_its_own_file_with_batch_headings(conn, local_map, use_llm, tmp_path):
    use_llm(pick=1)
    log = GeocodeLog.start(tmp_path)
    geocoder = Geocoder(conn, local_map, gsi=FakeGsi(), log_file=log)
    parsed = Parsed(places=[ref("淺草寺", "浅草寺", ref="p1"), ref("晴空塔", "スカイツリー", ref="p2"),
                            ref("燒肉 KING", "焼肉きんぐ", located="猜的", ref="p3")],
                    days=[Day("D1", "2", stops("p1", "p2", "p3"))], place_notes=[], moves=[], day_notes=[])

    asyncio.run(geocoder.resolve_article("M.1", parsed))
    asyncio.run(geocoder.resolve_article("M.2", Parsed(places=[ref("淺草寺", "浅草寺")], days=[Day("D1", "2", stops("p1"))],
                                                       place_notes=[], moves=[], day_notes=[])))

    def headings(article_id):
        return [line for line in log.article_path(article_id).read_text(encoding="utf-8").splitlines()
                if line.startswith(("==========", "一批", "["))]
    assert headings("M.1") == [
        "========== M.1 ==========",
        "一批 2 個：淺草寺、晴空塔（是）",
        "[p1] 淺草寺｜浅草寺｜東京｜景點｜是　→　地圖-第1輪：浅草寺",
        "[p2] 晴空塔｜スカイツリー｜東京｜景點｜是　→　地圖-第1輪：東京スカイツリー",
        "一批 1 個：燒肉 KING（猜的）",
        "[p3] 燒肉 KING｜焼肉きんぐ｜東京｜景點｜猜的　→　地圖-第1輪：焼肉きんぐ 浅草店",
    ]
    assert headings("M.2") == [
        "========== M.2 ==========",
        "一批 1 個：淺草寺（是）",
        "[p1] 淺草寺｜浅草寺｜東京｜景點｜是　→　資料庫：浅草寺",
    ]
    assert log.path.read_text(encoding="utf-8") == ""   # 地點的段落不寫進 _run.log


# ---------- 檔案 ----------

def test_folder_name_is_the_start_time_and_text_is_written_right_away(tmp_path):
    log = GeocodeLog.start(tmp_path / "沒有的資料夾", datetime(2026, 11, 19, 8, 5, 3))
    log.batch("M.1", [ref("淺草寺", "浅草寺")])

    assert log.folder == tmp_path / "沒有的資料夾" / "extract_20261119-080503"
    assert log.path == log.folder / "_run.log" and log.path.exists()
    assert "一批 1 個：淺草寺（是）" in log.article_path("M.1").read_text(encoding="utf-8")
    again = GeocodeLog.start(tmp_path / "沒有的資料夾", datetime(2026, 11, 19, 8, 5, 3))
    assert again.folder.name == "extract_20261119-080503-2"


def test_lines_go_to_the_article_being_worked_on_even_when_tasks_interleave(tmp_path):
    log = GeocodeLog.start(tmp_path)
    logger = logging.getLogger("v4.test_log")
    logger.setLevel(logging.INFO)
    handler = log.handler()
    logger.addHandler(handler)

    async def work(article_id):
        with log.article(article_id):
            for i in range(3):
                logger.info("%s 第 %d 步", article_id, i)
                await asyncio.sleep(0)   # 讓另一篇插進來

    async def both():
        await asyncio.gather(work("M.1"), work("M.2"))

    try:
        asyncio.run(both())
        logger.info("不在任何一篇裡")
        log.progress("M.1", "[1/2] M.1 寫入")
    finally:
        logger.removeHandler(handler)

    for article_id in ("M.1", "M.2"):
        lines = log.article_path(article_id).read_text(encoding="utf-8").splitlines()
        assert [l for l in lines if "第" in l] == [f"{article_id} 第 {i} 步" for i in range(3)]
    assert log.path.read_text(encoding="utf-8") == "不在任何一篇裡\n[1/2] M.1 寫入\n"
    assert log.article_path("M.1").read_text(encoding="utf-8").endswith("[1/2] M.1 寫入\n")


# ---------- 統計 ----------

def trace(result, fail_reason=None, outcomes=(), name="淺草寺", article="M.1") -> Trace:
    t = Trace(ref(name, name), Context(article), result=result, fail_reason=fail_reason)
    t.rounds = [{"outcome": o} for o in outcomes]
    return t


def test_summary_is_counted_from_the_traces():
    traces = [
        trace("資料庫"),
        trace("地圖-第1輪", outcomes=["挑中"]),
        trace("地圖-第2輪", outcomes=["沒有候選", "挑中"]),
        trace("上網", outcomes=["沒有候選", "AI 都不選"]),
        trace("解析不到", "上網查不到地址", outcomes=["AI 都不選", "名稱重複"]),
        trace("解析不到", "國土地理院查不到這個地址"),
        trace("解析不到", "猜不出來"),
        trace("解析不到", "猜不出來"),
    ]

    assert geocode_log.summary_lines(traces) == [
        "地點解析：共 8 個",
        "  資料庫 1、地圖-第1輪 1、地圖-第2輪 1、上網 1、解析不到 4",
        "  解析不到的原因：上網查不到地址 1、國土地理院查不到這個地址 1、猜不出來 2",
        "  本機地圖每輪失敗：沒有候選 2、AI 都不選 2、名稱重複 1",
    ]


def test_dropped_places_are_listed():
    traces = [trace("地圖-第1輪"), trace("解析不到", "猜不出來", name="飯店"),
              trace("解析不到", "上網查不到地址", name="築地", article="M.2")]

    assert geocode_log.dropped_lines(traces) == [
        "解析不到的地點（2 個）：",
        "  M.1 飯店（飯店）：猜不出來",
        "  M.2 築地（築地）：上網查不到地址",
    ]
    assert geocode_log.dropped_lines(traces[:1]) == []
