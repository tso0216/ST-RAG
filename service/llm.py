"""LLM 服務：送 system/user prompt，取回符合 JSON schema 的結果。

schema 為 None 時不限制輸出格式，回傳純文字。
tools 給工具名稱（例如 `["WebSearch", "WebFetch"]`）時開這些工具，而且直接執行不用確認；預設不開任何工具。

每次呼叫的用量會加進目前所有 `track()` 開著的 `Tally`，呼叫的人用它算一段流程總共花了多少 token。

LLM 呼叫只在這裡，之後要換成 `claude -p` 或正式 API 只改這個檔。
"""
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query


class LLMError(Exception):
    """LLM 回傳錯誤（包含額度用完），整批要停下。"""


@dataclass(frozen=True)
class Usage:
    """一次呼叫的用量。cost_usd 是 CLI 照 API 定價估的金額（走訂閱額度時不會真的收這筆）；duration_ms 是 API 花的時間。"""
    input_tokens: int = 0         # 含快取讀寫的 token
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    cache_read_tokens: int = 0    # input_tokens 裡從快取讀的（比一般輸入便宜很多）


@dataclass
class Tally:
    """好幾次呼叫的用量合計。"""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, usage: Usage) -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cost_usd += usage.cost_usd


_tallies: ContextVar[tuple[Tally, ...]] = ContextVar("llm_tallies", default=())


@contextmanager
def track(tally: Tally | None = None) -> Iterator[Tally]:
    """這個區塊裡（包含在裡面建立的 asyncio task）每次呼叫的用量都加進 tally；可以一層包一層，每一層都會加。"""
    tally = Tally() if tally is None else tally
    token = _tallies.set(_tallies.get() + (tally,))
    try:
        yield tally
    finally:
        _tallies.reset(token)


def record(usage: Usage) -> None:
    """把一次呼叫的用量加進目前開著的每個 `Tally`（假的 llm 也可以呼叫它）。"""
    for tally in _tallies.get():
        tally.add(usage)


async def call(
    system: str, user: str, schema: dict | None, model: str, effort: str | None = None,
    tools: list[str] | None = None,
) -> dict | str:
    """effort 給值時開 adaptive thinking，effort（low/medium/high/max）決定想多深；None 維持 CLI 預設。
    tools 是要開的工具；沒給或空的時候不開任何工具。"""
    out, _ = await call_with_usage(system, user, schema, model, effort, tools)
    return out


async def call_with_usage(
    system: str, user: str, schema: dict | None, model: str, effort: str | None = None,
    tools: list[str] | None = None,
) -> tuple[dict | str, int]:
    """同 `call`，另外回傳這次呼叫的輸入 token 數（含快取讀寫的 token；structured output 多輪時是各輪合計）。"""
    out, usage = await call_detailed(system, user, schema, model, effort, tools)
    return out, usage.input_tokens


async def call_detailed(
    system: str, user: str, schema: dict | None, model: str, effort: str | None = None,
    tools: list[str] | None = None,
) -> tuple[dict | str, Usage]:
    """同 `call`，另外回傳這次呼叫的用量（輸入／輸出 token、估計金額、API 時間）。"""
    os.environ.pop("ANTHROPIC_API_KEY", None)  # 走訂閱額度，不走 API 計費
    tools = list(tools or [])
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        tools=tools,
        **({"allowed_tools": tools} if tools else {}),   # 開了的工具直接執行，不用確認
        strict_mcp_config=True,
        setting_sources=[],
        output_format=(
            {"type": "json_schema", "schema": schema} if schema is not None else None
        ),
        verbatim_prompts=True,
        extra_args={"no-session-persistence": None},
        **({"thinking": {"type": "adaptive"}, "effort": effort} if effort else {}),
    )
    result = None
    try:
        async for msg in query(prompt=user, options=options):
            if isinstance(msg, ResultMessage):
                result = msg
    except Exception as e:
        raise LLMError(f"{type(e).__name__}: {e}") from e
    if result is None:
        raise LLMError("沒有收到結果")
    if result.is_error or (schema is not None and result.structured_output is None):
        detail = result.errors or result.result or result.subtype
        status = f" HTTP {result.api_error_status}" if result.api_error_status else ""
        raise LLMError(f"{result.subtype}{status}: {detail}")
    usage = result.usage or {}
    input_tokens = sum(usage.get(k) or 0 for k in
                       ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    spent = Usage(input_tokens, usage.get("output_tokens") or 0, result.total_cost_usd or 0.0,
                  result.duration_api_ms or 0, usage.get("cache_read_input_tokens") or 0)
    record(spent)
    return (result.result or "" if schema is None else result.structured_output), spent
