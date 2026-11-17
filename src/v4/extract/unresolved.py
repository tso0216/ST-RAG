"""`place_unresolved.tsv`：讀使用者填的答案、分辨是哪一種、寫入新的要使用者決定的地點、print 清單。

一列是一個要使用者決定的地點：

- `解析不到`：一個地點一列，`name_zh` 和 `city` 相同的只列一次（`猜的` 的地點另外要 `name_ja` 相同，
  因為同一個連鎖店名在不同文章猜的是不同分店），`articles` 列出所有出現的文章。答案之後別篇出現同樣的寫法時直接套用。
- `猜不出來`：一篇文章的一個地點一列（文章 id 和 `name_zh` 都相同），答案只對那一篇有效。
"""
import csv
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .data_schema import NO_GUESS, NOT_FOUND, Unresolved

COLUMNS = ("type", "name_zh", "name_ja", "city", "articles", "sentence", "tried", "reason", "answer")
SKIP = "不收"

OSM = "osm"       # OSM 網址：用這個 OSM 物件
NAME = "name"     # 日文名稱：用這個名稱查 OSM，取第一筆
COORD = "coord"   # 座標：OSM 沒有這個地點，直接用這個位置
EXCLUDE = "skip"  # 不收

_OSM_RE = re.compile(r"openstreetmap\.org/(node|way|relation)/(\d+)")
_COORD_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)$")
_ARTICLE_SEP = ","
_TRIED_SEP = "、"


@dataclass(frozen=True)
class Answer:
    kind: str                    # OSM／NAME／COORD／EXCLUDE
    text: str                    # 使用者填的原文
    osm_type: str = ""
    osm_id: int = 0
    name: str = ""
    lat: float = 0.0
    lon: float = 0.0


