"""本機地圖：在 `data/geo/japan.duckdb`（OSM＋Overture，`scripts/geo/build_japan.py` 建）裡用名稱模糊搜尋。

唯讀開啟，不打網路。表結構是 `SCHEMA`（建置腳本和測試也用它）。

範圍：搜尋時可以限定範圍（`Area`）：都道府縣、市區町村，或以某個點為中心的圓；沒給就是全日本。
llm 寫的縣市名稱先用 `admin_area` 對到地圖裡的寫法，對不到丟 `AreaError`（訊息是給 llm 看的）。

比對：查詢的名稱（可以好幾個）和資料裡的名稱都先 `normalize`；一筆地點拿它每個比對用名稱跟每個查詢名稱比，取最好的結果：
完全相同 → 包含（地圖上的名稱包含查詢的名稱，查詢至少 2 個字）→ 模糊（jaro_winkler_similarity ≥ 門檻）。
範圍內只要有一筆完全相同或包含，模糊的就都不列。

排序：照 Nominatim（`nominatim_api/search/geocoder.py`、`results.py`）的分數，越小越前面：

    分數 = 名稱扣分 − 重要度 ＋ 種類扣分

- 名稱扣分：完全相同 0；包含 `CONTAINS_PENALTY`（Nominatim 只靠部分字詞對上時的扣分：部分詞 0.3＋退回比對 0.2）。
  「包含」分不出是同一個地點的全名（恩賜上野動物園）還是別的地點（浅草橋），所以要扣分，重要度高很多才排得上來。
- 重要度：維基百科重要度（Nominatim 的 wikimedia-importance），沒有維基條目是 0。
- 種類扣分：跟要找的種類（景點／區域）不一樣加 `KIND_PENALTY`；空字串是不分。同名的車站常比同名的地區有名，
  找區域時不這樣會拿到車站。Nominatim 是直接過濾，這裡只扣分：llm 會判錯，而且有些地方 OSM 沒有區域（嵐山）。
- 同分：面積大的優先（Nominatim 比外框面積；這裡是多邊形面積，點和線是 0）→ 離圓心近的優先（範圍是圓時）
  → 照來源、來源 id 固定順序。

跟 Nominatim 不同的地方：
- 模糊比對 Nominatim 沒有；這裡範圍內完全沒有完全相同或包含時才用，照相似度高低排，再照分數。
- Nominatim 會丟掉比第一名差 0.5 以上的；這裡不丟，後面還有 llm 挑。
- Nominatim 給中心點時只看距離；這裡照樣看重要度，距離只在同分時比（重要度分得出哪筆是本尊、哪筆是重複的資料）。
- 沒有維基條目時 Nominatim 依地點層級給預設重要度（縣 0.29、市 0.19……店家 0）；這裡是 0。

同一個 wikidata 是同一個地點（車站的好幾個節點、行政界和它的 place 節點），只留排最前面的一筆。
OSM、Overture 一起排，取前 `config.CANDIDATES` 筆。
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
    boundary     VARCHAR,              -- 區域而且是多邊形才有（GeoJSON）
    wikidata     VARCHAR,              -- OSM 的 wikidata tag（Q123）；Overture 和沒有的是 NULL
    importance   DOUBLE  NOT NULL,     -- 維基百科重要度 0–1（Nominatim 的 wikimedia-importance）；沒有維基條目是 0
    area         DOUBLE  NOT NULL,     -- 多邊形的面積（平方公尺）；點和線是 0
    confidence   DOUBLE                -- Overture 的 confidence（0–1）；OSM 是 NULL
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


# 圓的範圍用的距離（公里）：緯度一度 111.195 公里，經度乘上 cos(緯度)。10 公里內跟球面距離差不到 1%。
_DISTANCE = "111.195 * sqrt(power(lat - $lat, 2) + power((lon - $lon) * cos(radians($lat)), 2))"

_WHERE = {
    "japan": "true",
    "admin": "pref = $pref AND ($municipality = '' OR municipality = $municipality)",
    "circle": f"lat BETWEEN $lat - $dlat AND $lat + $dlat AND lon BETWEEN $lon - $dlon AND $lon + $dlon "
              f"AND {_DISTANCE} <= $km",
}


# 分數的兩個扣分（見模組說明）
CONTAINS_PENALTY = 0.5
KIND_PENALTY = 0.5

_SCORE = f"""CASE WHEN b.match_rank = 0 THEN 0 ELSE {CONTAINS_PENALTY} END
           + CASE WHEN $kind <> '' AND p.kind <> $kind THEN {KIND_PENALTY} ELSE 0 END
           - p.importance"""


def _order(t: str = "") -> str:
    """排序：模糊的先照相似度 → 分數 → 面積大 → 離圓心近 → 來源、來源 id。t 是欄位前面的表名。"""
    return (f"CASE WHEN {t}match_rank = 2 THEN -{t}similarity ELSE 0 END, {t}score, {t}area DESC, "
            f"{t}distance, {t}source, {t}source_id")


def _search_sql(where: str, distance: str) -> str:
    # 先只帶排序要用的窄欄位排好、取前 N 筆，最後才接回名稱、地址、範圍（GeoJSON 很大）
    return f"""
