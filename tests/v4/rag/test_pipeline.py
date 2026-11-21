"""主流程：一篇發問文從拆題到合併答案。LLM、JEV、embedding、國土地理院都是假的；本機地圖是小型的。"""
import asyncio

import pytest

from tests.v4.extract import fakes as extract_fakes
from tests.v4.extract.fakes import SENSOJI, FakeGsi
from tests.v4.rag import fakes
from tests.v4.rag.fakes import RELEVANT, FakeJEV, at, new, nodes, point, ref
from v4 import place as place_table
from v4.extract.data_schema import Article
from v4.rag import config, pipeline
from v4.rag.data_schema import ROUTE_FIXED, ROUTE_GENERAL, ROUTE_RAG

ARTICLE = Article(id="M.1767013758.A.A9D", title="[問題] 淺草", author="poster", posted_at="2025-10-01T10:00:00",
                  text="D1 淺草寺 → 晴空塔\n淺草寺早上人多嗎？", comments=[])

LINE = extract_fakes.line(places=[extract_fakes.line_place("p1", "淺草寺", "浅草寺")],
                          days=[extract_fakes.day("D1", ["p1"], month="10")])


def split(*questions):
    return {"questions": [{"text": t, "type": q} for t, q in questions]}


@pytest.fixture
def temple(world, conn):
    """淺草寺（跟小型本機地圖的同一個來源 id，地點解析會用到同一列），有一筆相關的經驗。"""
    pid = place_table.add(conn, source=SENSOJI.source, source_id=SENSOJI.source_id, kind="景點", name_ja="浅草寺",
                          name_zh="淺草寺", city="台東区", lat=SENSOJI.lat, lon=SENSOJI.lon)
    a = world.article(1)
    note = world.note(a, pid, f"早上人很少{RELEVANT}", times=[(None, "10", None, "8")])
    return pid, note


@pytest.fixture
def ask(conn, local_map):
    """回傳 ask(llm, jev=None)：跑一篇，回傳 `Result`。"""
    def run(llm, jev=None):
        services = pipeline.Services(conn, local_map, jev or FakeJEV(), fakes.FakeEmbedder(), gsi=FakeGsi())
        return asyncio.run(pipeline.run_article(ARTICLE, services, db_path=":memory:"))
    return run


def rag_llm(use_llm, **kw):
    return use_llm(extract=extract_fakes.FakeLLM(line=LINE), **kw)


def test_unanswerable_calls_no_llm_after_split(ask, use_llm):
    llm = rag_llm(use_llm, split=split(("幫我訂機票", "無法處理")))
    r = ask(llm)
    assert r.error is None
    assert llm.kinds() == ["split"]
    assert r.answer == f"### 第 1 題：幫我訂機票\n\n{config.UNANSWERABLE_TEXT}"
    q = r.record["每一題"][0]
    assert q["走哪一條路"] == ROUTE_FIXED
    assert r.record["拆題與解析"]["行程解析"]["做了嗎"] is False


def test_other_question_runs_plan_and_answers_with_general(ask, use_llm, temple):
    llm = rag_llm(use_llm, split=split(("淺草寺可以用西瓜卡嗎", "其他")),
                  plan=nodes(point(at(new("淺草寺", "浅草寺", city="東京")), aspect="付款")))
    r = ask(llm)
    assert r.error is None
    assert llm.kinds().count("plan") == 1 and llm.kinds().count("replan") == 1
    assert "general" in llm.kinds() and "answer" not in llm.kinds()
    assert llm.extract.users("line") == []                       # 只有其他題：不解析行程
    q = r.record["每一題"][0]
    assert q["走哪一條路"] == ROUTE_GENERAL
    assert r.answer.endswith("一般知識的答案")
    assert q["每一輪"][0]["節點"][0]["結果"]               # 節點有跑


