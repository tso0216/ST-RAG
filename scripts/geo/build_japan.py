"""建本機地圖 `data/geo/japan.duckdb`：OSM（Geofabrik 的 japan-latest.osm.pbf）＋ Overture places（日本）。

手動跑一次：

    brew install osmium-tool
    # 從 https://download.geofabrik.de/asia/japan.html 下載 japan-latest.osm.pbf
    # 從 https://nominatim.org/data/wikimedia-importance.csv.gz 下載維基百科重要度，放到 data/geo/src/
    PYTHONPATH=.:src .venv/bin/python scripts/geo/build_japan.py ~/Downloads/japan-latest.osm.pbf

步驟：
1. `osmium tags-filter` 留下所有有 `name` 的物件 → `osmium export` 成 GeoJSONSeq（只帶要用的 tag）→ DuckDB spatial `ST_Read`。
   車站底下的月台、停車位置（`railway=stop`／`platform` 這類）不收：跟車站同名，會把同名的地點擠出候選。
2. 維基百科重要度：OSM 的 `wikidata`（沒有再用 `wikipedia`）對 Nominatim 的 wikimedia-importance，存成 `importance`。
3. Overture places：直接讀 S3 上的 parquet，只取日本的 bbox 而且 `addresses[0].country = 'JP'`；
   已歇業（`operating_status = permanently_closed`）的不收。confidence 照存，但不拿來過濾：
   位置錯的記錄 confidence 也可能很高（千歲的「成田空港」航廈 0.92）。
4. 用 OSM 的行政界（`ADMIN_LEVEL_PREF`、`ADMIN_LEVEL_MUNICIPALITY`）判斷每一筆的代表點落在哪個縣、哪個市區町村。
   會印出各 admin_level 的筆數和例子；層級跟預期不同就改這兩個常數重跑。
5. 寫成 `v4.extract.local_map.SCHEMA` 的表；先寫到暫存檔，成功才換掉原本的檔案。
6. 印 OSM、Overture 各幾筆，用幾個名稱試搜尋；任何一個連 1 筆都沒有，結束代碼是 1。

中間檔放在輸出檔旁邊的 `work/`，跑完不刪（重跑時可以用 `--reuse` 跳過 osmium 和下載 Overture）。
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from v4.extract.local_map import SCHEMA, TABLE, LocalMap, normalize  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "geo" / "japan.duckdb"
# Nominatim 依維基百科連結算的重要度（https://nominatim.org/data/wikimedia-importance.csv.gz）
DEFAULT_IMPORTANCE = ROOT / "data" / "geo" / "src" / "wikimedia-importance.csv.gz"

OVERTURE_RELEASE = "2026-11-18.0"   # 寫死；換版本時改這裡（https://docs.overturemaps.org/release-calendar/）
OVERTURE_PLACES = f"s3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}/theme=places/type=place/*"
JAPAN_BBOX = (122.0, 20.0, 154.0, 46.0)   # xmin, ymin, xmax, ymax
OVERTURE_NAME_LANGS = ("ja", "en", "zh", "zh-Hant", "zh-TW")   # names.common 裡也拿來比對的語言

ADMIN_LEVEL_PREF = "4"           # 都道府県
ADMIN_LEVEL_MUNICIPALITY = "7"   # 市区町村（東京 23 区也是 7；政令市的区是 8）

SPOT = "景點"
AREA = "區域"

# name、name:ja、alt_name 以外，也拿來比對的名稱（iata：機場代碼 NRT）
OTHER_NAME_TAGS = ("name:en", "name:zh", "name:zh-Hant", "name:zh-TW", "official_name", "short_name", "iata")
NAME_TAGS = ("name", "name:ja", "alt_name", *OTHER_NAME_TAGS)
# 決定 OSM 類別（key=value）時依序看的 key
CATEGORY_KEYS = (
    "place", "boundary", "amenity", "shop", "tourism", "leisure", "historic", "railway", "public_transport",
    "aeroway", "station", "office", "craft", "healthcare", "sport", "man_made", "natural", "waterway",
    "landuse", "building", "highway", "bridge", "military", "emergency", "power",
)
ADDR_TAGS = ("addr:full", "addr:province", "addr:city", "addr:suburb", "addr:quarter", "addr:neighbourhood",
             "addr:block_number", "addr:housenumber")
EXTRA_TAGS = ("admin_level", "wikidata", "wikipedia")

CHECK_NAMES = ("浅草寺", "新宿駅", "嵐山", "アメ横商店街", "東京ディズニーランド")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="建本機地圖 data/geo/japan.duckdb（OSM＋Overture）")
    ap.add_argument("pbf", type=Path, help="Geofabrik 的 japan-latest.osm.pbf")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"輸出路徑（預設 {DEFAULT_OUT}）")
    ap.add_argument("--importance", type=Path, default=DEFAULT_IMPORTANCE,
                    help=f"Nominatim 的 wikimedia-importance.csv.gz（預設 {DEFAULT_IMPORTANCE}）")
    ap.add_argument("--reuse", action="store_true", help="work/ 裡已經有 osmium 的輸出、Overture 的 parquet 就直接用")
    ap.add_argument("--skip-overture", action="store_true", help="不讀 Overture（離線測試用）")
    a = ap.parse_args(argv)

    out: Path = a.out
    work = out.parent / "work"
    work.mkdir(parents=True, exist_ok=True)
    geojson = work / "named.geojsonseq"
    if not (a.reuse and geojson.exists()):
        osmium(a.pbf, work, geojson)

    tmp = out.with_suffix(".building.duckdb")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        con.execute("INSTALL spatial; LOAD spatial;")
        con.execute("SET geometry_always_xy = true")   # 座標是（經度, 緯度），ST_Area_Spheroid 要知道
        con.create_function("normalize_names", _normalize_names, ["VARCHAR[]"], "VARCHAR[]", null_handling="special")
        load_osm(con, geojson)
        load_importance(con, a.importance)
        report_admin_levels(con)
        load_admin(con)
        if a.skip_overture:
            con.execute("CREATE TABLE overture_raw AS SELECT NULL::VARCHAR AS id WHERE false")
        else:
            load_overture(con, work / f"overture-{OVERTURE_RELEASE}.parquet", a.reuse)
        build_table(con, overture=not a.skip_overture)
        counts = con.execute(f"SELECT source, count(*) FROM {TABLE} GROUP BY source ORDER BY source").fetchall()
        for t in ("osm_raw", "osm_feature", "osm_importance", "admin", "overture_raw"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
    finally:
        con.close()
    shutil.move(str(tmp), str(out))

    print("\n筆數：" + "、".join(f"{s} {n:,}" for s, n in counts))
    return check(out)


# ---------- OSM ----------

def osmium(pbf: Path, work: Path, geojson: Path) -> None:
    named = work / "named.osm.pbf"
    config = work / "export-config.json"
    config.write_text(json.dumps({
        "attributes": {"type": True, "id": True},
        "include_tags": [*NAME_TAGS, *CATEGORY_KEYS, *ADDR_TAGS, *EXTRA_TAGS],
    }))
    _run(["osmium", "tags-filter", str(pbf), "nwr/name", "-o", str(named), "--overwrite"])
    _run(["osmium", "export", str(named), "-c", str(config), "-f", "geojsonseq", "-o", str(geojson),
          "--overwrite"])


def load_osm(con, geojson: Path) -> None:
    started = time.monotonic()
    print("讀 OSM（ST_Read）……", flush=True)
    con.execute(f"CREATE TABLE osm_raw AS SELECT * FROM ST_Read('{geojson}')")
    columns = {r[0] for r in con.execute("DESCRIBE osm_raw").fetchall()}

    def col(tag: str) -> str:
        return f'NULLIF(TRIM("{tag}"), \'\')' if tag in columns else "NULL::VARCHAR"

    category = "COALESCE(" + ", ".join(
        f"""CASE WHEN {col(k)} IS NOT NULL THEN '{k}=' || {col(k)} END""" for k in CATEGORY_KEYS) + ", '')"
    addr_parts = ", ".join(col(t) for t in ADDR_TAGS[1:])
    address = f"COALESCE({col('addr:full')}, NULLIF(concat_ws('', {addr_parts}), ''), '')"
    alt = f"COALESCE(string_split({col('alt_name')}, ';'), [])"
    other_names = ", ".join(col(t) for t in OTHER_NAME_TAGS)
    con.execute(f"""
        CREATE TABLE osm_feature AS
        SELECT "@type" || '/' || CAST("@id" AS VARCHAR) AS source_id,
               COALESCE({col('name:ja')}, {col('name')}) AS name,
               list_filter([{col('name')}, {col('name:ja')}, {other_names}]
                           || {alt}, lambda x: x IS NOT NULL AND trim(x) <> '') AS names,
               {category} AS category,
               CASE WHEN {col('place')} IS NOT NULL OR {col('boundary')} = 'administrative'
                    THEN '{AREA}' ELSE '{SPOT}' END AS kind,
               {col('boundary')} AS boundary_tag,
               {col('admin_level')} AS admin_level,
               {address} AS address,
               NULLIF(split_part({col('wikidata')}, ';', 1), '') AS wikidata,
               {col('wikipedia')} AS wikipedia,
               geom
        FROM osm_raw
        WHERE COALESCE({col('name:ja')}, {col('name')}) IS NOT NULL
          -- 車站底下的月台、停車位置：跟車站同名，會把車站和同名的地點擠出候選
          AND NOT COALESCE({col('railway')} IN ('stop', 'platform')
                           OR ({col('railway')} IS NULL AND {col('public_transport')} = 'stop_position')
                           OR ({col('railway')} IS NULL AND {col('public_transport')} = 'platform'
                               AND {col('highway')} IS DISTINCT FROM 'bus_stop'), false)
        -- 封閉的 way 會同時匯出成線和多邊形，同一個 id 只留一筆（多邊形優先）
        QUALIFY row_number() OVER (
            PARTITION BY source_id
            ORDER BY ST_GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON') DESC) = 1
    """)
    n = con.execute("SELECT count(*) FROM osm_feature").fetchone()[0]
    print(f"  OSM 有名稱的物件 {n:,} 筆（{time.monotonic() - started:.0f} 秒）", flush=True)


def load_importance(con, path: Path) -> None:
    """OSM 每筆的維基百科重要度：先用 wikidata tag 對，對不到再用 wikipedia tag（`ja:成田国際空港`）對語言和條目名稱。
    同一個條目各語言版本的重要度不同，取最高的。"""
    started = time.monotonic()
    print(f"讀維基百科重要度（{path}）……", flush=True)
    con.execute(f"""
        CREATE TEMP VIEW wiki AS
        SELECT * FROM read_csv('{path}', delim = '\t', header = true, quote = '', escape = '',
                               columns = {{'language': 'VARCHAR', 'type': 'VARCHAR', 'title': 'VARCHAR',
                                          'importance': 'DOUBLE', 'wikidata_id': 'VARCHAR'}})
    """)
    con.execute("""
        CREATE TABLE osm_importance AS
        WITH by_qid AS (
            SELECT wikidata_id, max(importance) AS importance FROM wiki
            WHERE wikidata_id IN (SELECT wikidata FROM osm_feature WHERE wikidata IS NOT NULL)
            GROUP BY wikidata_id
        ), tagged AS (
            SELECT source_id, split_part(wikipedia, ':', 1) AS language,
                   replace(substr(wikipedia, strpos(wikipedia, ':') + 1), ' ', '_') AS title
            FROM osm_feature WHERE wikipedia LIKE '%:%'
        ), by_title AS (
            SELECT t.source_id, max(w.importance) AS importance
            FROM tagged t JOIN wiki w ON w.language = t.language AND w.title = t.title
            GROUP BY t.source_id
        )
        SELECT f.source_id, COALESCE(q.importance, t.importance) AS importance
        FROM osm_feature f
        LEFT JOIN by_qid q ON q.wikidata_id = f.wikidata
        LEFT JOIN by_title t ON t.source_id = f.source_id
        WHERE COALESCE(q.importance, t.importance) IS NOT NULL
    """)
    n = con.execute("SELECT count(*) FROM osm_importance").fetchone()[0]
    print(f"  有維基百科重要度的 OSM 物件 {n:,} 筆（{time.monotonic() - started:.0f} 秒）", flush=True)


def report_admin_levels(con) -> None:
    """印出行政界各層級的筆數和例子，用來確認 ADMIN_LEVEL_PREF、ADMIN_LEVEL_MUNICIPALITY 對不對。"""
    rows = con.execute("""
        SELECT admin_level, count(*) AS n, list(name ORDER BY name)[1:5] AS examples
        FROM osm_feature
        WHERE boundary_tag = 'administrative' AND ST_GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')
        GROUP BY admin_level ORDER BY TRY_CAST(admin_level AS INTEGER)
    """).fetchall()
    print("行政界（多邊形）各層級：")
    for level, n, examples in rows:
        mark = {ADMIN_LEVEL_PREF: "  ← 都道府縣", ADMIN_LEVEL_MUNICIPALITY: "  ← 市區町村"}.get(level, "")
        print(f"  admin_level={level}：{n:,} 筆，例如 {'、'.join(examples)}{mark}")
    pref = next((n for level, n, _ in rows if level == ADMIN_LEVEL_PREF), 0)
    if pref != 47:
        print(f"  ※ admin_level={ADMIN_LEVEL_PREF} 有 {pref} 筆，不是 47 個都道府縣，請確認 ADMIN_LEVEL_PREF")


def load_admin(con) -> None:
    con.execute(f"""
        CREATE TABLE admin AS
        SELECT admin_level, name, geom FROM osm_feature
        WHERE boundary_tag = 'administrative'
          AND admin_level IN ('{ADMIN_LEVEL_PREF}', '{ADMIN_LEVEL_MUNICIPALITY}')
          AND ST_GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')
    """)


# ---------- Overture ----------

def load_overture(con, cache: Path, reuse: bool) -> None:
    """讀 Overture 日本的 places。從 S3 讀要好幾分鐘，所以存一份 parquet 到 work/，`--reuse` 時直接讀它。"""
    started = time.monotonic()
    if reuse and cache.exists():
        print(f"讀 Overture places（{cache}）……", flush=True)
        con.execute(f"CREATE TABLE overture_raw AS SELECT * FROM read_parquet('{cache}')")
    else:
        print(f"讀 Overture places（{OVERTURE_RELEASE}）……", flush=True)
        _download_overture(con)
        con.execute(f"COPY overture_raw TO '{cache}' (FORMAT parquet)")
    n = con.execute("SELECT count(*) FROM overture_raw").fetchone()[0]
    print(f"  Overture 日本 {n:,} 筆（{time.monotonic() - started:.0f} 秒）", flush=True)


def _download_overture(con) -> None:
    xmin, ymin, xmax, ymax = JAPAN_BBOX
    common = ", ".join(f"names.common['{lang}']" for lang in OVERTURE_NAME_LANGS)
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region = 'us-west-2';")
    con.execute(f"""
        CREATE TABLE overture_raw AS
        SELECT id,
               names.primary AS name,
               list_filter([names.primary, {common}], lambda x: x IS NOT NULL AND trim(x) <> '') AS names,
               COALESCE(taxonomy.primary, basic_category, '') AS category,
               COALESCE(addresses[1].freeform, '') AS address,
               confidence,
               operating_status,
               geometry AS geom
        FROM read_parquet('{OVERTURE_PLACES}', hive_partitioning = 1)
        WHERE bbox.xmin >= {xmin} AND bbox.xmax <= {xmax} AND bbox.ymin >= {ymin} AND bbox.ymax <= {ymax}
          AND addresses[1].country = 'JP'
          AND names.primary IS NOT NULL
    """)


# ---------- 合成一張表 ----------

def build_table(con, overture: bool) -> None:
    started = time.monotonic()
    print("判斷縣、市區町村，寫成一張表……", flush=True)
    union = f"""
        SELECT 'osm' AS source, f.source_id, name, names, category, kind, address,
               ST_PointOnSurface(geom) AS pt,
               CASE WHEN kind = '{AREA}' AND ST_GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')
                    THEN ST_AsGeoJSON(geom) END AS boundary,
               wikidata, COALESCE(imp.importance, 0) AS importance,
               CASE WHEN ST_GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')
                    THEN ST_Area_Spheroid(geom) ELSE 0 END AS area,
               NULL::DOUBLE AS confidence
        FROM osm_feature f LEFT JOIN osm_importance imp USING (source_id)
    """
    if overture:
        union += f"""
        UNION ALL
        -- Overture 的座標標成 OGC:CRS84，OSM 的是 EPSG:4326；兩者都是經度、緯度，只是標記不同，統一才能合併
        SELECT 'overture', id, name, names, category, '{SPOT}', address,
               ST_PointOnSurface(ST_SetCRS(geom, 'EPSG:4326')), NULL, NULL, 0, 0, confidence
        FROM overture_raw
        WHERE COALESCE(operating_status, '') <> 'permanently_closed'
        """
    con.execute(f"CREATE TEMP TABLE item AS SELECT row_number() OVER () AS id, * FROM ({union})")
    con.execute(SCHEMA)
    con.execute(f"""
        INSERT INTO {TABLE}
        WITH located AS (
            SELECT i.id, a.admin_level, a.name AS admin_name
            FROM item i JOIN admin a ON ST_Contains(a.geom, i.pt)
        ), area AS (
            SELECT id,
                   min(admin_name) FILTER (WHERE admin_level = '{ADMIN_LEVEL_PREF}') AS pref,
                   min(admin_name) FILTER (WHERE admin_level = '{ADMIN_LEVEL_MUNICIPALITY}') AS municipality
            FROM located GROUP BY id
        )
        SELECT i.id, i.source, i.source_id, i.name, normalize_names(i.names), i.category, i.kind,
               COALESCE(area.pref, ''), COALESCE(area.municipality, ''), i.address,
               ST_Y(i.pt), ST_X(i.pt), i.boundary, i.wikidata, i.importance, i.area, i.confidence
        FROM item i LEFT JOIN area USING (id)
        WHERE i.pt IS NOT NULL
    """)
    con.execute("DROP TABLE item")
    print(f"  完成（{time.monotonic() - started:.0f} 秒）", flush=True)


def _normalize_names(names: list[str] | None) -> list[str]:
    return list(dict.fromkeys(n for n in map(normalize, names or []) if n))


# ---------- 建完的檢查 ----------

def check(path: Path) -> int:
    local_map = LocalMap(path)
    missing = []
    try:
        for name in CHECK_NAMES:
            found = local_map.search(name)
            print(f"\n「{name}」：{len(found)} 筆")
            for c in found[:3]:
                print(f"  {c.name}｜{c.source} {c.source_id}｜{c.category}｜{c.pref} {c.municipality}｜{c.match}"
                      f"｜{c.lat:.5f},{c.lon:.5f}")
            if not found:
                missing.append(name)
    finally:
        local_map.close()
    if missing:
        print(f"\n查不到：{'、'.join(missing)}", file=sys.stderr)
        return 1
    return 0


def _run(cmd: list[str]) -> None:
    print("$ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    sys.exit(main())
