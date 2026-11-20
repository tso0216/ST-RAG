"""命令列：解析參數、呼叫 `pipeline`，結束時在終端機印幾行總結（寫入幾篇、解析不到幾個地點、llm 花費、時間、log 資料夾），
完整的總結（略過和拿掉的明細、地點解析的統計、解析不到的地點、各部分的 token 和時間）接在 `_run.log` 最後面；整批停下時以非 0 結束。
按 Ctrl-C 中斷時印出到中斷為止花的 token。`--verbose` 只是把 logger `v4` 開到 INFO，訊息寫進 log（`pipeline` 接的，每篇寫進自己的檔）。

    PYTHONPATH=.:src python -m v4.extract data/ptt_ask/itinerary --db data/v4.db
"""
import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path

from service import llm

from . import config, geocode_log, store, usage
from .geocode_log import DROPPED
from .local_map import MissingLocalMap
from .pipeline import PARSE_DROPPED, count_text, duration, run, shown_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m v4.extract", description="從 ptt 文章抽出點、線、段，寫進資料庫")
    ap.add_argument("sources", nargs="+", type=Path, metavar="資料夾或檔案", help="要抽的文章 JSON，或裝著它們的資料夾")
    ap.add_argument("--db", type=Path, default=None,
                    help=f"資料庫路徑（預設 {config.DB_PATH}）；不存在就照 src/v4/schema.sql 建")
    ap.add_argument("--jobs", type=int, default=1, help="同時做 llm 解析的篇數（預設 1）；地點解析和寫入一律逐篇做")
    ap.add_argument("--limit", type=int, default=None, help="只做前幾篇，測試用")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="在 log 檔另外寫每次 llm 的輸入和輸出，和每一步的過程：檢查出的問題和第幾次重試、被拿掉的資料、"
                         "每個地點查本機地圖、上網查地址和 llm 挑選的經過（終端機印的不變）")
    a = ap.parse_args(argv)
    logging.getLogger("v4").setLevel(logging.INFO if a.verbose else logging.WARNING)

    with llm.track() as total:
        try:
            r = asyncio.run(run(a.sources, db_path=a.db, jobs=a.jobs, limit=a.limit))
        except MissingLocalMap as e:
            print(str(e), file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\n中斷", file=sys.stderr)
            for line in usage_lines(total):
                print(line)
            return 130

    out: list[str] = []   # 完整的總結，接在 _run.log 最後面
    out.append(f"寫入：{len(r.written)} 篇")
    out.append(f"略過：{len(r.skipped)} 篇")
    for reason, count in Counter(reason for _, reason in r.skipped).items():
        out.append(f"  {reason}：{count} 篇")
    for name, reason in r.skipped:
        if reason != "資料庫已有":
            out.append(f"  {name}（{reason}）")
    dropped = {k: r.dropped.get(k, 0) for k in (*store.DROPPED_KINDS, PARSE_DROPPED)}
    out.append(f"拿掉沒寫入：{sum(dropped.values())} 筆" + (f"（{count_text(dropped)}）" if any(dropped.values()) else ""))
    out += [f"  {d.line()}" for d in r.dropped_items]
    out += ["", *geocode_log.summary_lines(r.geocode_traces), *geocode_log.dropped_lines(r.geocode_traces)]
    errors = [] if not r.error else [
        f"錯誤，整批停下：{r.error}", f"未完成：{len(r.unfinished)} 篇"]
    usage_part = ["", *usage_lines(total), *usage.lines(r.parts, duration), f"花費時間：{duration(r.seconds)}"]

    print("\n" + "\n".join(brief(r, total, sum(dropped.values()))))
    if errors:
        print("\n" + "\n".join(errors), file=sys.stderr)
    run_log = r.log_dir / geocode_log.RUN_LOG if r.log_dir else None
    if run_log and run_log.exists():
        with run_log.open("a", encoding="utf-8") as f:
            f.write("\n========== 總結 ==========\n\n" + "\n".join([*out, *([""] + errors if errors else []), *usage_part]) + "\n")
    return 1 if r.error else 0


def brief(r, total: llm.Tally, dropped: int) -> list[str]:
    """終端機印的總結，明細在 log 檔。"""
    gone = sum(1 for t in r.geocode_traces if t.result == DROPPED)
    lines = [f"寫入 {len(r.written)} 篇、略過 {len(r.skipped)} 篇" + (f"、拿掉沒寫入 {dropped} 筆" if dropped else ""),
             f"地點 {len(r.geocode_traces)} 個，解析不到 {gone} 個",
             f"llm {total.calls} 次，約 US${total.cost_usd:.2f}",
             f"花費時間：{duration(r.seconds)}"]
    if r.log_dir:
        lines.append(f"log：{shown_path(r.log_dir)}/")
    return lines


def usage_lines(total: llm.Tally) -> list[str]:
    """這次 llm 花的 token 合計（各部分的在 `usage.lines`）。"""
    if total.calls == 0:
        return ["llm 用量：這次沒有呼叫 llm"]
    return [f"llm 用量：共 {total.calls} 次，{_tokens(total)}",
            f"  約 US${total.cost_usd:.2f}"]


def _tokens(t: llm.Tally) -> str:
    cached = f"（其中快取讀取 {t.cache_read_tokens:,}）" if t.cache_read_tokens else ""
    return f"輸入 {t.input_tokens:,} token{cached}、輸出 {t.output_tokens:,} token"


if __name__ == "__main__":
    sys.exit(main())
