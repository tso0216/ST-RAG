"""主流程：照 README.md「流程」的順序把各個模組串起來，決定哪些文章略過、哪些從地點解析接著做，收集執行結果。

1. `article` 整理文章；沒有 `author`、資料庫已有、重複的略過。
2. `data/v4_pending/` 沒有這篇時做兩次 llm 解析（`parse_line`、`parse_note`，裡面各自檢查和重試），
   結果馬上存進 `v4_pending/`。同時做幾篇由 jobs 決定。
3. 地點解析（`geocoding`）和寫入（`store`）照輸入的順序逐篇做。解析不到的地點照樣寫入，
   place id 留空（`store.write_article`）。寫完刪掉 pending。

終端機只印少量資訊：開頭一行（要做幾篇、log 在哪）、每篇寫入時一行。
詳細進度寫在這次的 log 資料夾（`geocode_log.GeocodeLog`，整批一個 `_run.log`、每篇一個檔）：
每篇開頭標 `[第幾篇/共幾篇]`，llm 解析開始和完成、地點解析開始、寫入各一行（`_run.log` 和那一篇的檔都寫），
每個地點做完 `geocoding` 在那一篇的檔寫一段。地點解析一批地點一起問 llm，所以 llm 次數和花的時間寫在寫入那一行（整篇的）。
logger `v4` 的訊息也接到 log；做一篇的範圍用 `log_file.article()` 包住，訊息寫進那一篇的檔。
`--verbose` 時 `__main__.py` 把 logger 的層級開到 INFO，才會寫進每一步的過程。
各部分（線、點段、搜尋計畫、挑候選、上網、查地圖、國土地理院、寫入）的 token 和時間記在 `Report.parts`（`usage`）。

llm 呼叫失敗、國土地理院呼叫失敗、第一次解析重試後還有問題，都整批停下；已經寫入的文章保留，重跑會略過。
開始時開本機地圖（`data/geo/japan.duckdb`，不存在就丟 `MissingLocalMap`）和這次的 log 資料夾，結束時關掉地圖。
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
from . import config, parse_line, parse_note, pending, store, usage
from .data_schema import Article, Parsed, Report
from .geocode_log import GeocodeLog
from .geocoding import Geocoder, places_to_resolve
from .local_map import LocalMap, MissingLocalMap  # noqa: F401（MissingLocalMap 給呼叫的人接）

log = logging.getLogger(__name__)

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
    各部分的 token 和時間記在 `Report.parts`（`usage`）。
    """
    with usage.collect() as parts:
        return await _run(sources, db_path, jobs, limit, local_map, gsi, pending_dir, log_dir, Report(parts=parts))


