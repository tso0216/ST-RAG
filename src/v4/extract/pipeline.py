"""主流程：照 README.md「流程」的順序把各個模組串起來，決定哪些文章略過、哪些從地點解析接著做，收集執行結果。

1. `article` 整理文章；沒有 `author`、資料庫已有、重複的略過。
2. `data/v4_pending/` 沒有這篇時做兩次 llm 解析（`parse_line`、`parse_note`，裡面各自檢查和重試），
   結果馬上存進 `v4_pending/`。同時做幾篇由 jobs 決定。
3. 地點解析（`geocoding`）和寫入（`store`）照輸入的順序逐篇做。解析不到的地點捨棄，
   用到它的那一天、place_note、move 寫入時拿掉（`store.write_article`）。寫完刪掉 pending。

進度一律印在標準輸出：每篇開頭標 `[第幾篇/共幾篇]`，llm 解析開始和完成、地點解析每做完一個地點、寫入各印一行。
llm 用量照步驟（`PARSE`、`GEOCODE`）分開累計在 `Report.llm_usage`。

llm 呼叫失敗、國土地理院呼叫失敗、第一次解析重試後還有問題，都整批停下；已經寫入的文章保留，重跑會略過。
開始時開本機地圖（`data/geo/japan.duckdb`，不存在就丟 `MissingLocalMap`）和這次的地點解析 log 檔，結束時關掉。
"""
import asyncio
import logging
import time
from collections import Counter
from pathlib import Path

from service import llm
from service.gsi import GsiClient, GsiError
from service.llm import LLMError
from v4 import db

from . import article as article_loader
from . import config, parse_line, parse_note, pending, store
from . import geocode_log as glog
from .data_schema import Article, Parsed, Report
from .geocode_log import GeocodeLog
from .geocoding import Geocoder, places_to_resolve
from .local_map import LocalMap, MissingLocalMap  # noqa: F401（MissingLocalMap 給呼叫的人接）

log = logging.getLogger(__name__)

# `Report.llm_usage` 的步驟
PARSE = "llm 解析（線、筆記）"
GEOCODE = "地點解析"

# `Report.dropped` 除了 `store.DROPPED_KINDS` 以外的一種：第二次解析重問後還有問題而不收入的
PARSE_DROPPED = "解析檢查不過"


def collect(sources: list[Path], limit: int | None = None) -> list[Path]:
    """資料夾展開成裡面的 *.json（照檔名排），limit 只留前幾篇。"""
    files: list[Path] = []
    for source in map(Path, sources):
        files += sorted(source.glob("*.json")) if source.is_dir() else [source]
    return files if limit is None else files[:limit]


