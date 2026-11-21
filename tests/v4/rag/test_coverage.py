"""`COVERAGE`（每一欄怎麼查得到）跟 `schema.sql` 的表、欄位一致：每一欄都有寫到，也沒有寫到已經不存在的表或欄位。"""
import sqlite3
from pathlib import Path

from v4.rag.coverage import COVERAGE

SCHEMA = Path(__file__).resolve().parents[3] / "src" / "v4" / "schema.sql"


def schema_tables() -> dict[str, set[str]]:
    """照 schema.sql 建一個空的資料庫，回傳每張表（不含 view）的欄位。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")} for t in names}
    finally:
        conn.close()


def covered(columns) -> set[str]:
    """COVERAGE 一張表的欄位：可以是欄位名稱的 dict／list，或 (欄位, 說明) 的 list。"""
    return {c if isinstance(c, str) else c[0] for c in columns}


def test_every_table_is_covered():
    assert set(COVERAGE) == set(schema_tables())


def test_every_column_is_covered():
    for table, columns in schema_tables().items():
        assert covered(COVERAGE[table]) == columns, table
