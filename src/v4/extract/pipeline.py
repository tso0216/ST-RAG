"""主流程：照 README.md「流程」的順序把各個模組串起來，決定哪些文章略過、哪些從地點解析接著做，收集執行結果。

1. `article` 整理文章；沒有 `author`、資料庫已有、重複的略過。
2. `data/v4_pending/` 沒有這篇時做兩次 llm 解析（`parse_line`、`parse_note`，裡面各自檢查和重試），
   結果馬上存進 `v4_pending/`。同時做幾篇由 jobs 決定。
3. 地點解析（`geocoding`）和寫入（`store`）照輸入的順序逐篇做。地點都有結果才寫入，寫完刪掉 pending。
4. 整批跑完，把要使用者決定的地點寫進 `place_unresolved.tsv` 並印出來。

llm 呼叫失敗、國土地理院呼叫失敗、第一次解析重試後還有問題，都整批停下；已經寫入的文章保留，重跑會略過。
開始時開本機地圖（`data/geo/japan.duckdb`，不存在就丟 `MissingLocalMap`）和這次的地點解析 log 檔，結束時關掉。
"""
import asyncio
import logging
from pathlib import Path

from service.gsi import GsiClient, GsiError
from service.llm import LLMError
from v4 import db

from . import article as article_loader
from . import config, parse_line, parse_note, pending, store, unresolved
from .data_schema import Article, Parsed, Report
from .geocode_log import GeocodeLog
from .geocoding import Geocoder
from .local_map import LocalMap, MissingLocalMap  # noqa: F401（MissingLocalMap 給呼叫的人接）

log = logging.getLogger(__name__)


def collect(sources: list[Path], limit: int | None = None) -> list[Path]:
    """資料夾展開成裡面的 *.json（照檔名排），limit 只留前幾篇。"""
    files: list[Path] = []
    for source in map(Path, sources):
        files += sorted(source.glob("*.json")) if source.is_dir() else [source]
    return files if limit is None else files[:limit]


async def run(sources: list[Path], db_path: Path | None = None, jobs: int = 1, limit: int | None = None, *,
              local_map: LocalMap | None = None, gsi: GsiClient | None = None,
              unresolved_path: Path | None = None, pending_dir: Path | None = None,
              log_dir: Path | None = None) -> Report:
    """路徑沒給就用 `config` 的（呼叫時才讀）。

    local_map 沒給就開 `config.LOCAL_MAP_PATH`（不存在丟 `MissingLocalMap`，什麼都不做），結束時關掉；
    有給的由呼叫的人關。gsi 沒給就打真的國土地理院。
    """
    db_path = db_path or config.DB_PATH
    unresolved_path = unresolved_path or config.UNRESOLVED_PATH
    pending_dir = pending_dir or config.PENDING_DIR
    own_map = local_map is None
    local_map = local_map or LocalMap(config.LOCAL_MAP_PATH)
    report = Report()
    conn = db.connect(db_path)
    log_file = GeocodeLog.start(log_dir or config.LOG_DIR)
    report.geocode_log = log_file.path
    geocoder = Geocoder(conn, local_map, unresolved.Answers.load(unresolved_path), gsi=gsi or GsiClient(),
                        log_file=log_file)
    handled: set[str] = set()   # 這次做過地點解析的文章
    tasks: dict[str, asyncio.Task] = {}
    try:
        queue = _queue(collect(sources, limit), conn, report)
        gate = asyncio.Semaphore(max(1, jobs))

        async def parse(article: Article) -> Parsed:
            async with gate:
                log.info("%s：開始 llm 解析（%s，%d 則推文）", article.id, article.title, len(article.comments))
                line = await parse_line.parse(article)
                note, dropped = await parse_note.parse(article, line)
            parsed = Parsed.from_llm(line, note, dropped)
            pending.save(pending_dir, article.id, parsed)
            return parsed

        stored = {a.id: pending.load(pending_dir, a.id) for a in queue}
        tasks = {a.id: asyncio.create_task(parse(a)) for a in queue if stored[a.id] is None}
        for article in queue:
            try:
                if stored[article.id]:
                    log.info("%s：用 v4_pending 裡上次的 llm 解析結果", article.id)
                parsed = stored[article.id] or await tasks[article.id]
                place_ids, undecided = await geocoder.resolve_article(article.id, parsed)
            except (LLMError, GsiError, parse_line.ParseError) as e:
                report.error = f"{article.id}：{e}"
                report.unfinished = [a.id for a in queue if a.id not in handled]
                break
            handled.add(article.id)
            if undecided:
                report.waiting.append(article.id)
                report.unresolved += undecided
                print(f"{article.id}：有 {len(undecided)} 個地點要你決定，先不寫入", flush=True)
                continue
            dropped = store.write_article(conn, article, parsed, place_ids)
            pending.delete(pending_dir, article.id)
            report.written.append(article.id)
            report.dropped += parsed.dropped + (dropped or 0)
            print(f"{article.id}：寫入{'（接著上次的 llm 解析結果）' if stored[article.id] else ''}", flush=True)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        conn.close()
        log_file.close()
        if own_map:
            local_map.close()
        unresolved.save(unresolved_path, report.unresolved, handled)
    return report


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
