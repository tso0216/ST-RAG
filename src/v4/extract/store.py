"""寫入資料庫：查文章是否已有、整篇寫入（`place` 和別名表以外的表）。"""
import sqlite3
from collections import Counter

from .data_schema import Article, Parsed, TimeTag

# 寫入時拿掉的東西（`write_article` 回傳的 Counter 的 key）
DAY, DAY_NOTE, PLACE_NOTE, MOVE = "天", "day_note", "place_note", "move"
DROPPED_KINDS = (DAY, DAY_NOTE, PLACE_NOTE, MOVE)


def has_article(conn: sqlite3.Connection, article_id: str) -> bool:
    return conn.execute("SELECT 1 FROM article WHERE id = ?", (article_id,)).fetchone() is not None


def write_article(conn: sqlite3.Connection, article: Article, parsed: Parsed,
                  place_ids: dict[str, int | None]) -> Counter | None:
    """一篇一個 transaction 整篇寫入，回傳拿掉的東西（`DROPPED_KINDS` → 筆數）；資料庫已有這篇就不寫，回傳 None。

    place_ids：地點編號 → place id；捨棄的地點（解析不到、猜不出來）是 None。
    - `day`：有任何一站被捨棄，整天不寫（`day_place` 也不寫），掛在那一天的 `day_note` 也不寫。
    - `place_note`：地點被捨棄的不寫。
    - `move`：有一端被捨棄就整段不寫；兩端解析出來是同一個地點時改寫成 `place_note`。
    """
    if has_article(conn, article.id):
        return None
    names = {p.ref: p.name_zh for p in parsed.places}
    dropped: Counter = Counter()
    with conn:
        conn.execute("INSERT INTO article (id, title, posted_at) VALUES (?, ?, ?)",
                     (article.id, article.title, article.posted_at))

        day_ids: dict[str, int | None] = {}   # label → day id；那一天沒寫入是 None
        for day in parsed.days:
            day_id = None
            if day.stops and all(place_ids.get(ref) is not None for ref in day.stops):
                day_id = conn.execute("INSERT INTO day (article_id, month) VALUES (?, ?)",
                                      (article.id, day.month or None)).lastrowid
                conn.executemany("INSERT INTO day_place (day_id, seq, poi_id) VALUES (?, ?, ?)",
                                 [(day_id, seq, place_ids[ref]) for seq, ref in enumerate(day.stops, 1)])
            elif day.stops:
                dropped[DAY] += 1
            day_ids.setdefault(day.label, day_id)

        for n in parsed.day_notes:
            if n.day and day_ids.get(n.day) is None:
                dropped[DAY_NOTE] += 1
                continue
            conn.execute(
                "INSERT INTO day_note (article_id, day_id, content) VALUES (?, ?, ?)",
                (article.id, day_ids[n.day] if n.day else None, n.content))

        def place_note(ref: str, content: str, times: list[TimeTag]) -> None:
            note_id = conn.execute(
                "INSERT INTO place_note (article_id, poi_id, poi_name, content) VALUES (?, ?, ?, ?)",
                (article.id, place_ids[ref], names[ref], content)).lastrowid
            conn.executemany("INSERT INTO place_note_time (note_id, year, month, day_type, time)"
                             " VALUES (?, ?, ?, ?, ?)", [(note_id, *row) for row in _time_rows(times)])

        for n in parsed.place_notes:
            if place_ids.get(n.place) is None:
                dropped[PLACE_NOTE] += 1
            else:
                place_note(n.place, n.content, n.times)

        for m in parsed.moves:
            if place_ids.get(m.origin) is None or place_ids.get(m.dest) is None:
                dropped[MOVE] += 1
            elif place_ids[m.origin] == place_ids[m.dest]:
                place_note(m.origin, m.content, m.times)
            else:
                move_id = conn.execute(
                    "INSERT INTO move (article_id, from_id, to_id, from_name, to_name, content)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (article.id, place_ids[m.origin], place_ids[m.dest], names[m.origin], names[m.dest],
                     m.content)).lastrowid
                conn.executemany("INSERT INTO move_time (move_id, year, month, day_type, time)"
                                 " VALUES (?, ?, ?, ?, ?)", [(move_id, *row) for row in _time_rows(times=m.times)])
    return dropped


def _time_rows(times: list[TimeTag]) -> list[tuple]:
    """留空的欄位存 NULL；四欄都留空的那一筆不寫。"""
    rows = [(int(t.year) if t.year else None, t.month or None, t.day_type or None, t.time or None) for t in times]
    return [row for row in rows if any(v is not None for v in row)]
