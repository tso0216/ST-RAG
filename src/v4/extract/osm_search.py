"""OSM 查詢：用日文名稱查 5 筆、用 OSM 物件 id 查一筆，把回應整理成候選（名稱、類型、地址、座標、範圍）。

HTTP 呼叫在專案根目錄的 `service/nominatim.py`；這裡不快取。
"""
import json
from typing import Protocol

from service.nominatim import NominatimClient

from .data_schema import AREA, SPOT, Candidate

ANY = "不限"   # `service.nominatim` 的種類：不限景點或區域
# Nominatim 的 addresstype 裡算行政區的（`administrative` 是沒歸類的行政界，例如丁目）
ADMIN_TYPES = frozenset({
    "administrative", "neighbourhood", "quarter", "city_block", "suburb", "borough", "subdivision",
    "city_district", "district", "hamlet", "village", "town", "city", "municipality", "county",
    "state_district", "state", "province", "region",
})
_AREA_SHAPES = ("Polygon", "MultiPolygon")


class Nominatim(Protocol):
    def search(self, name_ja: str, city: str, kind: str, settlement_only: bool = False) -> str: ...

    def lookup(self, osm_type: str, osm_id: int) -> str: ...


class OsmSearch:
    """client 預設是 `NominatimClient`（每秒限 1 次）；測試時換成回固定內容的假實作。"""

    def __init__(self, client: Nominatim | None = None):
        self.client = client or NominatimClient()

    def search(self, name_ja: str, city: str = "") -> list[Candidate]:
        """用日文名稱（加城市）查，不限類型，最多 5 筆。呼叫失敗丟 `service.nominatim.NominatimError`。"""
        return _candidates(self.client.search(name_ja, city, ANY))

    def lookup(self, osm_type: str, osm_id: int) -> Candidate | None:
        """用 OSM 物件 id 查一筆；查不到是 None。"""
        found = _candidates(self.client.lookup(osm_type, osm_id))
        return found[0] if found else None


def _candidates(response: str) -> list[Candidate]:
    return [c for c in map(_candidate, json.loads(response)) if c]


def _candidate(hit: dict) -> Candidate | None:
    if not hit.get("osm_type") or hit.get("osm_id") is None:
        return None
    address = hit.get("display_name") or "、".join(str(v) for v in (hit.get("address") or {}).values())
    kind = AREA if hit.get("addresstype") in ADMIN_TYPES else SPOT
    geo = hit.get("geojson")
    return Candidate(
        osm_type=hit["osm_type"],
        osm_id=int(hit["osm_id"]),
        name=hit.get("name") or address.split(",")[0].strip(),
        type=":".join(t for t in (hit.get("category"), hit.get("type")) if t) or hit.get("addresstype") or "",
        address=address,
        lat=float(hit["lat"]),
        lon=float(hit["lon"]),
        kind=kind,
        boundary=geo if kind == AREA and geo and geo.get("type") in _AREA_SHAPES else None,
    )
