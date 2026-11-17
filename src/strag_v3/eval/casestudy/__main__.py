"""案例研究的命令列（從專案根目錄跑，`PYTHONPATH=.:src`）。

    python -m strag_v3.eval.casestudy run [--only C3 C4]   # 跑 cases.json 的案例，存結果檔到 runs/
    python -m strag_v3.eval.casestudy report [結果檔]       # 寫 result/casestudy.md 和流程圖 casestudy_<id>.png（預設拿最新的結果檔）

`run` 會呼叫規劃器、JEV（有快取的不重問）和生成器；`report` 只讀結果檔。
"""
import argparse
import asyncio
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from service.env import load_env
from service.jev import JEVClient
from service.nominatim import NominatimClient

from ...rag import llm
from ...rag.embedding import CachedEmbedder, OpenAIEmbedder
from ...rag.params import DEFAULT
from .figure import draw, load_excerpt
from .report import report
from .trace import TracingRetriever

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
DB = ROOT / "data" / "ptt.db"
CASES, RUNS, RESULT = HERE / "cases.json", HERE / "runs", HERE / "result" / "casestudy.md"


def load_cases(only: list[str] | None = None) -> list[dict]:
    cases = json.loads(CASES.read_text())["cases"]
    return [c for c in cases if not only or c["id"] in only]


async def run_case(case: dict, conn: sqlite3.Connection, jev: JEVClient, nominatim) -> dict:
    """一個案例走完整個主系統（預設參數）：規劃器 → 檢索（記錄每一層）→ 生成器。"""
    question = case["question"]
    plan = await llm.plan(question)
    retriever = TracingRetriever(conn, CachedEmbedder(OpenAIEmbedder(), conn), DEFAULT, nominatim=nominatim, jev=jev)
    sections = await retriever.retrieve(plan, question)
    answer = await llm.answer(question, sections)
    return {**case, "plan": plan.to_dict(), "trace": retriever.trace,
            "evidence": [asdict(e) for s in sections for e in s.evidence], "answer": answer,
            "stage_ok": all(t["stage"] == case["expect_stage"] for t in retriever.trace)}


async def run_cmd(db: Path, only: list[str] | None) -> int:
    started = datetime.now()
    conn = sqlite3.connect(db)  # 主系統會寫相關判斷和地點解析的快取，這是它原本的行為
    jev = JEVClient()
    out = []
    try:
        for case in load_cases(only):
            try:
                c = await run_case(case, conn, jev, NominatimClient())
            except Exception as e:  # noqa: BLE001  一個案例失敗不毀掉整批
                c = {**case, "error": f"{type(e).__name__}: {e}"}
            out.append(c)
            stages = c.get("error") or "、".join(t["stage"] for t in c["trace"])
            print(f"{c['id']}  {stages}" + ("" if c.get("error") or c["stage_ok"] else "  （跟預期不同）"), flush=True)
    finally:
        conn.close()
        await jev.aclose()
    path = RUNS / f"{started:%Y%m%d-%H%M%S}_casestudy.json"
    config = {"system": "strag", "model": llm.MODEL, "jev_model": DEFAULT.jev_model, "jev_pass": DEFAULT.jev_pass,
              "db": db.name, "started_at": started.astimezone().isoformat(timespec="seconds"), "run": path.name}
    RUNS.mkdir(exist_ok=True)
    with open(path, "x") as f:  # 不覆寫
        json.dump({"config": config, "cases": out}, f, ensure_ascii=False, indent=2)
    print(f"結果檔：{path}")
    return 0


def report_cmd(run: Path | None) -> int:
    run = run or max(RUNS.glob("*_casestudy.json"), default=None)
    if run is None:
        print("錯誤：runs/ 沒有結果檔，先跑 run", file=sys.stderr)
        return 1
    RESULT.parent.mkdir(exist_ok=True)
    data = json.loads(run.read_text())
    RESULT.write_text(report(data))
    print(f"{run.name} → {RESULT}")
    for c in data["cases"]:  # 每個案例一張流程圖（簡報、論文用）
        if not c.get("error"):
            png = RESULT.with_name(f"casestudy_{c['id'].lower()}.png")
            excerpt = load_excerpt(png.with_name(png.stem + "_excerpt.json"), run.name)
            print(f"{c['id']} → {draw(c, png, excerpt)}" + ("" if excerpt else "（沒有這份結果檔的回答節錄，圖裡不畫回答）"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m strag_v3.eval.casestudy", description="案例研究（strag full）")
    ap.add_argument("--db", type=Path, default=DB, help=f"片段所在的 DB（預設 {DB.relative_to(ROOT)}）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="跑案例，存結果檔")
    s.add_argument("--only", nargs="+", metavar="ID", help="只跑這幾個案例")
    s = sub.add_parser("report", help="把結果檔寫成 result/casestudy.md")
    s.add_argument("run", type=Path, nargs="?")
    a = ap.parse_args(argv)
    load_env()
    if a.cmd == "run":
        return asyncio.run(run_cmd(a.db, a.only))
    return report_cmd(a.run)


if __name__ == "__main__":
    sys.exit(main())
