"""開資料庫連線：不存在時照 schema.sql 建表，已經存在的照原樣開。"""
import sqlite3

from v4 import db

TABLES = {"article", "place", "place_source", "place_alias_ja", "place_alias_zh", "place_note", "place_note_time",
          "day", "day_place", "day_note", "move", "move_time"}


def tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_missing_database_is_created_with_schema(tmp_path):
    path = tmp_path / "sub" / "v4.db"

    conn = db.connect(path)

    assert path.exists()
    assert tables(conn) == TABLES


def test_existing_database_is_opened_as_is(tmp_path):
    path = tmp_path / "v4.db"
    first = db.connect(path)
    with first:
        first.execute("INSERT INTO article (id, title, posted_at) VALUES ('a', '標題', '2025-12-29T21:09:16')")
    first.close()

    conn = db.connect(path)

    assert conn.execute("SELECT id FROM article").fetchall() == [("a",)]


def test_foreign_keys_are_enforced(tmp_path):
    conn = db.connect(tmp_path / "v4.db")

    try:
        conn.execute("INSERT INTO day (article_id, month) VALUES ('沒有這篇', '1')")
    except sqlite3.IntegrityError:
        return
    raise AssertionError("應該擋下不存在的文章")
