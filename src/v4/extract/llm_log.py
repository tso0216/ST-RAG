"""呼叫 llm，`--verbose` 時把輸入和輸出印出來。`v4.extract` 裡呼叫 llm 都經過這裡（`feedback`、`ask_place`）。

一次呼叫印兩段，用編號對起來（好幾個呼叫會同時進行，輸入和輸出中間可能夾著別的）：

    ━━━━━ llm #3 輸入：地點解析：挑候選｜prompts/pick.md ━━━━━
    [system] 跟 llm #1 一樣（地點解析：挑候選）
    [user]
    <item ref="p2">…</item>
    ━━━━━ llm #3 輸出（地點解析：挑候選：8 秒）━━━━━
    {"places": [...]}

system prompt 很長、每次都一樣，同一段只在第一次用到時印全文，之後寫「跟 llm #N 一樣」。
"""
import json
import logging
import time
from itertools import count

from service import llm

from . import config

log = logging.getLogger(__name__)

_numbers = count(1)
_first_seen: dict[str, tuple[int, str]] = {}   # system prompt → 第一次用到它的（編號，label）


async def call(system: str, user: str, schema: dict | None, label: str, prompt: str,
               tools: list[str] | None = None, *, model: str | None = None,
               effort: str | None = None) -> dict | str:
    """同 `service.llm.call`。model、effort 沒給時用 `config` 的（抽取都不給，問答系統給自己的）。
    label 是這次在做什麼、prompt 是 system prompt 的檔名（`prompts/` 裡的），都只用在印出來的標題。"""
    no = next(_numbers)
    if log.isEnabledFor(logging.INFO):
        log.info("%s", _input(no, system, user, label, prompt, tools))
    t0 = time.monotonic()
    out = await llm.call(system, user, schema, config.MODEL if model is None else model,
                         effort=config.EFFORT if effort is None else effort, tools=tools)
    if log.isEnabledFor(logging.INFO):
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1)
        log.info("━━━━━ llm #%d 輸出（%s：%.0f 秒）━━━━━\n%s", no, label, time.monotonic() - t0, text)
    return out


def _input(no: int, system: str, user: str, label: str, prompt: str, tools: list[str] | None) -> str:
    seen = _first_seen.setdefault(system, (no, label))
    head = (f"━━━━━ llm #{no} 輸入：{label}｜prompts/{prompt}"
            + (f"（工具：{'、'.join(tools)}）" if tools else "") + " ━━━━━")
    if seen[0] == no:
        sys_part = f"[system]\n{system}"
    else:
        sys_part = f"[system] 跟 llm #{seen[0]} 一樣（{seen[1]}）"
    return f"{head}\n{sys_part}\n[user]\n{user}"
