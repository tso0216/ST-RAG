"""有問題時回報 llm 並附上問題：重試次數的控制，兩次 llm 解析共用。"""
import json
import logging
from collections.abc import Callable

from . import config, llm_log
from .check import Problem

log = logging.getLogger(__name__)


async def ask(system: str, user: str, schema: dict,
              check: Callable[[dict], list[Problem]], label: str, prompt: str, *,
              model: str | None = None, effort: str | None = None,
              retries: int | None = None) -> tuple[dict, list[Problem]]:
    """呼叫 llm 並檢查輸出；有問題就把上一次的輸出和問題清單附在後面再問，最多重問 `config.RETRIES` 次。

    label 是這次在做什麼（哪一篇的哪一次解析）、prompt 是 system prompt 的檔名，只用在 `--verbose` 印的訊息。
    model、effort、retries 沒給時用 `config` 的（抽取都不給；問答系統的拆題、整理重點給自己的）。
    回傳最後一次的輸出和它還有的問題（沒問題是空的）。llm 呼叫失敗時丟 `service.llm.LLMError`。
    """
    retries = config.RETRIES if retries is None else retries
    out = await llm_log.call(system, user, schema, label, prompt, model=model, effort=effort)
    problems = check(out)
    for attempt in range(1, retries + 1):
        if not problems:
            break
        log.info("%s：%d 個問題，重試 %d/%d\n%s", label, len(problems), attempt,
                 retries, "\n".join(f"    - {p}" for p in problems))
        out = await llm_log.call(system, user + report(out, problems), schema,
                                 f"{label}（重試第 {attempt} 次）", prompt, model=model, effort=effort)
        problems = check(out)
    if problems:
        log.info("%s：重試 %d 次後仍有 %d 個問題\n%s", label, retries, len(problems),
                 "\n".join(f"    - {p}" for p in problems))
    else:
        log.info("%s：檢查通過", label)
    return out, problems


def report(out: dict, problems: list) -> str:
    """附在原本的 user 訊息後面：上一次的輸出和問題清單（問題用 `str()` 印）。"""
    listing = "\n".join(f"- {p}" for p in problems)
    return (
        "\n\n你上一次的輸出：\n<previous>\n"
        f"{json.dumps(out, ensure_ascii=False, indent=1)}\n</previous>\n\n"
        f"程式檢查出這些問題（方括號裡是那一類的第幾筆，從 0 算起）：\n<problems>\n{listing}\n</problems>\n\n"
        "請修正這些問題，重新輸出完整的結果。沒有問題的部分照舊。"
    )
