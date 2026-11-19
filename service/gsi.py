"""國土地理院的地址查詢：把日文地址轉成經緯度。

`GET https://msearch.gsi.go.jp/address-search/AddressSearch?q=<地址>` 回傳 GeoJSON Feature 陣列，
取第一筆的 `geometry.coordinates`（[經度, 緯度]）和 `properties.title`；空陣列是查不到。
每秒最多 1 次；429、5xx、連線失敗重試，最多 4 次，指數等待（跟 `nominatim.py` 一樣）。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from service.nominatim import USER_AGENT

URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"


class GsiError(Exception):
    """國土地理院呼叫失敗（HTTP 錯誤或連線失敗，重試後仍不行），整批要停下。"""


@dataclass(frozen=True)
class GsiHit:
    title: str      # 國土地理院比對到的地址
    lat: float
    lon: float


class GsiClient:
    """限每秒 1 次請求，帶自訂 User-Agent。"""

    _last = 0.0  # 所有 client 共用，確保整個行程每秒最多 1 次

    def __init__(self, retries: int = 4):
        self.retries = retries

    def search(self, address: str) -> GsiHit | None:
        """查一個地址，回傳第一筆；查不到是 None。"""
        hits = json.loads(self._get(f"{URL}?{urllib.parse.urlencode({'q': address})}", address))
        return parse_first(hits)

    def _get(self, url: str, what: str) -> str:
        for attempt in range(1, self.retries + 1):
            wait = GsiClient._last + 1.0 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            GsiClient._last = time.monotonic()
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.read().decode()
            except urllib.error.HTTPError as e:
                if e.code != 429 and e.code < 500 or attempt == self.retries:
                    raise GsiError(f"國土地理院 HTTP {e.code}：{what}") from None
            except urllib.error.URLError as e:
                if attempt == self.retries:
                    raise GsiError(f"國土地理院連線失敗：{e.reason}") from None
            time.sleep(2 ** attempt)
        raise GsiError("國土地理院重試失敗")


def parse_first(features: list) -> GsiHit | None:
    """GeoJSON Feature 陣列取第一筆；空的是 None。"""
    if not features:
        return None
    f = features[0]
    lon, lat = f["geometry"]["coordinates"][:2]
    return GsiHit(title=(f.get("properties") or {}).get("title", ""), lat=float(lat), lon=float(lon))
