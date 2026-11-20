"""llm 解析：線（主文的行程）：組提示詞、呼叫 llm、把精簡的輸出轉回後面用的格式。"""
import logging

from . import check, config, feedback
from .data_schema import LINE_SCHEMA, STOP_PREFIX, Article

log = logging.getLogger(__name__)


class ParseError(Exception):
    """第一次解析重試後還有問題，這篇報 error，整批停下。"""


def numbered(text: str) -> str:
    """主文每一行前面加上行號（`12｜`），llm 用行號指出地點在哪一行。"""
    return "\n".join(f"{i}｜{line}" for i, line in enumerate(text.splitlines(), 1))


def build_messages(article: Article) -> tuple[str, str]:
    system = (config.PROMPT_DIR / "line.md").read_text()
    user = (
        f"標題：{article.title}\n"
        f"發文日期：{article.posted_date}\n\n"
        f"主文：\n<text>\n{numbered(article.text)}\n</text>"
    )
    return system, user


def expand(out: dict, text: str) -> dict:
    """llm 的精簡輸出（照 `LINE_SCHEMA`、檢查過的）轉回後面用的格式：

    地點的 `line` 換成 `sentence`（那一行的原文），一站 `p3`／`h3` 換成 `{"ref": "p3", "type": 景點／住宿}`。
    """
    lines = text.splitlines()
    places = [{**{k: v for k, v in p.items() if k != "line"}, "sentence": lines[p["line"] - 1].strip()}
              for p in out["places"]]
    days = [{**d, "stops": [{"ref": f"p{s[1:]}", "type": STOP_PREFIX[s[0]]} for s in d["stops"]]}
            for d in out["days"]]
    return {"places": places, "days": days}


async def parse(article: Article) -> dict:
    """回傳檢查過、轉回 `sentence` 和 `{ref, type}` 的結果。重試後還有問題丟 `ParseError`。"""
    system, user = build_messages(article)
    lines = article.text.splitlines()
    out, problems = await feedback.ask(system, user, LINE_SCHEMA, lambda o: check.check_line(o, lines),
                                         label=f"{article.id} 第一次解析（線）", prompt="line.md")
    if problems:
        listing = "；".join(map(str, problems))
        raise ParseError(f"{article.id} 的行程解析重試 {config.RETRIES} 次後還有問題：{listing}")
    log.info("%s 第一次解析（線）：%d 個地點、%d 天", article.id, len(out["places"]), len(out["days"]))
    return expand(out, article.text)
