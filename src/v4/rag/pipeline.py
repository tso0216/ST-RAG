"""一篇的流程（spec 2.1、2.3、2.5）：讀輸入 → 拆題、解析使用者的行程 → 每題跑一個迴圈 → 照順序合併答案。

本機地圖不存在、拆題或行程解析出錯（LLM 出錯、`parse_line.parse` 檢查不過）時，例外往外丟，這篇停下。
"""
import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from service import llm
from v4 import db
from v4.extract import parse_line
from v4.extract.article import load as load_json
from v4.extract.data_schema import LOCATED_UNKNOWN, Article, Parsed
from v4.extract.geocoding import Geocoder
from v4.extract.local_map import LocalMap

from . import config, loop
from .log import Log, usage_line
from .split import split
from .tools import Env, UserDay, UserPlace, UserStop

_TAIPEI = timezone(timedelta(hours=8))
_EMPTY_NOTE = {"places": [], "place_notes": [], "moves": [], "day_notes": []}


@dataclass
class Answer:
    question: str
    outcome: loop.Outcome


def from_question(text: str) -> Article:
    """一句問題當成一篇：文章 id 是 `q-` 加問題文字 SHA-256 的前 12 碼，發文時間是現在（台北時間）。"""
    text = text.strip()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    now = datetime.now(_TAIPEI).replace(tzinfo=None, microsecond=0).isoformat()
    return Article(id=f"q-{digest}", title="", author="", posted_at=now, text=text, comments=[])


def from_file(path: Path) -> Article:
    """PTT 文章 JSON；推文不用（推文是別人的回答）。讀不了丟 `v4.extract.article.Skipped`。"""
    a = load_json(path)
    return Article(a.id, a.title, a.author, a.posted_at, a.text, [])


async def parse_itinerary(conn, local_map: LocalMap, article: Article) -> tuple[Env, list[str], list[str], str, str]:
    """解析主文的地點和按天排的行程，只把行程裡的站解析成 place。

    回傳工具的環境、log 用的地點行和天的行、用量那一行、`--verbose` 的摘要。
    """
    started = time.monotonic()
    with llm.track() as tally:
        line = await parse_line.parse(article)
        parsed = Parsed.from_llm(line, _EMPTY_NOTE)
        geocoder = Geocoder(conn, local_map)
        reasons: dict[str, str] = {}

        def on_place(_done: int, _total: int, trace) -> None:
            if trace.fail_reason:
                reasons[trace.place.ref] = trace.fail_reason

        ids = await geocoder.resolve_article(article.id, parsed, on_place)
    env = Env(conn=conn, article_id=article.id, geocoder=geocoder)
    for p in parsed.places:
        up = UserPlace(p.ref, p.name_zh, p.name_ja, p.city, p.kind, p.located, p.sentence)
        if p.ref in ids:
            up.place_id = ids[p.ref]
            up.status = "已解析" if up.place_id is not None else "解析不到"
            up.reason = reasons.get(p.ref, "")
        elif p.located == LOCATED_UNKNOWN:
            up.status, up.reason = "解析不到", "猜不出來"
        env.places[p.ref] = up
    for i, d in enumerate(parsed.days, 1):
        stops = [UserStop(s.ref, env.places[s.ref].name_zh if s.ref in env.places else s.ref, s.type,
                          env.places[s.ref].place_id if s.ref in env.places else None) for s in d.stops]
        env.days[f"D{i}"] = UserDay(f"D{i}", d.label, d.month, stops)

    place_lines = []
    for up in env.places.values():
        result = {"已解析": str(up.place_id), "還沒解析": "還沒解析",
                  "解析不到": "解析不到" + (f"（{up.reason}）" if up.reason else "")}[up.status]
        place_lines.append(f"{up.ref} {up.name_zh} → {result}")
    day_lines = [f"{d.code}｜{_month(d.month)}｜" + " → ".join(s.name for s in d.stops) for d in env.days.values()]
    usage = usage_line(tally.input_tokens, tally.output_tokens, tally.cost_usd, time.monotonic() - started)
    failed = sum(1 for up in env.places.values() if up.status == "解析不到")
    summary = f"行程：{len(env.days)} 天、{len(env.places)} 個地點（解析不到 {failed} 個）"
    return env, place_lines, day_lines, usage, summary


