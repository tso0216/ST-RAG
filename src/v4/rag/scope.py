"""地點與範圍（規格 6）：點在不在區域裡、區域大小、區域範圍裡的地點、周圍、直線距離、名稱比對；
地點條件撈到哪些地點（6.1）。

- 區域的範圍是 `place.boundary`（geojson 的 Polygon 或 MultiPolygon）：點落在外圈裡、而且不在任何一個洞裡。
- 一個區域範圍裡的地點：區域本身，加上位置在範圍裡的景點，加上位置在範圍裡、而且比它小的區域。
  沒有 boundary 的區域只算它本身；範圍裡的區域沒有 boundary 時比不了大小，不算進去。
- 周圍：離中心直線距離不超過半徑的景點，加上中心本身。區域不算。
- 名稱比對：經驗的地點解析不到時，原文的名稱（繁中）跟 `name_zh`、繁中別名完全相同就當成是那個地點。
"""
import json
import sqlite3
from math import asin, cos, radians, sin, sqrt

from v4 import place as place_table

from .data_schema import ANYWHERE, AREA, AROUND, SPOT, Scope

EARTH_KM = 6371


def distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """大圓距離（公里），不四捨五入。"""
    a = (sin(radians(lat2 - lat1) / 2) ** 2
         + cos(radians(lat1)) * cos(radians(lat2)) * sin(radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_KM * asin(sqrt(min(1.0, a)))


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """直線距離：大圓距離四捨五入到 0.1 公里。"""
    return round(distance(lat1, lon1, lat2, lon2), 1)


def _polygons(boundary: dict | None) -> list[list[list[list[float]]]]:
    """Polygon、MultiPolygon 都變成「多邊形的 list」，每個多邊形是 [外圈, 洞, 洞……]，每圈是 [[lon, lat], …]。"""
    if not boundary:
        return []
    if boundary.get("type") == "Polygon":
        return [boundary.get("coordinates") or []]
    if boundary.get("type") == "MultiPolygon":
        return list(boundary.get("coordinates") or [])
    return []


def _in_ring(ring: list[list[float]], lat: float, lon: float) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > lat) != (y2 > lat):
            x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x:
                inside = not inside
    return inside


def contains(boundary: dict | None, lat: float, lon: float) -> bool:
    """點在不在範圍裡：在某個多邊形的外圈裡、而且不在它的任何一個洞裡。"""
    for polygon in _polygons(boundary):
        if not polygon:
            continue
        outer, holes = polygon[0], polygon[1:]
        if _in_ring(outer, lat, lon) and not any(_in_ring(h, lat, lon) for h in holes):
            return True
    return False


def _ring_area(ring: list[list[float]]) -> float:
    s = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % len(ring)][0], ring[(i + 1) % len(ring)][1]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def area_size(boundary: dict | None) -> float:
    """直接用經緯度算的面積（外圈減掉洞），只拿來比大小。"""
    return sum(_ring_area(p[0]) - sum(_ring_area(h) for h in p[1:]) for p in _polygons(boundary) if p)


def _bbox(boundary: dict) -> tuple[float, float, float, float] | None:
    points = [pt for p in _polygons(boundary) for ring in p[:1] for pt in ring]
    if not points:
        return None
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    return min(lats), max(lats), min(lons), max(lons)


def places_in_area(conn: sqlite3.Connection, area: place_table.Place) -> set[int]:
    """區域範圍裡的地點：區域本身、範圍裡的景點、範圍裡比它小的區域。"""
    ids = {area.id}
    box = _bbox(area.boundary) if area.boundary else None
    if box is None:
        return ids
    size = area_size(area.boundary)
    rows = conn.execute("SELECT id, kind, lat, lon, boundary FROM place WHERE id != ?"
                        " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?", (area.id, *box)).fetchall()
    for place_id, kind, lat, lon, boundary in rows:
        if not contains(area.boundary, lat, lon):
            continue
        if kind == SPOT:
            ids.add(place_id)
        elif kind == AREA and boundary and area_size(json.loads(boundary)) < size:
            ids.add(place_id)
    return ids


def around(conn: sqlite3.Connection, center: place_table.Place, radius_km: float) -> set[int]:
    """周圍：離中心直線距離（四捨五入到 0.1 公里）不超過半徑的景點，加上中心本身。"""
    ids = {center.id}
    dlat = radius_km / 110.0 + 0.01
    dlon = radius_km / max(0.01, 111.0 * cos(radians(center.lat))) + 0.01
    rows = conn.execute("SELECT id, lat, lon FROM place WHERE kind = ? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                        (SPOT, center.lat - dlat, center.lat + dlat, center.lon - dlon, center.lon + dlon)).fetchall()
    for place_id, lat, lon in rows:
        if km(center.lat, center.lon, lat, lon) <= radius_km:
            ids.add(place_id)
    return ids


def scope_places(conn: sqlite3.Connection, scope: Scope) -> set[int] | None:
    """地點條件撈到的地點；不限是 None（每一筆經驗都符合，包括解析不到的）。
    地點條件裡的地點要已經解析（④ 保證）。"""
    if scope.mode == ANYWHERE or scope.where is None or scope.where.place_id is None:
        return None
    found = place_table.get(conn, scope.where.place_id)
    if scope.mode == AROUND:
        return around(conn, found, scope.km or 0.0)
    if found.kind == AREA:
        return places_in_area(conn, found)
    return {found.id}


def zh_names(conn: sqlite3.Connection, ids: set[int]) -> dict[str, int]:
    """名稱比對用：這些地點的繁中名稱和繁中別名 → place id（同一個名稱對到好幾個地點時取 id 小的）。"""
    names: dict[str, int] = {}
    if not ids:
        return names
    for place_id in sorted(ids):
        rows = conn.execute("SELECT name_zh FROM place WHERE id = ? AND name_zh IS NOT NULL"
                            " UNION SELECT alias FROM place_alias_zh WHERE id = ?", (place_id, place_id)).fetchall()
        for (name,) in rows:
            name = (name or "").strip()
            if name and name not in names:
                names[name] = place_id
    return names


def display_name(conn: sqlite3.Connection, place_id: int | None, fallback: str = "") -> str:
    """地點的顯示名稱：`name_zh`，空的用 `name_ja`；沒有 place 的用原文的名稱。"""
    if place_id is None:
        return fallback
    row = conn.execute("SELECT name_zh, name_ja FROM place WHERE id = ?", (place_id,)).fetchone()
    if row is None:
        return fallback
    return row[0] or row[1] or fallback


def coordinates(conn: sqlite3.Connection, place_id: int | None) -> tuple[str, float, float] | None:
    """（種類, 緯度, 經度）；沒有 place 是 None。"""
    if place_id is None:
        return None
    row = conn.execute("SELECT kind, lat, lon FROM place WHERE id = ?", (place_id,)).fetchone()
    return tuple(row) if row else None