async def _run(sources: list[Path], db_path: Path | None, jobs: int, limit: int | None, local_map: LocalMap | None,
               gsi: GsiClient | None, pending_dir: Path | None, log_dir: Path | None, report: Report) -> Report:
    started = time.monotonic()
    db_path = db_path or config.DB_PATH
    pending_dir = pending_dir or config.PENDING_DIR
    own_map = local_map is None
    local_map = local_map or LocalMap(config.LOCAL_MAP_PATH)
    conn = db.connect(db_path)
    log_file = GeocodeLog.start(log_dir or config.LOG_DIR)
    report.log_dir = log_file.folder
    geocoder = Geocoder(conn, local_map, gsi=gsi or GsiClient(), log_file=log_file)
    handled: set[str] = set()   # 這次做過地點解析的文章
    tasks: dict[str, asyncio.Task] = {}
    say = log_file.progress      # 只寫 log：_run.log 和那一篇的檔

    def tell(message: str) -> None:
        """終端機和 _run.log 都寫。"""
        print(message, flush=True)
        log_file.run_line(message)

    handler = log_file.handler()
    logging.getLogger("v4").addHandler(handler)
    try:
        queue = _queue(collect(sources, limit), conn, report)
        tags = {a.id: f"[{n}/{len(queue)}] {a.id}" for n, a in enumerate(queue, 1)}
        stored = {a.id: pending.load(pending_dir, a.id) for a in queue}
        tell(_plan_line(len(queue), sum(1 for a in queue if stored[a.id]), report.skipped))
        print(f"log：{shown_path(log_file.folder)}/", flush=True)
        gate = asyncio.Semaphore(max(1, jobs))

        async def parse(article: Article) -> Parsed:
            with log_file.article(article.id):   # 這個 task 裡的 log 寫進這一篇的檔
                async with gate:
                    tag, t0 = tags[article.id], time.monotonic()
                    say(article.id, f"{tag} llm 解析：{article.title}，推文 {len(article.comments)} 則")
                    with llm.track() as spent:   # 這一篇的
                        with usage.part(usage.LINE):
                            line = await parse_line.parse(article)
                        with usage.part(usage.NOTE):
                            note, dropped = await parse_note.parse(article, line)
                parsed = Parsed.from_llm(line, note, dropped)
                say(article.id, f"{tag} llm 解析完成：地點 {len(parsed.places)}、天 {len(parsed.days)}，"
                    f"llm {spent.calls} 次，{duration(time.monotonic() - t0)}")
                pending.save(pending_dir, article.id, parsed)
                return parsed

        tasks = {a.id: asyncio.create_task(parse(a)) for a in queue if stored[a.id] is None}
        for article in queue:
            tag = tags[article.id]
            try:
                if stored[article.id]:
                    say(article.id, f"{tag} 用 pending：{article.title}")
                parsed = stored[article.id] or await tasks[article.id]
                t0 = time.monotonic()
                say(article.id, f"{tag} 地點解析：{len(places_to_resolve(parsed))} 個")
                with log_file.article(article.id), llm.track() as spent:   # 這一篇的
                    place_ids = await geocoder.resolve_article(article.id, parsed)
            except (LLMError, GsiError, parse_line.ParseError) as e:
                report.error = f"{article.id}：{e}"
                report.unfinished = [a.id for a in queue if a.id not in handled]
                break
            handled.add(article.id)
            took = duration(time.monotonic() - t0)
            with log_file.article(article.id), usage.part(usage.WRITE):
                items = store.write_article(conn, article, parsed, place_ids) or []
            report.dropped_items += items
            dropped = Counter(d.kind for d in items)
            if parsed.dropped:
                dropped[PARSE_DROPPED] += parsed.dropped
            pending.delete(pending_dir, article.id)
            report.written.append(article.id)
            for kind, n in dropped.items():
                report.dropped[kind] = report.dropped.get(kind, 0) + n
            gone = sum(1 for ref in place_ids.values() if ref is None)
            lost = "".join([f"，解析不到 {gone}" if gone else "", f"，拿掉 {count_text(dropped)}" if dropped else ""])
            say(article.id, f"{tag} 寫入：地點解析 llm {spent.calls} 次，{took}{lost}")
            print(f"{tag} {article.title}：地點 {len(parsed.places)}、天 {len(parsed.days)}{lost}", flush=True)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        conn.close()
        logging.getLogger("v4").removeHandler(handler)
        report.geocode_traces = log_file.traces
        if own_map:
            local_map.close()
        report.seconds = time.monotonic() - started
    return report


def shown_path(path: Path) -> str:
    """在專案裡就寫相對路徑（data/logs/extract_xxx），不在就寫完整路徑。"""
    try:
        return str(path.resolve().relative_to(config.ROOT))
    except ValueError:
        return str(path)


def _plan_line(total: int, resumed: int, skipped: list[tuple[str, str]]) -> str:
    """`共 3 篇，用 pending 1 篇，略過 1 篇`。"""
    extra = [f"用 pending {resumed} 篇"] if resumed else []
    if skipped:
        extra.append(f"略過 {len(skipped)} 篇")
    return "，".join([f"共 {total} 篇", *extra])


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
