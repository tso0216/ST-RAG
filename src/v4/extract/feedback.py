"""有問題時回報 llm 並附上問題：重試次數的控制，兩次 llm 解析共用。"""
import json
import logging
from collections.abc import Callable

from service import llm

from . import config
from .check import Problem

log = logging.getLogger(__name__)


async def ask(system: str, user: str, schema: dict,
              check: Callable[[dict], list[Problem]], label: str = "") -> tuple[dict, list[Problem]]:
    """呼叫 llm 並檢查輸出；有問題就把上一次的輸出和問題清單附在後面再問，最多重問 `config.RETRIES` 次。

    label 是這次在做什麼（哪一篇的哪一次解析），只用在 `--verbose` 印的訊息。
    回傳最後一次的輸出和它還有的問題（沒問題是空的）。llm 呼叫失敗時丟 `service.llm.LLMError`。
    """
    out = await llm.call(system, user, schema, config.MODEL, effort=config.EFFORT)
    problems = check(out)
    for attempt in range(1, config.RETRIES + 1):
        if not problems:
            break
        log.info("%s：檢查出 %d 個問題，回報 llm 重試第 %d 次（最多 %d 次）\n%s", label, len(problems), attempt,
                 config.RETRIES, "\n".join(f"    - {p}" for p in problems))
        out = await llm.call(system, user + _report(out, problems), schema, config.MODEL, effort=config.EFFORT)
        problems = check(out)
    if problems:
        log.info("%s：重試 %d 次後還有 %d 個問題\n%s", label, config.RETRIES, len(problems),
                 "\n".join(f"    - {p}" for p in problems))
    else:
        log.info("%s：檢查通過", label)
    return out, problems


def _report(out: dict, problems: list[Problem]) -> str:
    listing = "\n".join(f"- {p}" for p in problems)
    return (
        "\n\n你上一次的輸出：\n<previous>\n"
        f"{json.dumps(out, ensure_ascii=False, indent=1)}\n</previous>\n\n"
        f"程式檢查出這些問題（方括號裡是那一類的第幾筆，從 0 算起）：\n<problems>\n{listing}\n</problems>\n\n"
        "請修正這些問題，重新輸出完整的結果。沒有問題的部分照舊。"
    )
