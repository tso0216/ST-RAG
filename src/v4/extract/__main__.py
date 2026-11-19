"""命令列：解析參數、呼叫 `pipeline`、印出執行結果、捨棄的地點和這次花的 llm token；整批停下時以非 0 結束。
按 Ctrl-C 中斷時印出到中斷為止花的 token。

    PYTHONPATH=.:src python -m v4.extract data/ptt_ask/itinerary --db data/v4.db
"""
import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path

from service import llm

from . import config, geocode_log, store
from .local_map import MissingLocalMap
from .pipeline import PARSE_DROPPED, count_text, duration, run


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

    with llm.track() as total:
        try:
            r = asyncio.run(run(a.sources, db_path=a.db, jobs=a.jobs, limit=a.limit))
        except MissingLocalMap as e:
            print(str(e), file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\n中斷（已經寫入的保留，重跑會略過；llm 解析完的存在 v4_pending，重跑接著做）", file=sys.stderr)
            for line in usage_lines(total):
                print(line)
            return 130

    print(f"\n寫入：{len(r.written)} 篇")
    print(f"略過：{len(r.skipped)} 篇")
    for reason, count in Counter(reason for _, reason in r.skipped).items():
        print(f"  {reason}：{count} 篇")
    for name, reason in r.skipped:
        if reason != "資料庫已有":
            print(f"  {name}（{reason}）")
    dropped = {k: r.dropped.get(k, 0) for k in (*store.DROPPED_KINDS, PARSE_DROPPED)}
    print(f"拿掉沒寫入：{sum(dropped.values())} 筆" + (f"（{count_text(dropped)}）" if any(dropped.values()) else ""))
    if r.geocode_log and r.geocode_log.exists():
        print()
        for line in geocode_log.summary_lines(r.geocode_log, _shown(r.geocode_log)):
            print(line)
        for line in geocode_log.dropped_lines(r.geocode_log):
            print(line)
    if r.error:
        print(f"\n錯誤，整批停下：{r.error}", file=sys.stderr)
        print(f"還沒做完：{len(r.unfinished)} 篇（已經寫入的保留，重跑會略過）", file=sys.stderr)
    print()
    for line in usage_lines(total, r.llm_usage):
        print(line)
    print(f"花費時間：{duration(r.seconds)}")
    return 1 if r.error else 0


def usage_lines(total: llm.Tally, steps: dict[str, llm.Tally] | None = None) -> list[str]:
    """這次 llm 花的 token：合計一行，有分步驟就每步一行。"""
    if total.calls == 0:
        return ["llm 用量：這次沒有呼叫 llm"]
    lines = [f"llm 用量：共 {total.calls} 次，{_tokens(total)}",
             f"  照 API 定價約 US${total.cost_usd:.2f}（走訂閱額度不會真的收這筆）"]
    for step, tally in (steps or {}).items():
        lines.append(f"  {step}：{tally.calls} 次，{_tokens(tally)}" if tally.calls else f"  {step}：0 次")
    return lines


def _tokens(t: llm.Tally) -> str:
    cached = f"（其中快取讀取 {t.cache_read_tokens:,}）" if t.cache_read_tokens else ""
    return f"輸入 {t.input_tokens:,} token{cached}、輸出 {t.output_tokens:,} token"


def _shown(path: Path) -> str:
    """log 檔在專案裡就印相對路徑（data/logs/geocode_xxx.jsonl），不在就印完整路徑。"""
    try:
        return str(path.resolve().relative_to(config.ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    sys.exit(main())
