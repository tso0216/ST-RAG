"""② 分流（規格 9）：依題型分三條路。無法處理不呼叫 LLM，回固定文字。"""
from . import config
from .data_schema import OTHER, ROUTE_FIXED, ROUTE_GENERAL, ROUTE_RAG, UNANSWERABLE


def route(qtype: str) -> str:
    """無法處理 → 固定文字；其他 → 跑 ③ 到 ⑦，最後用 general.md 回答；點、段、行程 → 跑 ③ 到 ⑦，用 answer.md。"""
    if qtype == UNANSWERABLE:
        return ROUTE_FIXED
    if qtype == OTHER:
        return ROUTE_GENERAL
    return ROUTE_RAG


def fixed_answer() -> str:
    return config.UNANSWERABLE_TEXT
