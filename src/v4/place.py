"""`place` 和別名表的讀寫：查正名、別名，新增 place，新增別名。

一列 `place` 是一個 OSM 物件；使用者填座標的地點 OSM 上沒有，`osm_type` 是 `manual`，`osm_id` 用流水號。
正名是 `place.name_ja`（OSM 上的名稱）和 `place.name_zh`（第一次抽到的繁中名稱）；
別名是之後用來查到它的其他寫法，日文和繁中分兩張表。
"""
import json
import sqlite3
from dataclasses import dataclass

SPOT = "景點"
AREA = "區域"
MANUAL = "manual"

_COLUMNS = "id, osm_type, osm_id, kind, name_ja, name_zh, city, lat, lon, boundary"


@dataclass(frozen=True)
class Place:
    id: int
    osm_type: str
    osm_id: int
    kind: str
    name_ja: str
    name_zh: str | None
    city: str
    lat: float
    lon: float
    boundary: dict | None


def get(conn: sqlite3.Connection, place_id: int) -> Place:
    return _row(conn.execute(f"SELECT {_COLUMNS} FROM place WHERE id = ?", (place_id,)).fetchone())


def by_osm(conn: sqlite3.Connection, osm_type: str, osm_id: int) -> Place | None:
    r = conn.execute(f"SELECT {_COLUMNS} FROM place WHERE osm_type = ? AND osm_id = ?",
                     (osm_type, osm_id)).fetchone()
    return _row(r) if r else None


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


def add(conn: sqlite3.Connection, *, osm_type: str, osm_id: int, kind: str, name_ja: str,
        name_zh: str | None, city: str, lat: float, lon: float, boundary: dict | None = None) -> int:
    """新增一個 OSM 物件，回傳 place id。同一個 OSM 物件已經在資料庫就回傳原本的 id，不重複新增。"""
    existing = by_osm(conn, osm_type, osm_id)
    if existing:
        return existing.id
    with conn:
        return conn.execute(
            "INSERT INTO place (osm_type, osm_id, kind, name_ja, name_zh, city, lat, lon, boundary)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (osm_type, osm_id, kind, name_ja.strip(), (name_zh or "").strip() or None, (city or "").strip(),
             lat, lon, json.dumps(boundary, ensure_ascii=False) if boundary else None)).lastrowid


def add_manual(conn: sqlite3.Connection, *, name_ja: str, name_zh: str | None, city: str,
               lat: float, lon: float) -> int:
    """OSM 沒有的地點：直接用使用者填的座標，種類是景點。同一個座標已經加過就用原本那一個。"""
    row = conn.execute("SELECT id FROM place WHERE osm_type = ? AND lat = ? AND lon = ?",
                       (MANUAL, lat, lon)).fetchone()
    if row:
        return row[0]
    serial = conn.execute("SELECT COALESCE(MAX(osm_id), 0) + 1 FROM place WHERE osm_type = ?", (MANUAL,)).fetchone()[0]
    return add(conn, osm_type=MANUAL, osm_id=serial, kind=SPOT, name_ja=name_ja, name_zh=name_zh,
               city=city, lat=lat, lon=lon)


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
