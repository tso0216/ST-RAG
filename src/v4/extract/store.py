"""寫入資料庫：查文章是否已有、整篇寫入（`place` 和別名表以外的表）。

`day`、`place_note`、`day_note`、`move` 的 id 是 `row_id(文章 id, 第幾筆)`，例如 `1767020760-1`（見 `src/v4/schema.sql`）。
"""
import sqlite3
from collections import Counter
from dataclasses import dataclass

from .data_schema import Article, Parsed, TimeTag

# 寫入時拿掉的東西的種類（`Dropped.kind`）
DAY_NOTE = "day_note"
DROPPED_KINDS = (DAY_NOTE,)
SNIPPET = 20   # 印出拿掉的 day_note 時，內容最多印幾個字


@dataclass(frozen=True)
class Dropped:
    """寫入時拿掉的一筆。what：是哪一筆（`「第五天之後不用租車」`）；why：為什麼拿掉。"""
    kind: str
    article_id: str
    what: str
    why: str

    def line(self) -> str:
        """`M.xxx day_note 「這天還沒排」：講 D3，D3 沒有站`。"""
        return f"{self.article_id} {self.kind} {self.what}：{self.why}"


def row_id(article_id: str, n: int) -> str:
    """`M.1767020760.A.E80` 的第 n 筆是 `1767020760-n`；文章 id 不是這種寫法就用整個 id。"""
    parts = article_id.split(".")
    key = parts[1] if len(parts) == 4 and parts[1].isdigit() else article_id
    return f"{key}-{n}"


def has_article(conn: sqlite3.Connection, article_id: str) -> bool:
    return conn.execute("SELECT 1 FROM article WHERE id = ?", (article_id,)).fetchone() is not None


def write_article(conn: sqlite3.Connection, article: Article, parsed: Parsed,
                  place_ids: dict[str, int | None]) -> list[Dropped] | None:
    """一篇一個 transaction 整篇寫入，回傳拿掉的東西（一筆一個 `Dropped`）；資料庫已有這篇就不寫，回傳 None。

    place_ids：地點編號 → place id；解析不到的地點（查不到、猜不出來）是 None。
    解析不到的地點照樣寫入：`day_place.poi_id`、`place_note.poi_id`、`move.from_id`／`to_id` 留空，名稱照原文。
    - `day`：沒有任何一站的那天不寫；id 的第幾筆是行程裡的第幾天，所以那天的編號會跳過。
    - `day_note`：講到的那幾天有任何一天沒寫入（那天沒有站），整筆不寫；講整趟的照寫。
    - `move`：兩端解析出來是同一個地點時改寫成 `place_note`。
    """
    if has_article(conn, article.id):
        return None
    names = {p.ref: p.name_zh for p in parsed.places}
    dropped: list[Dropped] = []
    counts: Counter[str] = Counter()   # 表 → 這篇寫了幾筆，給 id 編號

    def next_id(table: str) -> str:
        counts[table] += 1
        return row_id(article.id, counts[table])

    with conn:
        conn.execute("INSERT INTO article (id, title, posted_at) VALUES (?, ?, ?)",
                     (article.id, article.title, article.posted_at))

        day_ids: list[str | None] = []   # 第幾天（0 起算）→ day id；那一天沒寫入是 None
        for no, day in enumerate(parsed.days, 1):
            day_id = None
            if day.stops:
                day_id = row_id(article.id, no)
                conn.execute("INSERT INTO day (id, article_id, month) VALUES (?, ?, ?)",
                             (day_id, article.id, day.month or None))
                conn.executemany(
                    "INSERT INTO day_place (day_id, seq, poi_id, poi_name, type) VALUES (?, ?, ?, ?, ?)",
                    [(day_id, seq, place_ids.get(s.ref), names[s.ref], s.type) for seq, s in enumerate(day.stops, 1)])
            day_ids.append(day_id)

        def day_label(d: int) -> str:
            return parsed.days[d - 1].label if 1 <= d <= len(parsed.days) else f"第 {d} 天"

        for n in parsed.day_notes:
            ids = [day_ids[d - 1] if 1 <= d <= len(day_ids) else None for d in n.days]
            if None in ids:
                missing = [day_label(d) for d, i in zip(n.days, ids) if i is None]
                dropped.append(Dropped(DAY_NOTE, article.id, _snippet(n.content),
                                       f"講 {'、'.join(map(day_label, n.days))}，{'、'.join(missing)} 沒有站"))
                continue
            note_id = next_id("day_note")
            conn.execute("INSERT INTO day_note (id, article_id, content) VALUES (?, ?, ?)", (note_id, article.id, n.content))
            conn.executemany("INSERT INTO day_note_day (note_id, day_id) VALUES (?, ?)",
                             [(note_id, day_id) for day_id in ids])

        def place_note(ref: str, content: str, times: list[TimeTag]) -> None:
            note_id = next_id("place_note")
            conn.execute("INSERT INTO place_note (id, article_id, poi_id, poi_name, content) VALUES (?, ?, ?, ?, ?)",
                         (note_id, article.id, place_ids.get(ref), names[ref], content))
            conn.executemany("INSERT INTO place_note_time (note_id, year, month, day_type, time)"
                             " VALUES (?, ?, ?, ?, ?)", [(note_id, *row) for row in _time_rows(times)])

        for n in parsed.place_notes:
            place_note(n.place, n.content, n.times)

        for m in parsed.moves:
            origin, dest = place_ids.get(m.origin), place_ids.get(m.dest)
            if origin is not None and origin == dest:
                place_note(m.origin, m.content, m.times)
                continue
            move_id = next_id("move")
            conn.execute("INSERT INTO move (id, article_id, from_id, to_id, from_name, to_name, content)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (move_id, article.id, origin, dest, names[m.origin], names[m.dest], m.content))
            conn.executemany("INSERT INTO move_time (move_id, year, month, day_type, time)"
                             " VALUES (?, ?, ?, ?, ?)", [(move_id, *row) for row in _time_rows(times=m.times)])
    return dropped


def _snippet(content: str) -> str:
    """`「第五天之後就不用租車」`；太長的截掉加 `…`。"""
    return f"「{content[:SNIPPET]}{'…' if len(content) > SNIPPET else ''}」"


def _time_rows(times: list[TimeTag]) -> list[tuple]:
    """留空的欄位存 NULL；四欄都留空的那一筆不寫。"""
    rows = [(int(t.year) if t.year else None, t.month or None, t.day_type or None, t.time or None) for t in times]
    return [row for row in rows if any(v is not None for v in row)]
