"""地圖場景要跟工具實際回傳的一致：直接跑真的工具，再看畫出來的東西。"""
import json

import pytest

from v4 import db
from v4.rag import tools as T
from v4.web import visual

SQUARE = json.dumps({"type": "Polygon", "coordinates": [[[135.0, 35.0], [135.1, 35.0], [135.1, 35.1], [135.0, 35.1],
                                                         [135.0, 35.0]]]})


@pytest.fixture
def env():
    conn = db.connect(db.MEMORY)
    conn.execute("INSERT INTO article VALUES ('M.100.A.1', '京都行程', '2026-01-01T00:00:00')")
    conn.executemany("INSERT INTO place (id, kind, name_ja, name_zh, city, lat, lon, boundary) VALUES (?,?,?,?,?,?,?,?)", [
        (1, "區域", "京都", "京都區", "京都", 35.05, 135.05, SQUARE),
        (2, "景點", "清水寺", "清水寺", "京都", 35.02, 135.02, None),
        (3, "景點", "伏見稲荷", "伏見稻荷", "京都", 35.03, 135.03, None),
        (4, "景點", "東京駅", "東京車站", "東京", 35.68, 139.76, None),
    ])
    conn.executemany("INSERT INTO place_note VALUES (?, 'M.100.A.1', ?, ?, ?)", [
        ("100-1", 2, "清水寺", "早上人少"), ("100-2", 3, "伏見稻荷", "很多階梯"),
        ("100-3", None, "查不到的店", "沒有座標"),
    ])
    conn.execute("INSERT INTO move VALUES ('100-1', 'M.100.A.1', 2, 3, '清水寺', '伏見稻荷', '搭京阪')")
    conn.execute("INSERT INTO day VALUES ('100-1', 'M.100.A.1', '2')")
    conn.executemany("INSERT INTO day_place VALUES ('100-1', ?, ?, ?, ?)", [
        (1, 2, "清水寺", "景點"), (2, 3, "伏見稻荷", "住宿")])
    conn.execute("INSERT INTO day_note VALUES ('100-1', 'M.100.A.1', '排起來剛好')")
    conn.execute("INSERT INTO day_note_day VALUES ('100-1', '100-1')")
    conn.commit()
    return T.Env(conn=conn, article_id="q")


def scene(env, name, run, args):
    return visual.tool_scene(env, name, args, run(env, args), False)


def by_id(s):
    return {p["id"]: p for p in s["points"]}


def test_list_places_draws_only_the_returned_page(env):
    s = scene(env, "list_places", T.list_places, {"within": {"area": 1}, "kind": T.SPOT, "has": "不限", "limit": 1})
    points = by_id(s)
    assert len(set(points) & {2, 3}) == 1 and 4 not in points
    assert points[1]["main"] is True and s["areas"][0]["id"] == 1


def test_list_places_around_draws_radius(env):
    s = scene(env, "list_places", T.list_places, {"within": {"around": 2, "km": 3}})
    assert s["circles"][0]["km"] == 3
    assert by_id(s)[2]["main"] is True


def test_search_notes_only_draws_what_the_tool_returned(env):
    s = scene(env, "search_notes", T.search_notes, {"place": {"mode": "地點", "place_id": 1}, "limit": 1})
    assert len(s["evidence"]) == 1


def test_note_without_coordinates_stays_in_evidence_only(env):
    s = visual.answer_scene(env, "有一家店 [place_note 100-3]")
    assert s["evidence"]["place_note 100-3"]["title"] == "查不到的店" and not s["points"]


def test_search_moves_merges_both_directions_into_one_line(env):
    args = {"origin": {"mode": "地點", "place_id": 3}, "dest": {"mode": "地點", "place_id": 2}, "both_ways": True}
    s = scene(env, "search_moves", T.search_moves, args)
    assert len(s["lines"]) == 1 and s["lines"][0]["refs"] == ["move 100-1"]
    assert s["evidence"]["move 100-1"]["title"] == "清水寺 → 伏見稻荷"


def test_search_moves_without_hits_draws_no_line(env):
    args = {"origin": {"mode": "地點", "place_id": 4}, "dest": {"mode": "地點", "place_id": 2}}
    s = scene(env, "search_moves", T.search_moves, args)
    assert not s["lines"] and not s["evidence"] and set(by_id(s)) == {2, 4}


def test_measure_numbers_stops_and_tags_km(env):
    s = scene(env, "measure", T.measure, {"stops": [2, 3, 4]})
    assert [p["seq"] for p in s["points"]] == [[1], [2], [3]]
    assert all(l["kind"] == "leg" and l["tag"].endswith(" km") for l in s["lines"]) and len(s["lines"]) == 2


def test_search_days_draws_route(env):
    s = scene(env, "search_days", T.search_days, {"stops": [2]})
    assert s["lines"][0]["kind"] == "route" and s["lines"][0]["title"] == "第 1 天"
    assert by_id(s)[2]["main"] is True and by_id(s)[3]["main"] is False
    assert "day_note 100-1" in s["evidence"]


def test_answer_scene_reports_citations_not_in_database(env):
    s = visual.answer_scene(env, "人少 [place_note 100-1]，路線 [day 100-1]、評語 [day_note 100-1]，亂寫 [move 999-9]")
    assert s["missing"] == ["move 999-9"]
    assert set(s["evidence"]) == {"place_note 100-1", "day 100-1", "day_note 100-1"}
    assert "day_note 100-1" in s["lines"][0]["refs"]


def test_tool_error_draws_nothing(env):
    s = visual.tool_scene(env, "search_notes", {"place": {"mode": "地點", "place_id": 2}}, "參數錯了", True)
    assert not s["points"] and not s["evidence"]
