"""gate 比較實驗的命令列（從專案根目錄跑，`PYTHONPATH=.:src`）。

    python -m strag_v3.eval.gate run --system {llm,semantic}   # 跑 50 題，存結果檔到 runs/
    python -m strag_v3.eval.gate run --system <名稱> --retry <結果檔>  # 只重跑那份結果檔裡出錯的題，另存新檔
    python -m strag_v3.eval.gate judge <結果檔> [--n N]          # 評審（跟 baseline 同一個評審）
    python -m strag_v3.eval.gate report [評審檔...]              # 品質表加成本表；不給就拿 full 和兩組最新的評審檔
    python -m strag_v3.eval.gate sweep [--deltas D ...]          # semantic 的 δ 檢索掃描（不呼叫 LLM），寫 result/ 的 CSV
    python -m strag_v3.eval.gate jev-usage                       # 重問 JEV 量 JEV gate 的 token 用量，寫 result/ 的 CSV

δ 敏感度：`run --system semantic --delta D` 每個 δ 跑一次再 `judge`，結果檔一樣存在 runs/；
`report` 預設拿的 semantic 是 δ 等於預設值的那一份。圖表在 `result/gate.ipynb`。

JEV 那一組是 baseline 的 `strag`（full），不重跑；`run` 沿用它結果檔裡每題的檢索計畫（`--full` 指定哪一份）。
"""
import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from service.env import load_env

from ...rag.params import Params
from ...rag.plan import Plan
from ..baseline import chunks
from ..baseline.__main__ import DB, ROOT, baseline_embedder, fragments_of, judge_cmd
from ..baseline.judge import Verdict
from ..baseline.metrics import score_run
from ..baseline.questions import PTT_V2, load_questions
from ..baseline.report import Row, report
from ..baseline.results import RUNS as BASELINE_RUNS
from ..baseline.results import Outcome, load_judge, load_run, save_run
from ..baseline.systems import MODEL, run_all
from .report import NOTE, cost_table, gate_label
from . import usage
from .sweep import DELTAS, sweep, write_csv
from .systems import GATES, GateStrag

RUNS = Path(__file__).resolve().parent / "runs"


def latest(folder: Path, pattern: str) -> Path | None:
    """檔名開頭是時間，照檔名排最後一個就是最新的。"""
    found = sorted(p for p in folder.glob(pattern))
    return found[-1] if found else None


def latest_full_run() -> Path | None:
    runs = [p for p in BASELINE_RUNS.glob("*_strag.json") if not p.name.endswith(".judge.json")]
    return sorted(runs)[-1] if runs else None


def load_plans(full: Path) -> dict[str, Plan]:
    _, outcomes = load_run(full)
    return {o.id: Plan.from_dict(o.extra["plan"]) for o in outcomes if "plan" in o.extra}


