"""拆題：把一篇發問文拆成幾個單獨看得懂的問句（spec 2.2）。"""
import json
import time
from dataclasses import dataclass, field

from service import llm
from v4.extract.data_schema import Article

from . import config

SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {
        "type": "object", "properties": {"text": {"type": "string"}},
        "required": ["text"], "additionalProperties": False}}},
    "required": ["questions"],
    "additionalProperties": False,
}


@dataclass
class Split:
    questions: list[str]
    retries: list[str] = field(default_factory=list)   # 每次重問時的問題
    tally: llm.Tally = field(default_factory=llm.Tally)
    seconds: float = 0.0


def user_message(article: Article) -> str:
    return f"標題：{article.title}\n發文日期：{article.posted_date}\n\n主文：\n{article.text}"


def problems_of(out: dict, question_only: bool = False) -> list[str]:
    questions = out.get("questions") or []
    if not questions and not question_only:
        return ["至少要有一題"]
    return [f"第 {i} 題問句是空的" for i, q in enumerate(questions, 1) if not (q.get("text") or "").strip()]


async def split(article: Article, question_only: bool = False, model: str | None = None) -> Split:
    """`question_only`（輸入是一句話）時可以拆出 0 題；文章拆不出來就把整篇當一題。LLM 出錯時丟 `llm.LLMError`。"""
    system = (config.PROMPT_DIR / "split.md").read_text()
    user = user_message(article)
    started = time.monotonic()
    result = Split([])
    with llm.track(result.tally):
        out = await llm.call(system, user, SCHEMA, model or config.MODEL, config.EFFORT)
        for _ in range(config.RETRIES):
            problems = problems_of(out, question_only)
            if not problems:
                break
            result.retries.append("；".join(problems))
            retry = (f"{user}\n\n<previous_output>\n{json.dumps(out, ensure_ascii=False)}\n</previous_output>\n"
                     f"<problems>\n" + "\n".join(f"- {p}" for p in problems) + "\n</problems>\n"
                     "上一次的輸出有上面這些問題，請修正後重新輸出。")
            out = await llm.call(system, retry, SCHEMA, model or config.MODEL, config.EFFORT)
    result.seconds = time.monotonic() - started
    texts = [t for q in out.get("questions") or [] if (t := (q.get("text") or "").strip())]
    result.questions = texts if question_only else texts or [article.text.strip()]
    return result
