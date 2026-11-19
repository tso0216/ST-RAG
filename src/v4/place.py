"""`place`、`place_source` 和別名表的讀寫：查正名、別名，新增 place，新增別名。

一列 `place` 是一個地點；它從哪裡來記在 `place_source`（`source`、`source_id` 不重複）：

| `source` | `source_id` |
|---|---|
| `osm` | `node/123`、`way/456`、`relation/789` |
| `overture` | GERS id |
| `gsi` | `<國土地理院回傳的地址>｜<name_ja>` |
| `manual` | 使用者填的座標（`35.7148,139.7967`） |

正名是 `place.name_ja`（地圖上的名稱）和 `place.name_zh`（第一次抽到的繁中名稱）；
別名是之後用來查到它的其他寫法，日文和繁中分兩張表。
"""
import json
import sqlite3
from dataclasses import dataclass

SPOT = "景點"
AREA = "區域"

OSM = "osm"
OVERTURE = "overture"
GSI = "gsi"
MANUAL = "manual"
SOURCES = (OSM, OVERTURE, GSI, MANUAL)

_COLUMNS = "id, kind, name_ja, name_zh, city, lat, lon, boundary"


@dataclass(frozen=True)
class Place:
    id: int
    kind: str
    name_ja: str
    name_zh: str | None
    city: str
    lat: float
    lon: float
    boundary: dict | None


def get(conn: sqlite3.Connection, place_id: int) -> Place:
    return _row(conn.execute(f"SELECT {_COLUMNS} FROM place WHERE id = ?", (place_id,)).fetchone())


def by_source(conn: sqlite3.Connection, source: str, source_id: str) -> Place | None:
    r = conn.execute(f"SELECT {', '.join('p.' + c for c in _COLUMNS.split(', '))} FROM place p"
                     " JOIN place_source s ON s.place_id = p.id WHERE s.source = ? AND s.source_id = ?",
                     (source, source_id)).fetchone()
    return _row(r) if r else None


def source_of(conn: sqlite3.Connection, place_id: int) -> tuple[str, str] | None:
    """這個地點的來源 (source, source_id)；有多個時取最早的那一個。"""
    return conn.execute("SELECT source, source_id FROM place_source WHERE place_id = ? ORDER BY rowid LIMIT 1",
                        (place_id,)).fetchone()


def matches(conn: sqlite3.Connection, *, name_ja: str | None = None, name_zh: str | None = None,
            city: str = "") -> list[int]:
    """正名或別名跟 name_ja、name_zh 其中一個相同，而且 `city` 相同的 place id。沒給的名稱不查。"""
    city = (city or "").strip()
    ids: set[int] = set()
    for name, column, alias_table in ((name_ja, "name_ja", "place_alias_ja"), (name_zh, "name_zh", "place_alias_zh")):
        name = (name or "").strip()
        if not name:
            continue
        ids.update(r[0] for r in conn.execute(
            f"SELECT id FROM place WHERE city = ? AND {column} = ?"
            f" UNION SELECT p.id FROM place p JOIN {alias_table} a ON a.id = p.id"
            " WHERE p.city = ? AND a.alias = ?", (city, name, city, name)))
    return sorted(ids)


def find(conn: sqlite3.Connection, *, name_ja: str | None = None, name_zh: str | None = None,
         city: str = "") -> int | None:
    """只查到一個才回傳它的 id；查不到或查到多個都是 None。"""
    ids = matches(conn, name_ja=name_ja, name_zh=name_zh, city=city)
    return ids[0] if len(ids) == 1 else None


def add(conn: sqlite3.Connection, *, source: str, source_id: str, kind: str, name_ja: str,
        name_zh: str | None, city: str, lat: float, lon: float, boundary: dict | None = None) -> int:
    """新增一個地點，回傳 place id。同一組 (source, source_id) 已經在資料庫就回傳原本的 id，不重複新增。

    `place` 和 `place_source` 在同一個 transaction 裡寫入。
    """
    existing = by_source(conn, source, source_id)
    if existing:
        return existing.id
    with conn:
        place_id = conn.execute(
            "INSERT INTO place (kind, name_ja, name_zh, city, lat, lon, boundary) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, name_ja.strip(), (name_zh or "").strip() or None, (city or "").strip(),
             lat, lon, json.dumps(boundary, ensure_ascii=False) if boundary else None)).lastrowid
        conn.execute("INSERT INTO place_source (place_id, source, source_id) VALUES (?, ?, ?)",
                     (place_id, source, source_id))
    return place_id


def add_manual(conn: sqlite3.Connection, *, name_ja: str, name_zh: str | None, city: str,
               lat: float, lon: float) -> int:
    """地圖上沒有的地點：直接用使用者填的座標，種類是景點。同一個座標已經加過就用原本那一個。"""
    return add(conn, source=MANUAL, source_id=coordinate_id(lat, lon), kind=SPOT, name_ja=name_ja,
               name_zh=name_zh, city=city, lat=lat, lon=lon)


def coordinate_id(lat: float, lon: float) -> str:
    """`manual` 的 source_id：座標寫成 `緯度,經度`。"""
    return f"{lat},{lon}"


def add_alias(conn: sqlite3.Connection, place_id: int, *, name_ja: str | None = None,
              name_zh: str | None = None) -> None:
    """加別名，已經有的不重複加。這個地點還沒有繁中名稱時，name_zh 也記成顯示用的名稱。"""
    name_ja, name_zh = (name_ja or "").strip(), (name_zh or "").strip()
    with conn:
        if name_ja:
            conn.execute("INSERT OR IGNORE INTO place_alias_ja (id, alias) VALUES (?, ?)", (place_id, name_ja))
        if name_zh:
            conn.execute("INSERT OR IGNORE INTO place_alias_zh (id, alias) VALUES (?, ?)", (place_id, name_zh))
            conn.execute("UPDATE place SET name_zh = ? WHERE id = ? AND name_zh IS NULL", (name_zh, place_id))


def _row(r) -> Place:
    *head, boundary = r
    return Place(*head, json.loads(boundary) if boundary else None)
