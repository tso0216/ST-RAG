"""命令列（規格 3）：

    PYTHONPATH=.:src .venv/bin/python -m v4.rag ask "10 月帶長輩，Day3 明治神宮 → 淺草寺 → 東京晴空塔，會不會太趕？"
    PYTHONPATH=.:src .venv/bin/python -m v4.rag ask --article data/ptt_ask/itinerary/ptt_M.1767020760.A.E80.json
    PYTHONPATH=.:src .venv/bin/python -m v4.rag index

`ask`：stdout 印答案（Markdown）；`--json` 時改印完整紀錄；`--log` 寫看得懂的流程紀錄（`flowlog`，出錯時也寫）：
主檔用白話寫每一輪查了什麼、為什麼、找到什麼，最後是答案；同資料夾的 `.detail` 檔是參數、每一步的用量，
和每次 llm 的完整輸入和輸出、重問、地點解析每一輪的過程（跑的時候收集 logger `v4` 的訊息）。
答完結束代碼 0；中途出錯是 1，錯誤印在 stderr。
本機地圖不存在時一開始就停下，什麼都不做。

`index`：把資料庫裡每一筆 `place_note` 和 `day_note` 的 embedding 算好（文字的寫法跟 ask 用的一樣），
印出這次新算了幾段、全部有幾段。
"""
import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

from service.env import load_env
from service.jev import JEVClient, JEVError
from v4 import db
from v4.extract import config as extract_config
from v4.extract.article import Skipped
from v4.extract.local_map import LocalMap, MissingLocalMap

from . import config, flowlog, parse, pipeline, trace
from .embedding import CachedEmbedder, EmbeddingError, OpenAIEmbedder

INDEX_CHUNK = 1024   # index 一次算幾段（算完一批就寫進 embedding 表，中途出錯前面的不用重算）


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m v4.rag", description="用 v4 資料庫裡的旅客經驗回答日本旅遊的發問")
    sub = ap.add_subparsers(dest="command", required=True)
    a_ask = sub.add_parser("ask", help="回答一句問題或一篇發問文")
    a_ask.add_argument("question", nargs="?", help="一句問題（跟 --article 二選一）")
    a_ask.add_argument("--article", type=Path, help="一篇 PTT 文章 JSON 的路徑（格式同 v4 抽取的輸入）")
    a_ask.add_argument("--db", type=Path, default=None, help=f"資料庫路徑（預設 {config.DB_PATH}）")
    a_ask.add_argument("--log", type=Path,
                       help="把看得懂的流程紀錄寫到這個路徑；每次 llm 的完整輸入和輸出寫在同資料夾的 .detail 檔")
    a_ask.add_argument("--json", action="store_true", help="stdout 印完整紀錄，不印答案的純文字")
    a_index = sub.add_parser("index", help="先算好資料庫裡每一筆 place_note 和 day_note 的 embedding")
    a_index.add_argument("--db", type=Path, default=None, help=f"資料庫路徑（預設 {config.DB_PATH}）")
    a = ap.parse_args(argv)
    load_env()
    if a.command == "index":
        return index(a.db or config.DB_PATH)
    if (a.question is None) == (a.article is None):
        ap.error("問題文字和 --article 要二選一")
    return ask(a)


def ask(a: argparse.Namespace) -> int:
    db_path = a.db or config.DB_PATH
    try:
        local_map = LocalMap(extract_config.LOCAL_MAP_PATH)
    except MissingLocalMap as e:
        print(str(e), file=sys.stderr)
        return 1
    try:
        article = parse.load_article(a.article) if a.article else parse.from_question(a.question)
    except Skipped as e:
        local_map.close()
        print(f"讀不了 {a.article}：{e}", file=sys.stderr)
        return 1
    conn = db.connect(db_path)
    capture = flowlog.Capture() if a.log else None
    try:
        with capture or contextlib.nullcontext():
            result = asyncio.run(_ask(article, conn, local_map, db_path, a.question))
    except JEVError as e:   # JEV client 建不起來（例如沒有金鑰）
        print(f"錯誤：{e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
        local_map.close()
    if capture is not None:
        flowlog.write(a.log, result.record, capture.text())
    if a.json:
        print(trace.dumps(result.record))
    if result.error:
        print(f"錯誤：{result.error}", file=sys.stderr)
        return 1
    if not a.json:
        print(result.answer)
    return 0


async def _ask(article, conn, local_map: LocalMap, db_path: Path, question_text: str | None) -> pipeline.Result:
    jev = JEVClient()
    try:
        services = pipeline.Services(conn, local_map, jev, OpenAIEmbedder(model=config.EMBEDDING_MODEL))
        return await pipeline.run_article(article, services, db_path=db_path, question_text=question_text)
    finally:
        await jev.aclose()


def index_texts(conn) -> dict[str, list[str]]:
    """ask 會拿去算 embedding 的文字：`place_note` 是「poi_name：content」、`day_note` 是 content（12.3）。"""
    notes = [f"{name}：{content or ''}" for name, content in conn.execute("SELECT poi_name, content FROM place_note")]
    days = [content for (content,) in conn.execute("SELECT content FROM day_note")]
    return {"place_note": notes, "day_note": days}


def index(db_path: Path) -> int:
    conn = db.connect(db_path)
    embedder = CachedEmbedder(OpenAIEmbedder(model=config.EMBEDDING_MODEL), conn)
    counts: dict[str, tuple[int, int]] = {}
    try:
        for table, texts in index_texts(conn).items():
            unique = sorted(set(texts))
            before = embedder.sent
            for i in range(0, len(unique), INDEX_CHUNK):
                embedder.embed(unique[i:i + INDEX_CHUNK])
            counts[table] = (embedder.sent - before, len(unique))
    except EmbeddingError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    for table, (new, total) in counts.items():
        print(f"{table}：這次新算 {new} 段，全部 {total} 段")
    return 0


if __name__ == "__main__":
    sys.exit(main())
