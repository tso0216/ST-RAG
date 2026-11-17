"""程式整理文章：讀 JSON、取文章 id、合併推文並編號、標出原 po 的推文、讀出發文日期。"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .data_schema import Article, Comment

_ID_RE = re.compile(r"M\.\d+\.A\.[0-9A-Za-z]+")
# ptt 的時間：Mon Dec 29 21:09:16 2025
_TIME_RE = re.compile(r"([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})\s+(\d{4})")
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_TAIPEI = timezone(timedelta(hours=8))


class Skipped(Exception):
    """這篇不抽，訊息是原因。"""


def load(path: Path) -> Article:
    """讀一篇文章 JSON。讀不了、取不到文章 id、沒有 `author` 的都丟 `Skipped`。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise Skipped(f"讀不了 JSON：{e}") from None
    if not isinstance(doc, dict):
        raise Skipped("JSON 不是一篇文章")
    article_id = article_id_of(doc.get("url") or "")
    if not article_id:
        raise Skipped("url 裡取不到文章 id")
    author = (doc.get("author") or "").strip()
    if not author:
        raise Skipped("沒有 author")
    return Article(
        id=article_id,
        title=(doc.get("title") or "").strip(),
        author=author,
        posted_at=posted_at(doc.get("time") or "", article_id),
        text=(doc.get("text") or "").strip(),
        comments=merge_comments(doc.get("comments") or [], author),
    )


def article_id_of(url: str) -> str | None:
    m = _ID_RE.search(url)
    return m.group(0) if m else None


def posted_at(time_text: str, article_id: str) -> str:
    """發文時間（ISO 格式）。`time` 讀不出來時用文章 id 裡的時間戳（台北時間）。"""
    m = _TIME_RE.search(time_text)
    if m and m.group(1).title() in _MONTHS:
        month, day, hour, minute, second, year = m.groups()
        try:
            return datetime(int(year), _MONTHS[month.title()], int(day),
                            int(hour), int(minute), int(second)).isoformat()
        except ValueError:
            pass
    stamp = int(article_id.split(".")[1])
    return datetime.fromtimestamp(stamp, _TAIPEI).replace(tzinfo=None).isoformat()


def merge_comments(comments: list[dict], author: str) -> list[Comment]:
    """同一人連續的推文合併成一則（ptt 一則留言太長會被切成好幾行），合併後從 1 編號；空的推文略過。"""
    merged: list[list[str]] = []
    for c in comments:
        user, content = (c.get("user") or "").strip(), (c.get("content") or "").strip()
        if not content:
            continue
        if merged and merged[-1][0] == user:
            merged[-1][1] += content
        else:
            merged.append([user, content])
    return [Comment(no, user, content, user == author) for no, (user, content) in enumerate(merged, 1)]
