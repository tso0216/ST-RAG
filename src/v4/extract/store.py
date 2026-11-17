"""寫入資料庫：查文章是否已有、整篇寫入（`place` 和別名表以外的表）。"""
import sqlite3

from .data_schema import Article, Parsed, TimeTag


def has_article(conn: sqlite3.Connection, article_id: str) -> bool:
    return conn.execute("SELECT 1 FROM article WHERE id = ?", (article_id,)).fetchone() is not None


def write_article(conn: sqlite3.Connection, article: Article, parsed: Parsed,
                  place_ids: dict[str, int | None]) -> int | None:
    """一篇一個 transaction 整篇寫入，回傳不收入的筆數；資料庫已有這篇就不寫，回傳 None。

    place_ids：地點編號 → place id；不收入的地點是 None。
    - `day_place`：不收入的站不寫，`seq` 照原文的位置，所以會跳號。
    - `day`：沒有任何一站可以放的那一天不寫，掛在那一天的 `day_note` 也不寫。
    - `place_note`：地點不收入的不寫。
    - `move`：一端不收入時改寫成 `place_note` 掛在另一端，兩端都不收入就不寫；
      兩端解析出來是同一個地點時也寫成 `place_note`。
    """
    if has_article(conn, article.id):
        return None
    names = {p.ref: p.name_zh for p in parsed.places}
    dropped = 0
    with conn:
        conn.execute("INSERT INTO article (id, title, posted_at) VALUES (?, ?, ?)",
                     (article.id, article.title, article.posted_at))

        day_ids: dict[str, int | None] = {}   # label → day id；那一天沒寫入是 None
        for day in parsed.days:
            stops = [(seq, place_ids[ref]) for seq, ref in enumerate(day.stops, 1) if place_ids.get(ref) is not None]
            dropped += len(day.stops) - len(stops)
            day_id = None
            if stops:
                day_id = conn.execute("INSERT INTO day (article_id, month) VALUES (?, ?)",
                                      (article.id, day.month or None)).lastrowid
                conn.executemany("INSERT INTO day_place (day_id, seq, poi_id) VALUES (?, ?, ?)",
                                 [(day_id, seq, place_id) for seq, place_id in stops])
            day_ids.setdefault(day.label, day_id)

        for n in parsed.day_notes:
            if n.day and day_ids.get(n.day) is None:
                dropped += 1
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
                dropped += 1
            else:
                place_note(n.place, n.content, n.times)

        for m in parsed.moves:
            ends = [ref for ref in (m.origin, m.dest) if place_ids.get(ref) is not None]
            if not ends:
                dropped += 1
            elif len(ends) == 1 or place_ids[m.origin] == place_ids[m.dest]:
                place_note(ends[0], m.content, m.times)
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
