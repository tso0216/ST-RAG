"""各部分花的 token 和時間：llm 解析、地點解析的每一種呼叫、查本機地圖、國土地理院、寫入資料庫，各記一份。

- `collect()` 開一份記錄（`pipeline.run` 開，在裡面建立的 asyncio task 也記到同一份）；沒開時 `part()` 什麼都不記。
- `part(名稱)` 包住一段工作：這段裡的 llm 用量加進那個部分（`service.llm.track`），花的時間也加進去。
  只包最底層的工作（一次 llm 解析、一次問 llm、一次查地圖……），各部分不互相包含。
- `lines` 是結束時印的那一段：每個部分的 llm 次數、token、金額、時間，和佔全部的比例。

時間是各部分各自累計的。llm 解析和地點解析會同時進行（下一篇的 llm 解析在背景跑），
所以各部分加起來可能比整批花的時間長；時間佔比的分母是各部分的加總。
"""
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from service import llm

LINE = "線（第一次解析）"
NOTE = "點、段、day_note（第二次解析）"
PLAN = "地點解析：搜尋計畫"
PICK = "地點解析：挑候選"
WEB = "地點解析：上網查地址"
MAP = "查本機地圖"
GSI = "國土地理院"
WRITE = "寫入資料庫"
PARTS = (LINE, NOTE, PLAN, PICK, WEB, MAP, GSI, WRITE)
LLM_PARTS = (LINE, NOTE, PLAN, PICK, WEB)


@dataclass
class Part:
    tally: llm.Tally = field(default_factory=llm.Tally)
    seconds: float = 0.0


_board: ContextVar[dict[str, Part] | None] = ContextVar("usage_board", default=None)


@contextmanager
def collect() -> Iterator[dict[str, Part]]:
    """開一份記錄：部分名稱 → `Part`（`PARTS` 每個都有）。"""
    board = {name: Part() for name in PARTS}
    token = _board.set(board)
    try:
        yield board
    finally:
        _board.reset(token)


@contextmanager
def part(name: str) -> Iterator[None]:
    """這段工作的 llm 用量和時間記到 name 那個部分；沒有開 `collect()` 就不記。"""
    board = _board.get()
    if board is None:
        yield
        return
    started = time.monotonic()
    try:
        with llm.track(board[name].tally):
            yield
    finally:
        board[name].seconds += time.monotonic() - started


def lines(board: dict[str, Part], duration) -> list[str]:
    """結束時印的那一段。duration 把秒數變成文字（`pipeline.duration`）。"""
    tokens = {n: p.tally.input_tokens + p.tally.output_tokens for n, p in board.items()}
    all_tokens = sum(tokens.values())
    all_seconds = sum(p.seconds for p in board.values())

    def share(x: float, total: float) -> str:
        return f"{x / total:.0%}" if total else "0%"

    out = ["各部分："]
    for name in PARTS:
        p = board[name]
        spent = [f"時間 {duration(p.seconds)}（{share(p.seconds, all_seconds)}）"]
        if name in LLM_PARTS:
            spent.insert(0, f"llm {p.tally.calls} 次，token {tokens[name]:,}（{share(tokens[name], all_tokens)}），"
                            f"US${p.tally.cost_usd:.2f}")
        out.append(f"  {name}：{'，'.join(spent)}")
    return out
