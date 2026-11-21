"""① 拆題與解析（規格 8）：讀發問文（4.3）、拆題、檢查拆題的輸出（8.4）、重問；
有點、段、行程題時呼叫 v4 抽取的行程解析（`parse_line.parse`），再解析行程裡的站（`Geocoder.resolve_article`）。

整篇發問文只跑一次，各題共用。
"""
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from v4.extract import article as article_mod
from v4.extract import config as extract_config
from v4.extract import feedback, parse_line
from v4.extract.check import Problem
from v4.extract.data_schema import Article, Day, Parsed, PlaceRef
from v4.extract.data_schema import Stop as ExtractStop
from v4.extract.geocoding import Geocoder

from . import config, trace
from .data_schema import DB_QTYPES, OTHER, QTYPES, SPLIT_SCHEMA, Question, UserDay, UserStop

log = logging.getLogger(__name__)

TAIPEI = timezone(timedelta(hours=8))


def from_question(text: str, now: datetime | None = None) -> Article:
    """一句問題：文章 id 是 `q-` 加上問題文字 SHA-256 的前 12 個十六進位字元；標題空的；發文時間是現在（台北時間）。"""
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    now = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    return Article(id=f"q-{digest}", title="", author="", posted_at=now.replace(tzinfo=None, microsecond=0).isoformat(),
                   text=text.strip(), comments=[])


def load_article(path: Path) -> Article:
    """用 v4 抽取讀文章的方式讀進來（推文不用）。讀不了丟 `article.Skipped`。"""
    return article_mod.load(path)


def split_user(article: Article) -> str:
    return (f"標題：{article.title}\n"
            f"發文日期：{article.posted_date}\n\n"
            f"主文：\n<text>\n{article.text}\n</text>")


def check_split(out: dict) -> list[Problem]:
    """8.4：至少一題；題型是五種之一；問句不是空的。"""
    qs = out.get("questions") or []
    if not qs:
        return [Problem("questions", 0, "至少要拆出一題")]
    problems = []
    for i, q in enumerate(qs):
        if q.get("type") not in QTYPES:
            problems.append(Problem("questions", i, f"type「{q.get('type')}」要是{'、'.join(QTYPES)}其中一個"))
        if not (q.get("text") or "").strip():
            problems.append(Problem("questions", i, "text（問句）是空的"))
    return problems


async def split(article: Article, record: dict) -> list[Question]:
    """拆題。重問後題型還不合法的改成其他；問句還是空的拿掉；一題都沒有就把整篇主文當一題，題型其他。"""
    system = (config.PROMPT_DIR / "split.md").read_text()
    attempts: list[list[str]] = []

    def check(out: dict) -> list[Problem]:
        problems = check_split(out)
        attempts.append([str(p) for p in problems])
        return problems

    with trace.step("拆題", config.MODEL):
        out, problems = await feedback.ask(system, split_user(article), SPLIT_SCHEMA, check, f"{article.id} 拆題",
                                           "split.md", model=config.MODEL, effort=config.EFFORT,
                                           retries=config.RETRIES)
    fixes: list[str] = []
    questions: list[Question] = []
    for q in out.get("questions") or []:
        text = (q.get("text") or "").strip()
        if not text:
            fixes.append("問句是空的，拿掉")
            continue
        qtype = q.get("type")
        if qtype not in QTYPES:
            fixes.append(f"「{text}」的題型「{qtype}」不合法，改成{OTHER}")
            qtype = OTHER
        questions.append(Question(len(questions) + 1, text, qtype))
    if not questions:
        fixes.append(f"一題都沒拆出來，整篇主文當成一題，題型{OTHER}")
        questions = [Question(1, article.text.strip() or article.title.strip(), OTHER)]
    record.update({"輸出": out, "重問次數": max(0, len(attempts) - 1), "每次的問題": attempts,
                   "重問後還有的問題": [str(p) for p in problems], "修正": fixes,
                   "題目": [{"第幾題": q.no, "問句": q.text, "題型": q.qtype} for q in questions]})
    log.info("拆題：%s", "；".join(f"{q.no}. [{q.qtype}] {q.text}" for q in questions))
    return questions


@dataclass
class Itinerary:
    """① 第 3、4 步的結果：地點列表、使用者的行程、行程裡每一站的解析結果（地點編號 → place id）。"""
    places: list[PlaceRef] = field(default_factory=list)
    days: list[UserDay] = field(default_factory=list)
    resolved: dict[str, int | None] = field(default_factory=dict)


def needs_itinerary(questions: list[Question]) -> bool:
    return any(q.qtype in DB_QTYPES for q in questions)


async def itinerary(article: Article, geocoder: Geocoder, record: dict) -> Itinerary:
    """解析使用者的行程（v4 抽取的第一次 llm 解析），再解析行程裡的站。行程解析重問後還有問題丟 `ParseError`。"""
    with trace.step("行程解析", extract_config.MODEL):
        line = await parse_line.parse(article)
    places = [PlaceRef(**p) for p in line["places"]]
    days = [UserDay(d["label"], d["month"], tuple(UserStop(s["ref"], s["type"]) for s in d["stops"]))
            for d in line["days"]]
    record.update({
        "地點列表": [{"編號": p.ref, "繁中名稱": p.name_zh, "日文名稱": p.name_ja, "城市": p.city, "種類": p.kind,
                  "找得到嗎": p.located, "依據": p.reason, "原文": p.sentence} for p in places],
        "使用者的行程": [{"編號": f"D{i}", "原文": d.label, "月份": d.month,
                    "站": [f"{s.ref}（{s.type}）" for s in d.stops]} for i, d in enumerate(days, 1)],
    })
    parsed = Parsed(places=places, days=[Day(d.label, d.month, [ExtractStop(s.ref, s.type) for s in d.stops])
                                         for d in days], place_notes=[], moves=[], day_notes=[])
    traces = []
    with trace.step("地點解析（行程裡的站）", extract_config.MODEL):
        resolved = await geocoder.resolve_article(article.id, parsed, on_place=lambda _i, _n, t: traces.append(t))
    names = {p.ref: p.name_zh for p in places}
    record["行程裡的站的解析"] = [
        {"編號": t.place.ref, "名稱": names.get(t.place.ref, ""), "place_id": resolved.get(t.place.ref),
         "結果": t.result, "原因": t.fail_reason} for t in traces]
    log.info("行程解析：%d 個地點、%d 天；站解析不到 %d 個", len(places), len(days),
             sum(1 for v in resolved.values() if v is None))
    return Itinerary(places, days, resolved)
