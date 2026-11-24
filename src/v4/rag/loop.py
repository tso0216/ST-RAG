"""一題的工具迴圈（spec 2.4–2.8）：把 `tools.TOOLS` 包成同一個行程裡的 MCP server，用 Agent SDK 跑到 LLM 不再呼叫工具。

- 每題最多呼叫工具 `config.TOOL_CALLS` 次（回錯誤的也算），之後每個工具都回錯誤，請 LLM 用查到的資料回答。
- 碰到回合上限（`error_max_turns`）時答案改成 `config.OVER_LIMIT_TEXT`。
- 工具的參數錯、外部服務出錯，都當成工具錯誤回給 LLM，迴圈繼續跑。
- 回合、判斷、工具呼叫從 SDK 的訊息串交給 `log.Log`。
"""
import inspect
import os
import time
from dataclasses import dataclass

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolAnnotations,
                              ToolResultBlock, ToolUseBlock, UserMessage, create_sdk_mcp_server, query, tool)

from service.llm import LLMError

from . import config
from .log import Log
from .tools import TOOLS, Env, ToolError, ToolSpec

SERVER = "db"
LIMIT_TEXT = "這一題的工具呼叫次數用完了，請用目前查到的資料回答。"
RESULT_CHARS = 200_000     # 工具回傳在這個長度內不會被 Claude Code 存成檔案（迴圈裡沒有讀檔的工具）
ANSWERED, MAX_TURNS = "答完", "碰到回合上限"


@dataclass
class Outcome:
    answer: str
    stop: str               # 答完／碰到回合上限
    calls: int
    turns: int              # 呼叫工具的回合數，跟 log 的「## 回合 N」同一套數法（不是 SDK 的 num_turns）
    input_tokens: int
    output_tokens: int
    cost_usd: float
    seconds: float


def full_name(name: str) -> str:
    return f"mcp__{SERVER}__{name}"


def _error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def build_server(env: Env, counter: list[int]):
    def wrap(spec: ToolSpec):
        async def handler(args: dict) -> dict:
            counter[0] += 1
            if counter[0] > config.TOOL_CALLS:
                return _error(LIMIT_TEXT)
            try:
                out = spec.run(env, args)
                if inspect.isawaitable(out):
                    out = await out
            except ToolError as e:
                return _error(str(e))
            except Exception as e:      # 外部服務（embedding、國土地理院、本機地圖、地點解析的 LLM）出錯
                return _error(f"外部服務出錯（{type(e).__name__}）：{e}")
            return {"content": [{"type": "text", "text": out}]}

        return tool(spec.name, spec.description, spec.schema,
                    annotations=ToolAnnotations(readOnlyHint=False, maxResultSizeChars=RESULT_CHARS))(handler)

    return create_sdk_mcp_server(name=SERVER, tools=[wrap(s) for s in TOOLS])


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


async def run(env: Env, system: str, first_message: str, log: Log, model: str | None = None) -> Outcome:
    """跑一題；`model` 沒給用 `config.MODEL`。LLM 出錯（碰到回合上限以外）丟 `LLMError`。"""
    os.environ.pop("ANTHROPIC_API_KEY", None)   # 走訂閱額度，跟 service/llm.py 一樣
    counter = [0]
    options = ClaudeAgentOptions(
        model=model or config.MODEL,
        system_prompt=system,
        tools=[],
        mcp_servers={SERVER: build_server(env, counter)},
        allowed_tools=[full_name(s.name) for s in TOOLS],
        max_turns=config.MAX_TURNS,
        strict_mcp_config=True,
        setting_sources=[],
        verbatim_prompts=True,
        extra_args={"no-session-persistence": None},
        thinking={"type": "adaptive"},
        effort=config.EFFORT,
    )
    started = time.monotonic()
    result: ResultMessage | None = None
    turn, no, new_turn, last_id = 0, 0, True, None
    tool_turns = 0                           # 最後一個呼叫工具的回合
    pending: list[str] = []                  # 這一回合還沒確定是判斷還是答案的文字
    uses: dict[str, tuple[int, int, str, dict]] = {}

    def flush() -> None:
        for text in pending:
            if text.strip():
                log.judgment(turn, text)
        pending.clear()

    try:
        async for msg in query(prompt=first_message, options=options):
            if isinstance(msg, AssistantMessage) and msg.parent_tool_use_id is None:
                # 同一則回應的 content block 可能分成好幾則訊息、中間夾著工具結果，有 message_id 時用它分回合
                if (msg.message_id != last_id) if msg.message_id else new_turn:
                    turn, new_turn, last_id = turn + 1, False, msg.message_id
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        pending.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        flush()
                        no += 1
                        tool_turns = turn
                        name = block.name.removeprefix(f"mcp__{SERVER}__")
                        uses[block.id] = (turn, no, name, block.input)
            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock) and block.tool_use_id in uses:
                        flush()
                        t, n, name, args = uses.pop(block.tool_use_id)
                        log.tool(t, n, name, args, _text(block.content), bool(block.is_error))
                        new_turn = True
            elif isinstance(msg, ResultMessage):
                result = msg
    except Exception as e:
        if result is None or result.subtype != "error_max_turns":
            raise LLMError(f"{type(e).__name__}: {e}") from e

    if result is None:
        raise LLMError("沒有收到結果")
    if result.subtype == "error_max_turns":
        flush()
        answer, stop = config.OVER_LIMIT_TEXT, MAX_TURNS
    elif result.is_error:
        detail = result.errors or result.result or result.subtype
        raise LLMError(f"{result.subtype}: {detail}")
    else:
        answer, stop = result.result or "\n".join(pending), ANSWERED
    usage = result.usage or {}
    input_tokens = sum(usage.get(k) or 0 for k in
                       ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return Outcome(answer, stop, counter[0], tool_turns, input_tokens, usage.get("output_tokens") or 0,
                   result.total_cost_usd or 0.0, time.monotonic() - started)