def parse_answer(text: str) -> Answer | None:
    """分辨使用者填的是哪一種答案；沒填是 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if text == SKIP:
        return Answer(EXCLUDE, text)
    if m := _OSM_RE.search(text):
        return Answer(OSM, text, osm_type=m.group(1), osm_id=int(m.group(2)))
    if (m := _COORD_RE.match(text)) and abs(float(m.group(1))) <= 90 and abs(float(m.group(2))) <= 180:
        return Answer(COORD, text, lat=float(m.group(1)), lon=float(m.group(2)))
    return Answer(NAME, text, name=text)


class Answers:
    """使用者在 `place_unresolved.tsv` 填的答案。"""

    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []

    @classmethod
    def load(cls, path: Path) -> "Answers":
        return cls(_read(path))

    def for_place(self, name_zh: str, name_ja: str, city: str, guessed: bool = False) -> Answer | None:
        """`解析不到` 的答案：同樣寫法的地點都套用，不分文章。"""
        return self._find(NOT_FOUND, "", name_zh, name_ja, city, guessed)

    def for_guess(self, article_id: str, name_zh: str) -> Answer | None:
        """`猜不出來` 的答案：只看這一篇、這個地點的那一列。"""
        return self._find(NO_GUESS, article_id, name_zh, "", "", False)

    def _find(self, type_: str, article_id: str, name_zh: str, name_ja: str, city: str, guessed: bool):
        for row in self.rows:
            if _same(row, type_, article_id, name_zh, name_ja, city, guessed):
                if answer := parse_answer(row["answer"]):
                    return answer
        return None


def save(path: Path, items: list[Unresolved], handled: set[str]) -> None:
    """把這次要使用者決定的地點寫進 tsv，`answer` 欄空著。

    已經填過答案的列保留不動。還沒填的列：這次處理過的文章（handled）先從它的 `articles` 拿掉，
    再把這次還要決定的加回去；沒有文章的列刪掉（那個地點已經不用決定了）。
    """
    rows = _read(path)
    answered = [r for r in rows if r["answer"].strip()]
    open_rows = [r for r in rows if not r["answer"].strip()]
    # 先照原本的 articles 比對，最後才改寫，這樣還要決定的列留在原位、不會每次重跑都換位置
    kept = {id(r): [a for a in _split(r["articles"]) if a not in handled] for r in open_rows}
    for it in items:
        if any(_matches(r, it) for r in answered):
            continue
        row = next((r for r in open_rows if _matches(r, it)), None)
        if row is None:
            row = {**dict.fromkeys(COLUMNS, ""), "type": it.type, "name_zh": it.name_zh.strip(),
                   "name_ja": it.name_ja.strip(), "city": it.city.strip(), "articles": it.article_id}
            open_rows.append(row)
            rows.append(row)
            kept[id(row)] = []
        kept[id(row)].append(it.article_id)
        row["sentence"] = row["sentence"] or _clean(it.sentence)
        row["tried"] = _join([*_split(row["tried"], _TRIED_SEP), *it.tried], _TRIED_SEP)
        row["reason"] = row["reason"] or _clean(it.reason)
    for r in open_rows:
        r["articles"] = _join(kept[id(r)], _ARTICLE_SEP)
    rows = [r for r in rows if r["answer"].strip() or r["articles"]]
    if not rows and not path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def print_list(items: list[Unresolved], file=None) -> None:
    """印出每個要使用者決定的地點。`解析不到` 的同一個地點合成一筆，列出所有出現的文章。"""
    file = file or sys.stdout
    groups: list[list[Unresolved]] = []
    for it in items:
        group = next((g for g in groups if it.type == NOT_FOUND and _same_item(g[0], it)), None)
        if group:
            group.append(it)
        else:
            groups.append([it])
    for group in groups:
        it = group[0]
        city = f"，{it.city}" if it.city else ""
        print(f"\n[{it.type}] {it.name_zh}（{it.name_ja}{city}）", file=file)
        print(f"  文章：{'、'.join(dict.fromkeys(g.article_id for g in group))}", file=file)
        if it.sentence:
            print(f"  原文：{_clean(it.sentence)}", file=file)
        if it.type == NOT_FOUND:
            tried = list(dict.fromkeys(name for g in group for name in g.tried))
            if tried:
                print(f"  試過的日文名稱：{_TRIED_SEP.join(tried)}", file=file)
            print(f"  OSM 搜尋：{search_url(it.name_ja)}", file=file)
            if it.reason:
                print(f"  猜的依據：{_clean(it.reason)}", file=file)
        else:
            print(f"  行程：{it.where}", file=file)
            print(f"  同一天的其他站：{'、'.join(it.same_day) or '（沒有）'}", file=file)
            print(f"  原因：{_clean(it.reason)}", file=file)
        for note in dict.fromkeys(g.note for g in group if g.note):
            print(f"  ※ {note}", file=file)


def search_url(name: str) -> str:
    return "https://www.openstreetmap.org/search?query=" + urllib.parse.quote(name)


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [{c: (row.get(c) or "").strip() for c in COLUMNS} for row in csv.DictReader(f, delimiter="\t")]


def _same(row: dict, type_: str, article_id: str, name_zh: str, name_ja: str, city: str, guessed: bool) -> bool:
    if row["type"] != type_ or row["name_zh"] != name_zh.strip():
        return False
    if type_ == NO_GUESS:
        return article_id in _split(row["articles"])
    return row["city"] == city.strip() and (not guessed or row["name_ja"] == name_ja.strip())


def _matches(row: dict, it: Unresolved) -> bool:
    return _same(row, it.type, it.article_id, it.name_zh, it.name_ja, it.city, it.guessed)


def _same_item(a: Unresolved, b: Unresolved) -> bool:
    return (a.name_zh, a.city, a.guessed) == (b.name_zh, b.city, b.guessed) and (not a.guessed or a.name_ja == b.name_ja)


def _split(text: str, sep: str = _ARTICLE_SEP) -> list[str]:
    return [s.strip() for s in text.split(sep) if s.strip()]


def _join(values: list[str], sep: str) -> str:
    return sep.join(dict.fromkeys(_clean(v) for v in values if v))


def _clean(text: str) -> str:
    """tsv 一格不能有 tab 和換行。"""
    return re.sub(r"\s+", " ", text or "").strip()