WITH scope AS (
    SELECT id, match_names FROM {TABLE} WHERE {where}
), names AS (
    SELECT id, unnest(match_names) AS n FROM scope
), best AS (
    SELECT id,
           min(CASE WHEN list_contains($qs, n) THEN 0
                    WHEN list_bool_or([length(q) >= 2 AND contains(n, q) FOR q IN $qs]) THEN 1
                    ELSE 2 END) AS match_rank,
           max(list_max([jaro_winkler_similarity(n, q) FOR q IN $qs])) AS similarity
    FROM names
    GROUP BY id
), hit AS (
    SELECT p.id, p.source, p.source_id, p.wikidata, p.area, b.match_rank, b.similarity,
           {distance} AS distance, {_SCORE} AS score
    FROM best b JOIN {TABLE} p USING (id)
    WHERE b.match_rank < 2 OR b.similarity >= $threshold
), kept AS (
    SELECT * FROM hit
    QUALIFY match_rank < 2 OR NOT bool_or(match_rank < 2) OVER ()
), unique_place AS (
    SELECT * FROM kept
    QUALIFY row_number() OVER (PARTITION BY COALESCE(wikidata, source || ' ' || source_id) ORDER BY {_order()}) = 1
), top AS (
    SELECT * FROM unique_place ORDER BY {_order()} LIMIT $limit
)
SELECT t.source, t.source_id, p.name, p.category, p.kind, p.pref, p.municipality, p.address, p.lat, p.lon,
       p.boundary, p.importance, t.match_rank, t.similarity
FROM top t JOIN {TABLE} p USING (id)
ORDER BY {_order("t.")}
"""


_SEARCH = {"japan": _search_sql(_WHERE["japan"], "0"), "admin": _search_sql(_WHERE["admin"], "0"),
           "circle": _search_sql(_WHERE["circle"], _DISTANCE)}


class LocalMap:
    """唯讀開啟本機地圖。檔案不存在丟 `MissingLocalMap`。"""

    def __init__(self, path: Path | str | None = None, limit: int | None = None,
                 threshold: float | None = None):
        self.path = Path(path or config.LOCAL_MAP_PATH)
        if not self.path.exists():
            raise MissingLocalMap(f"找不到本機地圖 {self.path}，請先跑 scripts/geo/build_japan.py")
        self.limit = limit or config.CANDIDATES
        self.threshold = config.FUZZY_THRESHOLD if threshold is None else threshold
        self.conn = duckdb.connect(str(self.path), read_only=True)
        self._admin_cache: list[tuple[str, str]] | None = None

    def close(self) -> None:
        self.conn.close()

    def search(self, names: str | list[str], area: Area | None = None, kind: str = "") -> list[Candidate]:
        """用一個或幾個日文名稱，在範圍內（沒給是全日本）模糊搜尋，照模組說明的順序回傳最多 N 筆。

        kind：要找的種類（景點／區域），跟它一樣的排前面；空字串是不分。
        """
        qs = list(dict.fromkeys(q for q in (normalize(n) for n in ([names] if isinstance(names, str) else names)) if q))
        if not qs:
            return []
        area = area or Area()
        params = {"qs": qs, "threshold": self.threshold, "limit": self.limit, "kind": kind}
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
        return [_candidate(r[:12], _RANK[r[12]], r[13]) for r in rows]

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
    source, source_id, name, category, kind, pref, municipality, address, lat, lon, boundary, importance = r
    return Candidate(source=source, source_id=source_id, name=name, category=category, kind=kind, pref=pref,
                     municipality=municipality, address=address, lat=lat, lon=lon,
                     boundary=json.loads(boundary) if boundary else None, match=match,
                     similarity=round(float(similarity or 0.0), 4), importance=round(float(importance or 0.0), 3))


def _clean(name: str | None) -> str:
    return _DROP.sub("", unicodedata.normalize("NFKC", name or ""))


def _similar(name: str, choices: list[str], n: int = 8) -> list[str]:
    """名稱相近的市區町村：互相包含的，再加上 difflib 覺得像的。"""
    key = city_key(name)
    contains = [c for c in choices if len(key) >= 2 and (key in c or city_key(c) in key)]
    close = difflib.get_close_matches(name, choices, n=n, cutoff=0.5)
    return list(dict.fromkeys(contains + close))[:n]
