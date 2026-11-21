"""流程紀錄（`ask --log`）：主檔白話寫每一輪查了什麼、找到什麼；細節檔是 llm 的完整輸入和輸出。用假的 LLM、JEV 跑主流程。"""
import logging
from pathlib import Path

from tests.v4.rag import fakes
from tests.v4.rag.fakes import FakeJEV, at, new, nodes, point, ref
from tests.v4.rag.test_pipeline import ask, rag_llm, split, temple  # noqa: F401（fixture）
from v4.rag import flowlog


def run_logged(ask, llm, jev=None):  # noqa: F811
    with flowlog.Capture() as capture:
        r = ask(llm, jev)
    return r, flowlog.render(r.record, "a.detail.log"), flowlog.render_detail(r.record, capture.text())


def in_order(text: str, *parts: str) -> bool:
    at_ = -1
    for p in parts:
        at_ = text.find(p, at_ + 1)
        if at_ < 0:
            return False
    return True


def test_main_file_tells_the_story(ask, use_llm, temple):  # noqa: F811
    llm = rag_llm(use_llm, split=split(("淺草寺早上人多嗎", "點")), plan=nodes(point(at(ref("p1")), months=(10,))))
    r, text, detail = run_logged(ask, llm)
    assert r.error is None
    assert in_order(text, "文章：[問題] 淺草", "題目：淺草寺早上人多嗎（點）", "提到的地點：淺草寺", "你的行程：",
                    "── 第 1 輪：先查 ──", "要查：", "N1 淺草寺的經驗｜時間：月份 10", "想知道：", "為什麼：",
                    "結果：", "N1 ✓ 找到 1 筆", "── 第 2 輪：補查 ──", "LLM 認為不用再查了",
                    "停下來：", "回答用到：N1", "答案：", "花費：", "a.detail.log")
    assert "llm #" not in text                              # llm 的輸入輸出只在細節檔
    assert in_order(detail, "━━ 參數 ━━", "━━ 每一步的 LLM 用量", "━━ 每一步的細節", "llm #")


def test_why_nothing_was_found(ask, use_llm, temple):  # noqa: F811
    llm = rag_llm(use_llm, split=split(("晴空塔人多嗎", "點")), plan=nodes(point(at(ref("p1")), months=(3,))))
    _, text, _ = run_logged(ask, llm)
    assert "N1 ✗ 有 1 筆，但時間都對不上" in text and "回答用到：沒有；沒找到：N1" in text


def test_dropped_node_and_unresolved_place(ask, use_llm, temple):  # noqa: F811
    llm = rag_llm(use_llm, split=split(("淺草寺早上人多嗎", "點")),
                  plan=nodes(point(at(new("謎之店", "なぞの店"))), point(at(ref("p1")))))
    _, text, _ = run_logged(ask, llm)
    assert "謎之店 → 找不到" in text and "✗ 不查：" in text


def test_error_is_written_and_stops_there(ask, use_llm, temple):  # noqa: F811
    llm = rag_llm(use_llm, split=split(("淺草寺人多嗎", "點")), plan=nodes(point(at(ref("p1")))))
    r, text, _ = run_logged(ask, llm, FakeJEV(fail=fakes.JEVFail("壞了")))
    assert r.error
    assert in_order(text, "── 第 1 輪：先查 ──", "✗ 出錯，停在這裡：", "壞了", "花費：")
    assert "答案：" not in text


def test_unanswerable_goes_straight_to_the_answer(ask, use_llm):  # noqa: F811
    llm = rag_llm(use_llm, split=split(("幫我訂機票", "無法處理")))
    _, text, _ = run_logged(ask, llm)
    assert "提到的地點" not in text and "第 1 輪" not in text
    assert in_order(text, "━━ 第 1 題：幫我訂機票 ━━", "答案：")


def test_write_makes_both_files(tmp_path: Path):
    path = tmp_path / "logs" / "ask.log"
    flowlog.write(path, {"輸入": {"問題文字": "淺草寺人多嗎"}, "參數": {"模型": "m"}}, "llm #1 輸入")
    assert flowlog.detail_path(path) == tmp_path / "logs" / "ask.detail.log"
    assert "ask.detail.log" in path.read_text(encoding="utf-8")
    assert "llm #1 輸入" in flowlog.detail_path(path).read_text(encoding="utf-8")


def test_capture_restores_the_logger():
    logger = logging.getLogger("v4")
    before = logger.level
    with flowlog.Capture() as c:
        logging.getLogger("v4.rag.x").info("看得到")
    logging.getLogger("v4.rag.x").info("出來後不收")
    assert c.lines == ["看得到"] and logger.level == before and c not in logger.handlers
