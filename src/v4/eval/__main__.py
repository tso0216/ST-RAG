"""命令列（規格 19）：

    PYTHONPATH=.:src .venv/bin/python -m v4.eval route [--limit N]
    PYTHONPATH=.:src .venv/bin/python -m v4.eval run <發問文 JSON 或資料夾>... [--db PATH]

`route`：分流評估，結果存一個檔。`--limit N` 只做 N 題（五個題型的檔輪流取，不是只取第一個檔）。
`run`：逐篇跑完整的問答，每篇存一個檔。

結果檔在 `src/v4/eval/runs/`，不覆寫。遇到 LLM、JEV、OpenAI、國土地理院、行程解析的錯誤整批停下（結束代碼 1），
已經跑完的留著；重跑時略過已經有結果的題或篇。
"""
import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from service import llm
from service.env import load_env
from service.jev import JEVClient, JEVError
from v4 import db
from v4.extract import config as extract_config
from v4.extract.article import Skipped
from v4.extract.local_map import LocalMap, MissingLocalMap
from v4.rag import config as rag_config
from v4.rag import parse, pipeline
from v4.rag.embedding import OpenAIEmbedder

from . import questions, route, runs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m v4.eval", description="v4 問答系統的評估")
    sub = ap.add_subparsers(dest="command", required=True)
    a_route = sub.add_parser("route", help="分流評估：只跑拆題，比較題型")
    a_route.add_argument("--limit", type=int, default=None, help="只做幾題（五個題型的檔輪流取）")
    a_run = sub.add_parser("run", help="回答評估：逐篇跑完整的問答，每篇存一份紀錄")
    a_run.add_argument("sources", nargs="+", type=Path, metavar="發問文 JSON 或資料夾")
    a_run.add_argument("--db", type=Path, default=None, help=f"資料庫路徑（預設 {rag_config.DB_PATH}）")
    a = ap.parse_args(argv)
    load_env()
    if a.command == "route":
        return route_command(a.limit)
    return run_command(a.sources, a.db or rag_config.DB_PATH)


def sample(items: list[questions.LabeledQuestion], limit: int | None) -> list[questions.LabeledQuestion]:
    """limit 給了時，五個題型的檔輪流取，取到 limit 題。"""
    if limit is None:
        return items
    groups: dict[str, list[questions.LabeledQuestion]] = {}
    for q in items:
        groups.setdefault(q.key.split("#")[0], []).append(q)
    out: list[questions.LabeledQuestion] = []
    queues = [list(g) for _, g in sorted(groups.items())]
    while len(out) < limit and any(queues):
        for queue in queues:
            if queue and len(out) < limit:
                out.append(queue.pop(0))
    return out


def route_command(limit: int | None) -> int:
    items = sample(questions.load(), limit)
    done = runs.finished_routes()
    previous = [done[q.key] for q in items if q.key in done]
    todo = [q for q in items if q.key not in done]
    print(f"共 {len(items)} 題，之前做過 {len(previous)} 題，這次做 {len(todo)} 題")

    def progress(n: int, total: int, r: dict) -> None:
        mark = "✓" if r["人工"] == r["預測"] else "✗"
        print(f"[{n}/{total}] {mark} {r['人工']} → {r['預測']}：{r['問題內容'][:40]}")

    started = datetime.now()
    with llm.track() as tally:
        new, error = asyncio.run(route.evaluate(todo, on_done=progress))
    order = {q.key: i for i, q in enumerate(items)}
    results = sorted([*previous, *new], key=lambda r: order[r["key"]])
    summary = route.score(results)
    data = {"kind": runs.ROUTE, "開始時間": started.isoformat(timespec="seconds"), "限制題數": limit,
            "模型": rag_config.MODEL, "結果": summary, "每一題": results,
            "用量": {"LLM 次數": tally.calls, "輸入 token": tally.input_tokens, "輸出 token": tally.output_tokens,
                   "估計金額（美元）": round(tally.cost_usd, 6)},
            "錯誤": error}
    path = runs.save(runs.ROUTE, data)
    print("\n".join(route.lines(summary)))
    print(f"llm {tally.calls} 次，約 US${tally.cost_usd:.2f}")
    print(f"結果：{path}")
    if error:
        print(f"錯誤，整批停下：{error}（還沒做 {len(todo) - len(new)} 題）", file=sys.stderr)
        return 1
    return 0


def collect(sources: list[Path]) -> list[Path]:
    files: list[Path] = []
    for s in sources:
        files += sorted(s.glob("*.json")) if s.is_dir() else [s]
    return files


def run_command(sources: list[Path], db_path: Path) -> int:
    try:
        local_map = LocalMap(extract_config.LOCAL_MAP_PATH)
    except MissingLocalMap as e:
        print(str(e), file=sys.stderr)
        return 1
    conn = db.connect(db_path)
    try:
        return asyncio.run(_run_all(collect(sources), conn, local_map, db_path))
    except JEVError as e:   # JEV client 建不起來
        print(f"錯誤：{e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
        local_map.close()


async def _run_all(files: list[Path], conn, local_map: LocalMap, db_path: Path) -> int:
    done = runs.finished_articles()
    jev = JEVClient()
    try:
        services = pipeline.Services(conn, local_map, jev, OpenAIEmbedder(model=rag_config.EMBEDDING_MODEL))
        for i, f in enumerate(files, 1):
            try:
                article = parse.load_article(f)
            except Skipped as e:
                print(f"[{i}/{len(files)}] 略過 {f.name}：{e}")
                continue
            if article.id in done:
                print(f"[{i}/{len(files)}] 略過 {article.id}：已經有結果")
                continue
            result = await pipeline.run_article(article, services, db_path=db_path)
            path = runs.save(article.id, {"kind": runs.RUN, "文章": article.id, "檔案": str(f),
                                          "紀錄": result.record, "錯誤": result.error})
            used = result.record.get("用量", {}).get("LLM", {})
            print(f"[{i}/{len(files)}] {article.id} {article.title}：{'出錯' if result.error else '完成'}，"
                  f"llm {used.get('次數', 0)} 次，約 US${used.get('估計金額（美元）', 0):.2f}；{path.name}")
            if result.error:
                print(f"錯誤，整批停下：{result.error}", file=sys.stderr)
                return 1
    finally:
        await jev.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
