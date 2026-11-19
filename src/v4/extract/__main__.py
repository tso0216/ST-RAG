"""命令列：解析參數、呼叫 `pipeline`、印出執行結果，有地點要使用者決定時以非 0 結束。

    PYTHONPATH=.:src python -m v4.extract data/ptt_ask/itinerary --db data/v4.db
"""
import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path

from . import config, geocode_log, unresolved
from .local_map import MissingLocalMap
from .pipeline import run


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m v4.extract", description="從 ptt 文章抽出點、線、段，寫進資料庫")
    ap.add_argument("sources", nargs="+", type=Path, metavar="資料夾或檔案", help="要抽的文章 JSON，或裝著它們的資料夾")
    ap.add_argument("--db", type=Path, default=None,
                    help=f"資料庫路徑（預設 {config.DB_PATH}）；不存在就照 src/v4/schema.sql 建")
    ap.add_argument("--jobs", type=int, default=1, help="同時做 llm 解析的篇數（預設 1）；地點解析和寫入一律逐篇做")
    ap.add_argument("--limit", type=int, default=None, help="只做前幾篇，測試用")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="印出每一步的過程：檢查出的問題和第幾次重試、被拿掉的資料、每個地點查本機地圖、上網查地址和 llm 挑選的經過")
    a = ap.parse_args(argv)
    if a.verbose:
        logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)
        logging.getLogger("v4").setLevel(logging.INFO)

    try:
        r = asyncio.run(run(a.sources, db_path=a.db, jobs=a.jobs, limit=a.limit))
    except MissingLocalMap as e:
        print(str(e), file=sys.stderr)
        return 1

    print(f"\n寫入：{len(r.written)} 篇")
    print(f"略過：{len(r.skipped)} 篇")
    for reason, count in Counter(reason for _, reason in r.skipped).items():
        print(f"  {reason}：{count} 篇")
    for name, reason in r.skipped:
        if reason != "資料庫已有":
            print(f"  {name}（{reason}）")
    print(f"等地點答案：{len(r.waiting)} 篇")
    print(f"不收入：{r.dropped} 筆")
    if r.geocode_log and r.geocode_log.exists():
        print()
        for line in geocode_log.summary_lines(r.geocode_log, _shown(r.geocode_log)):
            print(line)
    if r.unresolved:
        print(f"\n要你決定的地點（{len(r.unresolved)} 個）：", file=sys.stderr)
        unresolved.print_list(r.unresolved, file=sys.stderr)
        print(f"\n請在 {config.UNRESOLVED_PATH} 的 answer 欄填 OSM 網址、日文名稱、座標或「{unresolved.SKIP}」，然後重跑。",
              file=sys.stderr)
    if r.error:
        print(f"\n錯誤，整批停下：{r.error}", file=sys.stderr)
        print(f"還沒做完：{len(r.unfinished)} 篇（已經寫入的保留，重跑會略過）", file=sys.stderr)
    return 1 if r.error or r.unresolved else 0


def _shown(path: Path) -> str:
    """log 檔在專案裡就印相對路徑（data/logs/geocode_xxx.jsonl），不在就印完整路徑。"""
    try:
        return str(path.resolve().relative_to(config.ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    sys.exit(main())