def _month(month: str) -> str:
    return f"{month} 月" if (month or "").strip() else "月份沒寫"


def first_message(article: Article, env: Env, question: str) -> str:
    rows = ["編號｜繁中名稱｜日文名稱｜城市｜景點或區域｜place id"]
    for up in env.places.values():
        pid = {"已解析": str(up.place_id), "還沒解析": f"還沒解析，用 find_place ref={up.ref}",
               "解析不到": "解析不到"}[up.status]
        rows.append(f"{up.ref}｜{up.name_zh}｜{up.name_ja}｜{up.city}｜{up.kind}｜{pid}")
    places = "\n".join(rows) if env.places else "（沒有）"
    days = []
    for d in env.days.values():
        stops = " → ".join(f"{s.ref} {s.name}（{s.type}，{s.place_id if s.place_id is not None else '解析不到'}）"
                           for s in d.stops)
        days.append(f"{d.code}（原文：{d.label}）｜{_month(d.month)}｜{stops}")
    itinerary = "\n".join(days) if days else "沒有按天排的行程"
    return (f"<article>\n標題：{article.title}\n發文日期：{article.posted_date}\n主文：\n{article.text}\n</article>\n\n"
            f"<places>\n{places}\n</places>\n\n"
            f"<itinerary>\n{itinerary}\n</itinerary>\n\n"
            f"<question>{question}</question>\n\n"
            f"<limit>這一題最多呼叫工具 {config.TOOL_CALLS} 次。</limit>")


def system_prompt() -> str:
    return (config.PROMPT_DIR / "agent.md").read_text().replace("{{NO_EVIDENCE_OPENING}}", config.NO_EVIDENCE_OPENING)


async def run(article: Article, db_path: Path, log: Log, *, question_only: bool = False,
              local_map_path: Path | None = None, model: str | None = None) -> str:
    """跑完一篇，回傳合併後的答案（Markdown）。每題的答案也會隨跑隨交給 `log`。

    `model` 換掉拆題和工具迴圈的模型（沒給用 `config.MODEL`）；行程解析照 `v4.extract.config`。
    """
    log.input(article, question_only)
    local_map = LocalMap(local_map_path)          # 不存在丟 MissingLocalMap
    conn = db.connect(db_path)
    parsing = asyncio.create_task(parse_itinerary(conn, local_map, article))   # 跟拆題同時跑；拆出 0 題就取消
    try:
        parts = await split(article, question_only, model)
        log.split(parts.questions, parts.retries, usage_line(parts.tally.input_tokens, parts.tally.output_tokens,
                                                              parts.tally.cost_usd, parts.seconds))
        if not parts.questions:
            log.no_question(config.NO_QUESTION_TEXT)
            return config.NO_QUESTION_TEXT
        env, place_lines, day_lines, usage, summary = await parsing
        log.attach(env)
        log.itinerary(place_lines, day_lines, usage, summary)

        system = system_prompt()
        answers: list[Answer] = []
        for n, question in enumerate(parts.questions, 1):
            env.question = question
            log.question(n, question)
            out = await loop.run(env, system, first_message(article, env, question), log, model)
            log.answer(n, question, out.answer, out.stop, out.calls, out.turns,
                       usage_line(out.input_tokens, out.output_tokens, out.cost_usd, out.seconds))
            answers.append(Answer(question, out))
        return "\n\n".join(f"### 第 {n} 題：{a.question}\n\n{a.outcome.answer.strip()}"
                           for n, a in enumerate(answers, 1))
    finally:
        parsing.cancel()
        try:
            await asyncio.gather(parsing, return_exceptions=True)   # 等它停下才能關連線
        finally:
            conn.close()
            local_map.close()
