"""建本機地圖 `data/geo/japan.duckdb`：OSM（Geofabrik 的 japan-latest.osm.pbf）＋ Overture places（日本）。

手動跑一次：

    brew install osmium-tool
    # 從 https://download.geofabrik.de/asia/japan.html 下載 japan-latest.osm.pbf
    PYTHONPATH=.:src .venv/bin/python scripts/geo/build_japan.py ~/Downloads/japan-latest.osm.pbf

步驟：
1. `osmium tags-filter` 留下所有有 `name` 的物件 → `osmium export` 成 GeoJSONSeq（只帶要用的 tag）→ DuckDB spatial `ST_Read`。
2. Overture places：直接讀 S3 上的 parquet，只取日本的 bbox 而且 `addresses[0].country = 'JP'`。
3. 用 OSM 的行政界（`ADMIN_LEVEL_PREF`、`ADMIN_LEVEL_MUNICIPALITY`）判斷每一筆的代表點落在哪個縣、哪個市區町村。
   會印出各 admin_level 的筆數和例子；層級跟預期不同就改這兩個常數重跑。
4. 寫成 `v4.extract.local_map.SCHEMA` 的表；先寫到暫存檔，成功才換掉原本的檔案。
5. 印 OSM、Overture 各幾筆，用幾個名稱試搜尋；任何一個連 1 筆都沒有，結束代碼是 1。

中間檔放在輸出檔旁邊的 `work/`，跑完不刪（重跑時可以用 `--reuse` 跳過 osmium）。
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

OVERTURE_RELEASE = "2026-11-18.0"   # 寫死；換版本時改這裡（https://docs.overturemaps.org/release-calendar/）
OVERTURE_PLACES = f"s3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}/theme=places/type=place/*"
JAPAN_BBOX = (122.0, 20.0, 154.0, 46.0)   # xmin, ymin, xmax, ymax

ADMIN_LEVEL_PREF = "4"           # 都道府県
ADMIN_LEVEL_MUNICIPALITY = "7"   # 市区町村（東京 23 区也是 7；政令市的区是 8）

SPOT = "景點"
AREA = "區域"

NAME_TAGS = ("name", "name:ja", "alt_name", "official_name", "short_name")
# 決定 OSM 類別（key=value）時依序看的 key
CATEGORY_KEYS = (
    "place", "boundary", "amenity", "shop", "tourism", "leisure", "historic", "railway", "public_transport",
    "aeroway", "station", "office", "craft", "healthcare", "sport", "man_made", "natural", "waterway",
    "landuse", "building", "highway", "bridge", "military", "emergency", "power",
)
ADDR_TAGS = ("addr:full", "addr:province", "addr:city", "addr:suburb", "addr:quarter", "addr:neighbourhood",
             "addr:block_number", "addr:housenumber")
EXTRA_TAGS = ("admin_level",)

CHECK_NAMES = ("浅草寺", "新宿駅", "嵐山", "アメ横商店街", "東京ディズニーランド")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="建本機地圖 data/geo/japan.duckdb（OSM＋Overture）")
    ap.add_argument("pbf", type=Path, help="Geofabrik 的 japan-latest.osm.pbf")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"輸出路徑（預設 {DEFAULT_OUT}）")
    ap.add_argument("--reuse", action="store_true", help="work/ 裡已經有 osmium 的輸出就直接用")
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
        con.create_function("normalize_names", _normalize_names, ["VARCHAR[]"], "VARCHAR[]", null_handling="special")
        load_osm(con, geojson)
        report_admin_levels(con)
        load_admin(con)
        if a.skip_overture:
            con.execute("CREATE TABLE overture_raw AS SELECT NULL::VARCHAR AS id WHERE false")
        else:
            load_overture(con)
        build_table(con, overture=not a.skip_overture)
        counts = con.execute(f"SELECT source, count(*) FROM {TABLE} GROUP BY source ORDER BY source").fetchall()
        for t in ("osm_raw", "osm_feature", "admin", "overture_raw"):
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
    con.execute(f"""
        CREATE TABLE osm_feature AS
        SELECT "@type" || '/' || CAST("@id" AS VARCHAR) AS source_id,
               COALESCE({col('name:ja')}, {col('name')}) AS name,
               list_filter([{col('name')}, {col('name:ja')}, {col('official_name')}, {col('short_name')}]
                           || {alt}, lambda x: x IS NOT NULL AND trim(x) <> '') AS names,
               {category} AS category,
               CASE WHEN {col('place')} IS NOT NULL OR {col('boundary')} = 'administrative'
                    THEN '{AREA}' ELSE '{SPOT}' END AS kind,
               {col('boundary')} AS boundary_tag,
               {col('admin_level')} AS admin_level,
               {address} AS address,
               geom
        FROM osm_raw
        WHERE COALESCE({col('name:ja')}, {col('name')}) IS NOT NULL
        -- 封閉的 way 會同時匯出成線和多邊形，同一個 id 只留一筆（多邊形優先）
        QUALIFY row_number() OVER (
            PARTITION BY source_id
            ORDER BY ST_GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON') DESC) = 1
    """)
    n = con.execute("SELECT count(*) FROM osm_feature").fetchone()[0]
    print(f"  OSM 有名稱的物件 {n:,} 筆（{time.monotonic() - started:.0f} 秒）", flush=True)


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

def load_overture(con) -> None:
    started = time.monotonic()
    print(f"讀 Overture places（{OVERTURE_RELEASE}）……", flush=True)
    xmin, ymin, xmax, ymax = JAPAN_BBOX
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region = 'us-west-2';")
    con.execute(f"""
        CREATE TABLE overture_raw AS
        SELECT id,
               names.primary AS name,
               list_filter([names.primary, names.common['ja']], lambda x: x IS NOT NULL AND trim(x) <> '') AS names,
               COALESCE(taxonomy.primary, basic_category, '') AS category,
               COALESCE(addresses[1].freeform, '') AS address,
               geometry AS geom
        FROM read_parquet('{OVERTURE_PLACES}', hive_partitioning = 1)
        WHERE bbox.xmin >= {xmin} AND bbox.xmax <= {xmax} AND bbox.ymin >= {ymin} AND bbox.ymax <= {ymax}
          AND addresses[1].country = 'JP'
          AND names.primary IS NOT NULL
    """)
    n = con.execute("SELECT count(*) FROM overture_raw").fetchone()[0]
    print(f"  Overture 日本 {n:,} 筆（{time.monotonic() - started:.0f} 秒）", flush=True)


# ---------- 合成一張表 ----------

def build_table(con, overture: bool) -> None:
    started = time.monotonic()
    print("判斷縣、市區町村，寫成一張表……", flush=True)
    union = f"""
        SELECT 'osm' AS source, source_id, name, names, category, kind, address,
               ST_PointOnSurface(geom) AS pt,
               CASE WHEN kind = '{AREA}' AND ST_GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')
                    THEN ST_AsGeoJSON(geom) END AS boundary
        FROM osm_feature
    """
    if overture:
        union += f"""
        UNION ALL
        SELECT 'overture', id, name, names, category, '{SPOT}', address, ST_PointOnSurface(geom), NULL
        FROM overture_raw
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
               ST_Y(i.pt), ST_X(i.pt), i.boundary
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