async def run_cmd(name: str, delta: float, full: Path | None, retry: Path | None, db: Path, concurrency: int) -> int:
    kept: dict[str, Outcome] = {}
    if retry is not None:
        old_config, old = load_run(retry)
        if old_config["system"] != f"gate_{name}" or (name == "semantic" and old_config["gate"]["delta"] != delta):
            print(f"錯誤：{retry.name} 不是同一個設定的結果檔", file=sys.stderr)
            return 1
        kept = {o.id: o for o in old if o.error is None}
        full = full or BASELINE_RUNS / old_config["plans_from"]
    full = full or latest_full_run()
    if full is None:
        print("錯誤：找不到 full（baseline 的 strag）結果檔，先跑 eval.baseline run --system strag", file=sys.stderr)
        return 1
    questions = load_questions()
    plans = load_plans(full)
    if missing := [q.id for q in questions if q.id not in plans]:
        print(f"錯誤：{full.name} 裡這些題沒有檢索計畫：{'、'.join(missing)}", file=sys.stderr)
        return 1
    params = Params(gate=name, delta=delta)
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    cache = chunks.connect()
    conn = sqlite3.connect(db)
    try:
        system = GateStrag(conn, baseline_embedder(cache), params, plans, name=f"gate_{name}")
        todo = [q for q in questions if q.id not in kept]
        redone = {o.id: o for o in await run_all(system, todo, concurrency)}
        outcomes = [kept.get(q.id) or redone[q.id] for q in questions]
    finally:
        cache.close()
        conn.close()
    gate = ({"name": "llm", "model": params.llm_gate_model, "effort": params.llm_gate_effort} if name == "llm"
            else {"name": "semantic", "delta": params.delta})
    config = {"system": system.name, "k": None, "retrieval": system.retrieval, "model": MODEL, "gate": gate,
              "plans_from": full.name,
              **({"retried_from": retry.name, "retried": sorted(redone)} if retry is not None else {}), "questions": str(PTT_V2.name), "db": str(db.name), "started_at": started,
              "finished_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    path = save_run(config, outcomes, RUNS)
    errors = sum(o.error is not None for o in outcomes)
    print(f"結果檔：{path}（{len(outcomes)} 題，出錯 {errors} 題）")
    return 0


def latest_semantic_judge(delta: float) -> Path | None:
    """runs/ 裡 semantic 有很多個 δ（敏感度實驗），只拿指定 δ 最新的一份。"""
    found = [p for p in sorted(RUNS.glob("*_gate_semantic*.judge.json"))
             if load_judge(p)[0]["run_config"]["gate"]["delta"] == delta]
    return found[-1] if found else None


def default_judges() -> list[Path]:
    found = [latest(BASELINE_RUNS, "*_strag*.judge.json"), latest(RUNS, "*_gate_llm*.judge.json"),
             latest_semantic_judge(Params().delta)]
    return [p for p in found if p is not None]


async def sweep_cmd(deltas: list[float], full: Path | None, db: Path) -> int:
    full = full or latest_full_run()
    if full is None:
        print("錯誤：找不到 full（baseline 的 strag）結果檔，先跑 eval.baseline run --system strag", file=sys.stderr)
        return 1
    questions = load_questions()
    rows = await sweep(deltas, questions, load_plans(full), fragments_of(db), db, baseline_embedder)
    path = write_csv(rows)
    print(f"{path.relative_to(ROOT)}：{len(deltas)} 個 δ × {len(questions)} 題（計畫來自 {full.name}）")
    return 0


def report_cmd(paths: list[Path], db: Path) -> int:
    paths = paths or default_judges()
    if not paths:
        print("錯誤：沒有評審檔", file=sys.stderr)
        return 1
    questions = load_questions()
    fragments = fragments_of(db)
    rows, runs = [], []
    for p in paths:
        data, run_path = load_judge(p)
        config, outcomes = load_run(run_path)
        verdicts = [Verdict(**v) for v in data["questions"]]
        rows.append(Row(gate_label(config), config["retrieval"],
                        score_run(questions, outcomes, verdicts, fragments, config["retrieval"])))
        runs.append((config, outcomes))
    print("評審檔：" + "、".join(str(p.relative_to(ROOT)) if p.is_absolute() else str(p) for p in paths) + "\n")
    print(report(rows))
    print("## 成本\n\n" + cost_table(runs) + "\n\n" + NOTE)
    return 0


async def jev_usage_cmd(full: Path | None, db: Path) -> int:
    full = full or latest_full_run()
    if full is None:
        print("錯誤：找不到 full（baseline 的 strag）結果檔，先跑 eval.baseline run --system strag", file=sys.stderr)
        return 1
    rows = await usage.measure(load_questions(), load_plans(full), db, baseline_embedder)
    path = usage.write_csv(rows)
    calls, tokens = sum(r["calls"] for r in rows), sum(r["input_tokens"] for r in rows)
    print(f"{path.relative_to(ROOT)}：{len(rows)} 題，{calls} 次呼叫，輸入 {tokens} token"
          f"（沒回報用量 {sum(r['unreported'] for r in rows)} 次；計畫來自 {full.name}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m strag_v3.eval.gate", description="相關判斷（gate）的比較實驗")
    ap.add_argument("--db", type=Path, default=DB, help=f"片段所在的 DB（預設 {DB.relative_to(ROOT)}）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="跑 50 題，存結果檔")
    s.add_argument("--system", choices=GATES, required=True,
                   help="llm：Haiku（effort medium）判斷相關；semantic：語意向量的相對門檻。jev 就是 full，不在這裡跑")
    s.add_argument("--delta", type=float, default=Params().delta, help="semantic 的 δ（預設 %(default)s）")
    s.add_argument("--full", type=Path, default=None, help="沿用哪一份 full 結果檔的檢索計畫（預設最新的 *_strag.json）")
    s.add_argument("--retry", type=Path, default=None, help="只重跑這份結果檔裡出錯的題，其他題照抄，另存新檔")
    s.add_argument("--concurrency", type=int, default=4, help="同時跑幾題")
    s = sub.add_parser("judge", help="評審一份結果檔")
    s.add_argument("run", type=Path)
    s.add_argument("--n", type=int, default=1, help="同一個判斷重問幾次（分數只用第 1 次）")
    s.add_argument("--concurrency", type=int, default=8)
    s = sub.add_parser("report", help="印品質表和成本表")
    s.add_argument("judges", type=Path, nargs="*")
    s = sub.add_parser("sweep", help="semantic 的 δ 檢索掃描：只跑檢索，寫每個 δ × 每題的檢索分數")
    s.add_argument("--deltas", type=float, nargs="+", default=DELTAS, help="預設 0.00–0.60、每 0.02 一格")
    s.add_argument("--full", type=Path, default=None, help="沿用哪一份 full 結果檔的檢索計畫（預設最新的 *_strag.json）")
    s = sub.add_parser("jev-usage", help="重問 JEV 量 JEV gate 的 token 用量（會實際呼叫 JEV）")
    s.add_argument("--full", type=Path, default=None, help="沿用哪一份 full 結果檔的檢索計畫（預設最新的 *_strag.json）")
    a = ap.parse_args(argv)
    load_env()
    if a.cmd == "run":
        return asyncio.run(run_cmd(a.system, a.delta, a.full, a.retry, a.db, a.concurrency))
    if a.cmd == "judge":
        return asyncio.run(judge_cmd(a.run, a.n, a.db, a.concurrency))
    if a.cmd == "report":
        return report_cmd(a.judges, a.db)
    if a.cmd == "sweep":
        return asyncio.run(sweep_cmd(a.deltas, a.full, a.db))
    if a.cmd == "jev-usage":
        return asyncio.run(jev_usage_cmd(a.full, a.db))
    return 1


if __name__ == "__main__":
    sys.exit(main())