async def run(sources: list[Path], db_path: Path | None = None, jobs: int = 1, limit: int | None = None, *,
              local_map: LocalMap | None = None, gsi: GsiClient | None = None,
              pending_dir: Path | None = None, log_dir: Path | None = None) -> Report:
    """路徑沒給就用 `config` 的（呼叫時才讀）。

    local_map 沒給就開 `config.LOCAL_MAP_PATH`（不存在丟 `MissingLocalMap`，什麼都不做），結束時關掉；
    有給的由呼叫的人關。gsi 沒給就打真的國土地理院。
    """
    started = time.monotonic()
    db_path = db_path or config.DB_PATH
    pending_dir = pending_dir or config.PENDING_DIR
    own_map = local_map is None
    local_map = local_map or LocalMap(config.LOCAL_MAP_PATH)
    report = Report(llm_usage={PARSE: llm.Tally(), GEOCODE: llm.Tally()})
    conn = db.connect(db_path)
    log_file = GeocodeLog.start(log_dir or config.LOG_DIR)
    report.geocode_log = log_file.path
    geocoder = Geocoder(conn, local_map, gsi=gsi or GsiClient(), log_file=log_file)
    handled: set[str] = set()   # 這次做過地點解析的文章
    tasks: dict[str, asyncio.Task] = {}
    try:
        queue = _queue(collect(sources, limit), conn, report)
        tags = {a.id: f"[{n}/{len(queue)}] {a.id}" for n, a in enumerate(queue, 1)}
        stored = {a.id: pending.load(pending_dir, a.id) for a in queue}
        _say(_plan_line(len(queue), sum(1 for a in queue if stored[a.id]), report.skipped))
        gate = asyncio.Semaphore(max(1, jobs))

        async def parse(article: Article) -> Parsed:
            async with gate:
                tag, t0 = tags[article.id], time.monotonic()
                _say(f"{tag} llm 解析中：{article.title}（{len(article.comments)} 則推文）")
                log.info("%s：開始 llm 解析（%s，%d 則推文）", article.id, article.title, len(article.comments))
                with llm.track(report.llm_usage[PARSE]), llm.track() as spent:   # 整批的、這一篇的
                    line = await parse_line.parse(article)
                    note, dropped = await parse_note.parse(article, line)
            parsed = Parsed.from_llm(line, note, dropped)
            _say(f"{tag} llm 解析完成：{len(parsed.places)} 個地點、{len(parsed.days)} 天"
                 f"（llm {spent.calls} 次，{duration(time.monotonic() - t0)}）")
            pending.save(pending_dir, article.id, parsed)
            return parsed

        tasks = {a.id: asyncio.create_task(parse(a)) for a in queue if stored[a.id] is None}
        for article in queue:
            tag = tags[article.id]
            try:
                if stored[article.id]:
                    log.info("%s：用 v4_pending 裡上次的 llm 解析結果", article.id)
                    _say(f"{tag} 接著上次的 llm 解析結果（存在 v4_pending）")
                parsed = stored[article.id] or await tasks[article.id]
                t0 = time.monotonic()
                _say(f"{tag} 地點解析開始：{len(places_to_resolve(parsed))} 個地點")
                with llm.track(report.llm_usage[GEOCODE]):
                    place_ids = await geocoder.resolve_article(
                        article.id, parsed, on_place=lambda no, total, trace: _say(_place_line(no, total, trace)))
            except (LLMError, GsiError, parse_line.ParseError) as e:
                report.error = f"{article.id}：{e}"
                report.unfinished = [a.id for a in queue if a.id not in handled]
                break
            handled.add(article.id)
            took = duration(time.monotonic() - t0)
            dropped = store.write_article(conn, article, parsed, place_ids) or Counter()
            if parsed.dropped:
                dropped[PARSE_DROPPED] += parsed.dropped
            pending.delete(pending_dir, article.id)
            report.written.append(article.id)
            for kind, n in dropped.items():
                report.dropped[kind] = report.dropped.get(kind, 0) + n
            gone = sum(1 for ref in place_ids.values() if ref is None)
            lost = f"，捨棄 {gone} 個地點，拿掉 {count_text(dropped)}" if dropped or gone else ""
            _say(f"{tag} 寫入（地點解析 {took}{lost}）")
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        conn.close()
        log_file.close()
        if own_map:
            local_map.close()
        report.seconds = time.monotonic() - started
    return report


def _say(message: str) -> None:
    print(message, flush=True)


def _plan_line(total: int, resumed: int, skipped: list[tuple[str, str]]) -> str:
    extra = [f"{resumed} 篇接著上次的 llm 解析結果"] if resumed else []
    if skipped:
        extra.append(f"略過 {len(skipped)} 篇")
    return f"這次要做 {total} 篇{'（' + '，'.join(extra) + '）' if extra else ''}：每篇 llm 解析 → 地點解析 → 寫入"


def _place_line(no: int, total: int, trace: glog.Trace) -> str:
    """地點解析做完一個地點的那一行，例如 `  3/15 新宿站：地圖-第1輪 → 新宿（llm 2 次，8 秒）`。"""
    result = trace.result or ""
    if trace.used:
        result += f" → {trace.used['name']}"
    elif trace.result == glog.DROPPED and trace.fail_reason:
        result += f"（{trace.fail_reason}）"
    cost = [f"llm {trace.llm_calls} 次"] if trace.llm_calls else []
    cost.append(duration(time.monotonic() - trace.started))
    return f"  {no}/{total} {trace.place.name_zh}：{result}（{'，'.join(cost)}）"


def count_text(counts) -> str:
    """`天 1、place_note 2`；只列有的。"""
    return "、".join(f"{k} {n}" for k, n in counts.items() if n)


def duration(seconds: float) -> str:
    """`8 秒`、`3 分 05 秒`、`1 小時 02 分`。"""
    s = round(seconds)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分 {s % 60:02d} 秒"
    return f"{s // 3600} 小時 {s % 3600 // 60:02d} 分"


def _queue(files: list[Path], conn, report: Report) -> list[Article]:
    """逐篇整理文章；讀不了、沒有 `author`、重複、資料庫已有的記在 report.skipped，其餘回傳。"""
    queue, seen = [], set()
    for path in files:
        try:
            article = article_loader.load(path)
            if article.id in seen:
                raise article_loader.Skipped("重複")
            if store.has_article(conn, article.id):
                raise article_loader.Skipped("資料庫已有")
        except article_loader.Skipped as e:
            report.skipped.append((path.name, str(e)))
            continue
        seen.add(article.id)
        queue.append(article)
    return queue
