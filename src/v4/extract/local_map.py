"""本機地圖：在 `data/geo/japan.duckdb`（OSM＋Overture，`scripts/geo/build_japan.py` 建）裡用名稱模糊搜尋。

唯讀開啟，不打網路。表結構是 `SCHEMA`（建置腳本和測試也用它）。

範圍：搜尋時可以限定範圍（`Area`）：都道府縣、市區町村，或以某個點為中心的圓；沒給就是全日本。
llm 寫的縣市名稱先用 `admin_area` 對到地圖裡的寫法，對不到丟 `AreaError`（訊息是給 llm 看的）。

比對：查詢的名稱（可以好幾個）和資料裡的名稱都先 `normalize`；一筆地點拿它每個比對用名稱跟每個查詢名稱比，取最好的結果：
完全相同 → 包含（地圖上的名稱包含查詢的名稱，查詢至少 2 個字）→ 模糊（jaro_winkler_similarity ≥ 門檻）。
範圍內只要有一筆完全相同或包含，模糊的就都不列。
排序：比對結果 → 相似度高 → 離圓心近 → 名稱短。OSM、Overture 各取前 N 筆，合起來照同樣的順序排。
"""
import difflib
import json
import math
import re
import unicodedata
from pathlib import Path

import duckdb

from . import config
from .data_schema import CONTAINS, EXACT, FUZZY, Area, Candidate

TABLE = "place"
SCHEMA = f"""
CREATE TABLE {TABLE} (
    id           INTEGER PRIMARY KEY,
    source       VARCHAR NOT NULL,     -- osm／overture
    source_id    VARCHAR NOT NULL,     -- OSM：node/123、way/456、relation/789；Overture：GERS id
    name         VARCHAR NOT NULL,     -- OSM：name:ja，沒有再用 name；Overture：names.primary
    match_names  VARCHAR[] NOT NULL,   -- 正規化後的所有名稱
    category     VARCHAR NOT NULL,     -- OSM：key=value；Overture：categories.primary
    kind         VARCHAR NOT NULL,     -- 景點／區域
    pref         VARCHAR NOT NULL,     -- 都道府縣
    municipality VARCHAR NOT NULL,     -- 市區町村
    address      VARCHAR NOT NULL,
    lat          DOUBLE  NOT NULL,     -- 代表點
    lon          DOUBLE  NOT NULL,
    boundary     VARCHAR               -- 區域而且是多邊形才有（GeoJSON）
)
"""

_DROP = re.compile(r"[\s・\-‐－]")
_KANA = str.maketrans({"ヶ": "ケ", "ヵ": "カ"})
# 判斷同城市時去掉的字尾
_SUFFIXES = ("都", "府", "県", "市", "区", "町", "村")
_RANK = {0: EXACT, 1: CONTAINS, 2: FUZZY}


class MissingLocalMap(Exception):
    """本機地圖檔不存在。"""


class AreaError(Exception):
    """llm 寫的縣市在本機地圖裡對不到。訊息直接回給 llm，請它改。"""


def normalize(name: str | None) -> str:
    """名稱正規化：NFKC → 英文轉小寫 → 去掉空白（含全形）、`・`、`-`、`‐`、`－`（`ー` 保留）→ `ヶ`、`ヵ` 轉 `ケ`、`カ`。"""
    s = unicodedata.normalize("NFKC", name or "")
    s = "".join(ch.lower() if ch.isascii() else ch for ch in s)
    return _DROP.sub("", s).translate(_KANA)


def city_key(name: str | None) -> str:
    """比對縣市名稱用的寫法：去掉結尾的「都、府、県、市、区、町、村」。

    去掉後只剩一個字時不去（「京都」不變成「京」，這樣「京都」和「京都府」、「京都市」才對得上）。
    """
    s = _DROP.sub("", unicodedata.normalize("NFKC", name or ""))
    return s[:-1] if len(s) >= 3 and s.endswith(_SUFFIXES) else s


_COLUMNS = "source, source_id, name, category, kind, pref, municipality, address, lat, lon, boundary"

# 圓的範圍用的距離（公里）：緯度一度 111.195 公里，經度乘上 cos(緯度)。10 公里內跟球面距離差不到 1%。
_DISTANCE = "111.195 * sqrt(power(lat - $lat, 2) + power((lon - $lon) * cos(radians($lat)), 2))"

_WHERE = {
    "japan": "true",
    "admin": "pref = $pref AND ($municipality = '' OR municipality = $municipality)",
    "circle": f"lat BETWEEN $lat - $dlat AND $lat + $dlat AND lon BETWEEN $lon - $dlon AND $lon + $dlon "
              f"AND {_DISTANCE} <= $km",
}


def _search_sql(where: str, distance: str) -> str:
    return f"""
WITH area AS (
    SELECT * FROM {TABLE} WHERE {where}
), names AS (
    SELECT id, unnest(match_names) AS n FROM area
), best AS (
    SELECT id,
           min(CASE WHEN list_contains($qs, n) THEN 0
                    WHEN list_bool_or([length(q) >= 2 AND contains(n, q) FOR q IN $qs]) THEN 1
                    ELSE 2 END) AS match_rank,
           max(list_max([jaro_winkler_similarity(n, q) FOR q IN $qs])) AS similarity
    FROM names
    GROUP BY id
), hit AS (
    SELECT a.*, b.match_rank, b.similarity, {distance} AS distance
    FROM best b JOIN area a USING (id)
    WHERE b.match_rank < 2 OR b.similarity >= $threshold
), kept AS (
    SELECT * FROM hit WHERE match_rank < 2 OR NOT EXISTS (SELECT 1 FROM hit WHERE match_rank < 2)
), ranked AS (
    SELECT *, row_number() OVER (
        PARTITION BY source ORDER BY match_rank, similarity DESC, distance, length(name), id) AS n
    FROM kept
)
SELECT {_COLUMNS}, match_rank, similarity
FROM ranked
WHERE n <= $per_source
ORDER BY match_rank, similarity DESC, distance, length(name), source, id
"""


