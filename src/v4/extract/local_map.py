"""本機地圖：在 `data/geo/japan.duckdb`（OSM＋Overture，`scripts/geo/build_japan.py` 建）裡用名稱模糊搜尋、用來源 id 查一筆。

唯讀開啟，不打網路。表結構是 `SCHEMA`（建置腳本和測試也用它）。

比對：查詢的名稱和資料裡的名稱都先 `normalize`；一筆地點拿它每個比對用名稱跟查詢比，取最好的結果：
完全相同 → 包含（地圖上的名稱包含查詢的名稱，查詢至少 2 個字）→ 模糊（jaro_winkler_similarity ≥ 門檻）。
排序：同城市 → 比對結果 → 相似度高 → 名稱短。OSM、Overture 各取前 N 筆，合起來照同樣的順序排。
"""
import json
import re
import unicodedata
from pathlib import Path

import duckdb

from . import config
from .data_schema import CONTAINS, EXACT, FUZZY, Candidate

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


def normalize(name: str | None) -> str:
    """名稱正規化：NFKC → 英文轉小寫 → 去掉空白（含全形）、`・`、`-`、`‐`、`－`（`ー` 保留）→ `ヶ`、`ヵ` 轉 `ケ`、`カ`。"""
    s = unicodedata.normalize("NFKC", name or "")
    s = "".join(ch.lower() if ch.isascii() else ch for ch in s)
    return _DROP.sub("", s).translate(_KANA)


def city_key(name: str | None) -> str:
    """判斷同城市用的寫法：去掉結尾的「都、府、県、市、区、町、村」。

    去掉後只剩一個字時不去（「京都」不變成「京」，這樣「京都」和「京都府」、「京都市」才對得上）。
    """
    s = _DROP.sub("", unicodedata.normalize("NFKC", name or ""))
    return s[:-1] if len(s) >= 3 and s.endswith(_SUFFIXES) else s


def _city_key_sql(column: str) -> str:
    """`city_key` 的 SQL 版本，用在縣名、市區町村名的欄位上。"""
    suffixes = ", ".join(f"'{s}'" for s in _SUFFIXES)
    return (f"CASE WHEN length({column}) >= 3 AND right({column}, 1) IN ({suffixes})"
            f" THEN left({column}, length({column}) - 1) ELSE {column} END")


_COLUMNS = "source, source_id, name, category, kind, pref, municipality, address, lat, lon, boundary"

_SEARCH = f"""
WITH names AS (
    SELECT id, unnest(match_names) AS n FROM {TABLE}
), best AS (
    SELECT id,
           min(CASE WHEN n = $q THEN 0 WHEN length($q) >= 2 AND contains(n, $q) THEN 1 ELSE 2 END) AS match_rank,
           max(jaro_winkler_similarity(n, $q)) AS similarity
    FROM names
    GROUP BY id
), hit AS (
    SELECT p.*, b.match_rank, b.similarity,
           ($city <> '' AND ($city = {_city_key_sql('p.pref')} OR $city = {_city_key_sql('p.municipality')}))
               AS same_city
    FROM best b JOIN {TABLE} p USING (id)
    WHERE b.match_rank < 2 OR b.similarity >= $threshold
), ranked AS (
    SELECT *, row_number() OVER (
        PARTITION BY source ORDER BY same_city DESC, match_rank, similarity DESC, length(name), id) AS n
    FROM hit
)
SELECT {_COLUMNS}, match_rank, similarity
FROM ranked
WHERE n <= $per_source
ORDER BY same_city DESC, match_rank, similarity DESC, length(name), source, id
"""


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

    def close(self) -> None:
        self.conn.close()

    def search(self, name_ja: str, city: str = "") -> list[Candidate]:
        """用一個日文名稱（和城市）模糊搜尋全日本，回傳最多 每個來源 N 筆 × 2 的候選，不合併去重。"""
        q = normalize(name_ja)
        if not q:
            return []
        rows = self.conn.execute(_SEARCH, {"q": q, "city": city_key(city), "threshold": self.threshold,
                                           "per_source": self.per_source}).fetchall()
        return [_candidate(r[:11], _RANK[r[11]], r[12]) for r in rows]

    def lookup(self, source: str, source_id: str) -> Candidate | None:
        """用來源 id 查一筆；查不到是 None。"""
        r = self.conn.execute(f"SELECT {_COLUMNS} FROM {TABLE} WHERE source = ? AND source_id = ? LIMIT 1",
                              (source, source_id)).fetchone()
        return _candidate(r) if r else None


def _candidate(r, match: str = "", similarity: float = 0.0) -> Candidate:
    source, source_id, name, category, kind, pref, municipality, address, lat, lon, boundary = r
    return Candidate(source=source, source_id=source_id, name=name, category=category, kind=kind, pref=pref,
                     municipality=municipality, address=address, lat=lat, lon=lon,
                     boundary=json.loads(boundary) if boundary else None, match=match,
                     similarity=round(float(similarity or 0.0), 4))
