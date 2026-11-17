"""主流程：照 README.md「流程」的順序把各個模組串起來，決定哪些文章略過、哪些從地點解析接著做，收集執行結果。

1. `article` 整理文章；沒有 `author`、資料庫已有、重複的略過。
2. `data/v4_pending/` 沒有這篇時做兩次 llm 解析（`parse_line`、`parse_note`，裡面各自檢查和重試），
   結果馬上存進 `v4_pending/`。同時做幾篇由 jobs 決定。
3. 地點解析（`geocoding`）和寫入（`store`）照輸入的順序逐篇做。地點都有結果才寫入，寫完刪掉 pending。
4. 整批跑完，把要使用者決定的地點寫進 `place_unresolved.tsv` 並印出來。

llm 呼叫失敗、OSM 呼叫失敗、第一次解析重試後還有問題，都整批停下；已經寫入的文章保留，重跑會略過。
"""
import asyncio
import logging
from pathlib import Path

from service.llm import LLMError
from service.nominatim import NominatimError
from v4 import db

from . import article as article_loader
from . import config, parse_line, parse_note, pending, store, unresolved
from .data_schema import Article, Parsed, Report
from .geocoding import Geocoder
from .osm_search import OsmSearch

log = logging.getLogger(__name__)


def collect(sources: list[Path], limit: int | None = None) -> list[Path]:
    """資料夾展開成裡面的 *.json（照檔名排），limit 只留前幾篇。"""
    files: list[Path] = []
    for source in map(Path, sources):
        files += sorted(source.glob("*.json")) if source.is_dir() else [source]
    return files if limit is None else files[:limit]


async def run(sources: list[Path], db_path: Path | None = None, jobs: int = 1, limit: int | None = None, *,
              osm: OsmSearch | None = None, unresolved_path: Path | None = None,
              pending_dir: Path | None = None) -> Report:
    """路徑沒給就用 `config` 的（呼叫時才讀）。osm 沒給就打真的 Nominatim。"""
    db_path = db_path or config.DB_PATH
    unresolved_path = unresolved_path or config.UNRESOLVED_PATH
    pending_dir = pending_dir or config.PENDING_DIR
    report = Report()
    conn = db.connect(db_path)
    geocoder = Geocoder(conn, osm or OsmSearch(), unresolved.Answers.load(unresolved_path))
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
            except (LLMError, NominatimError, parse_line.ParseError) as e:
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