_SEARCH = {"japan": _search_sql(_WHERE["japan"], "0"), "admin": _search_sql(_WHERE["admin"], "0"),
           "circle": _search_sql(_WHERE["circle"], _DISTANCE)}


class LocalMap:
    """唯讀開啟本機地圖。檔案不存在丟 `MissingLocalMap`。"""

    def __init__(self, path: Path | str | None = None, per_source: int | None = None,
                 threshold: float | None = None):
        self.path = Path(path or config.LOCAL_MAP_PATH)
        if not self.path.exists():
            raise MissingLocalMap(f"找不到本機地圖 {self.path}，請先跑 scripts/geo/build_japan.py")
        self.per_source = per_source or config.CANDIDATES_PER_SOURCE
        self.threshold = config.FUZZY_THRESHOLD if threshold is None else threshold
        self.conn = duckdb.connect(str(self.path), read_only=True)
        self._admin_cache: list[tuple[str, str]] | None = None

    def close(self) -> None:
        self.conn.close()

    def search(self, names: str | list[str], area: Area | None = None) -> list[Candidate]:
        """用一個或幾個日文名稱，在範圍內（沒給是全日本）模糊搜尋。

        回傳最多 每個來源 N 筆 × 2 的候選，不合併去重。
        """
        qs = list(dict.fromkeys(q for q in (normalize(n) for n in ([names] if isinstance(names, str) else names)) if q))
        if not qs:
            return []
        area = area or Area()
        params = {"qs": qs, "threshold": self.threshold, "per_source": self.per_source}
        if area.lat is not None and area.lon is not None and area.km is not None:
            mode = "circle"
            params |= {"lat": area.lat, "lon": area.lon, "km": area.km, "dlat": area.km / 111.0,
                       "dlon": area.km / (111.0 * max(math.cos(math.radians(area.lat)), 0.1))}
        elif area.pref:
            mode = "admin"
            params |= {"pref": area.pref, "municipality": area.municipality}
        else:
            mode = "japan"
        rows = self.conn.execute(_SEARCH[mode], params).fetchall()
        return [_candidate(r[:11], _RANK[r[11]], r[12]) for r in rows]

    def admin_area(self, pref: str = "", municipality: str = "") -> Area:
        """把 llm 寫的都道府縣、市區町村對到地圖裡的寫法，回傳行政區範圍；兩個都空的是全日本。

        比對時去掉結尾的「都府県市区町村」；市區町村完全相同的優先（「湯沢町」不會對到「湯沢市」）。
        對不到、或沒寫縣而市區町村在好幾個縣都有，丟 `AreaError`。
        """
        pref, municipality = _clean(pref), _clean(municipality)
        admin = self._admin()
        prefs = sorted({p for p, _ in admin})
        if pref:
            matched = [p for p in prefs if p == pref or city_key(p) == city_key(pref)]
            if not matched:
                raise AreaError(f"沒有「{pref}」這個都道府縣。地圖裡的寫法：{'、'.join(prefs)}")
            pref = matched[0]
        if not municipality:
            return Area(pref=pref)
        pool = [(p, m) for p, m in admin if m and (not pref or p == pref)]
        found = [(p, m) for p, m in pool if m == municipality] or \
                [(p, m) for p, m in pool if city_key(m) == city_key(municipality)]
        if not found:
            where = f"「{pref}」" if pref else "地圖"
            similar = _similar(municipality, sorted({m for _, m in pool}))
            hint = f"名稱相近的：{'、'.join(similar)}。" if similar else ""
            raise AreaError(f"{where}裡沒有「{municipality}」這個市區町村。{hint}"
                            "政令指定都市只到「市」（例：さいたま市、横浜市、大阪市），沒有「区」。")
        if len(found) > 1:
            listed = "、".join(f"{p} {m}" for p, m in found)
            raise AreaError(f"「{municipality}」有好幾個：{listed}。請寫上都道府縣（pref）。")
        return Area(pref=found[0][0], municipality=found[0][1])

    def _admin(self) -> list[tuple[str, str]]:
        """地圖裡所有的（都道府縣, 市區町村），第一次用到時讀。"""
        if self._admin_cache is None:
            self._admin_cache = self.conn.execute(
                f"SELECT DISTINCT pref, municipality FROM {TABLE} WHERE pref <> '' ORDER BY pref, municipality"
            ).fetchall()
        return self._admin_cache


def _candidate(r, match: str = "", similarity: float = 0.0) -> Candidate:
    source, source_id, name, category, kind, pref, municipality, address, lat, lon, boundary = r
    return Candidate(source=source, source_id=source_id, name=name, category=category, kind=kind, pref=pref,
                     municipality=municipality, address=address, lat=lat, lon=lon,
                     boundary=json.loads(boundary) if boundary else None, match=match,
                     similarity=round(float(similarity or 0.0), 4))


def _clean(name: str | None) -> str:
    return _DROP.sub("", unicodedata.normalize("NFKC", name or ""))


def _similar(name: str, choices: list[str], n: int = 8) -> list[str]:
    """名稱相近的市區町村：互相包含的，再加上 difflib 覺得像的。"""
    key = city_key(name)
    contains = [c for c in choices if len(key) >= 2 and (key in c or city_key(c) in key)]
    close = difflib.get_close_matches(name, choices, n=n, cutoff=0.5)
    return list(dict.fromkeys(contains + close))[:n]