def test_point_question_with_evidence_ends_after_one_round(ask, use_llm, temple):
    pid, note = temple
    llm = rag_llm(use_llm, split=split(("淺草寺早上人多嗎", "點")), plan=nodes(point(at(ref("p1")), months=(10,))))
    r = ask(llm)
    assert r.error is None
    q = r.record["每一題"][0]
    assert q["走哪一條路"] == ROUTE_RAG
    assert q["為什麼停"] == pipeline.STOP_EMPTY
    assert [x["第幾輪"] for x in q["每一輪"]] == [0, 1]
    assert llm.kinds().count("replan") == 1
    assert llm.kinds()[-1] == "answer"
    assert f"place_note {note}" in llm.users("answer")[0]
    assert r.answer == "### 第 1 題：淺草寺早上人多嗎\n\n答案"
    assert r.record["拆題與解析"]["行程解析"]["行程裡的站的解析"][0]["place_id"] == pid


def test_plan_output_checked_and_geocoded_before_running(ask, use_llm, temple):
    jev = FakeJEV()
    llm = rag_llm(use_llm, split=split(("淺草寺早上人多嗎", "點")),
                  plan=nodes(point(at(new("謎之店", "なぞの店"))), point(at(ref("p1")))))
    r = ask(llm, jev)
    first = r.record["每一題"][0]["每一輪"][0]
    assert [(d["原因"], d["說明"]) for d in first["計畫"]["拿掉的節點"]] == [("地點解析不到", "地點解析不到：謎之店")]
    assert len(first["節點"]) == 1 and len(jev.passages) == 1
    assert [row["狀態"] for row in first["查詢狀態表"]] == ["地點解析不到", "有（1 筆）"]   # 照 ③ 輸出的順序


def test_split_type_still_invalid_becomes_other(ask, use_llm):
    llm = rag_llm(use_llm, split=split(("淺草寺可以刷卡嗎", "亂寫")))
    r = ask(llm)
    assert llm.kinds().count("split") == 1 + config.RETRIES
    q = r.record["每一題"][0]
    assert q["題型"] == "其他" and q["走哪一條路"] == ROUTE_GENERAL


def test_replan_stops_after_two_rounds(ask, use_llm, temple):
    llm = rag_llm(use_llm, split=split(("淺草寺人多嗎", "點")), plan=nodes(point(at(ref("p1")))),
                  replan=[nodes(point(at(ref("p1")), aspect="排隊")), nodes(point(at(ref("p1")), aspect="景色"))])
    r = ask(llm)
    q = r.record["每一題"][0]
    assert q["為什麼停"] == pipeline.STOP_ROUNDS
    assert llm.kinds().count("replan") == 2
    assert [x["第幾輪"] for x in q["每一輪"]] == [0, 1, 2]


def test_no_evidence_answer_gets_opening(ask, use_llm, temple):
    llm = rag_llm(use_llm, split=split(("晴空塔人多嗎", "點")),
                  plan=nodes(point(at(ref("p1")), months=(3,))))
    r = ask(llm)
    assert r.answer.endswith(f"{config.NO_EVIDENCE_OPENING}。\n\n答案")


def test_questions_merged_in_order(ask, use_llm, temple):
    llm = rag_llm(use_llm, split=split(("淺草寺人多嗎", "點"), ("幫我訂機票", "無法處理")),
                  plan=nodes(point(at(ref("p1")))))
    r = ask(llm)
    assert r.answer == (f"### 第 1 題：淺草寺人多嗎\n\n答案\n\n"
                        f"### 第 2 題：幫我訂機票\n\n{config.UNANSWERABLE_TEXT}")
    assert len(llm.extract.users("line")) == 1


def test_jev_error_stops_the_article(ask, use_llm, temple):
    llm = rag_llm(use_llm, split=split(("淺草寺人多嗎", "點")), plan=nodes(point(at(ref("p1")))))
    r = ask(llm, FakeJEV(fail=fakes.JEVFail("壞了")))
    assert r.answer == "" and "壞了" in r.error and r.record["錯誤"] == r.error
    assert "answer" not in llm.kinds()
    assert r.record["用量"]["LLM"]["次數"] >= 2
