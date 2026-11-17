"""llm 解析：線（主文的行程）：組提示詞、呼叫 llm。"""
import logging

from . import check, config, feedback
from .data_schema import LINE_SCHEMA, Article

log = logging.getLogger(__name__)


class ParseError(Exception):
    """第一次解析重試後還有問題，這篇報 error，整批停下。"""


def build_messages(article: Article) -> tuple[str, str]:
    system = (config.PROMPT_DIR / "line.md").read_text()
    user = (
        f"標題：{article.title}\n"
        f"發文日期：{article.posted_date}\n\n"
        f"主文：\n<text>\n{article.text}\n</text>"
    )
    return system, user


async def parse(article: Article) -> dict:
    """回傳照 `LINE_SCHEMA`、檢查過的輸出。重試後還有問題丟 `ParseError`。"""
    system, user = build_messages(article)
    out, problems = await feedback.ask(system, user, LINE_SCHEMA, check.check_line, label=f"{article.id} 第一次解析（線）")
    if problems:
        listing = "；".join(map(str, problems))
        raise ParseError(f"{article.id} 的行程解析重試 {config.RETRIES} 次後還有問題：{listing}")
    log.info("%s 第一次解析（線）：%d 個地點、%d 天", article.id, len(out["places"]), len(out["days"]))
    return out
