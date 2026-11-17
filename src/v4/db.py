"""開資料庫連線；資料庫不存在時照 `schema.sql` 建表。"""
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
MEMORY = ":memory:"


def connect(path: Path | str) -> sqlite3.Connection:
    """已經存在的資料庫照原樣開，不檢查也不更動它的表。"""
    new = str(path) == MEMORY or not Path(path).exists()
    if new and str(path) != MEMORY:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    if new:
        conn.executescript(SCHEMA_PATH.read_text())
    return conn
