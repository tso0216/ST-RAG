"""評審可信度實驗的命令列（從專案根目錄跑，`PYTHONPATH=.:src`）。

    python -m strag_v3.eval.judge jev [--n 3]     # JEV 重問 n 次，評審檔存到 runs/<時間>_jev_n<n>.judge.json
    python -m strag_v3.eval.judge llm             # LLM 評審（sonnet、medium），存到 runs/<時間>_llm.judge.json
    python -m strag_v3.eval.judge report          # 拿 runs/ 裡各自最新的一份，寫 result/ 的報告和 CSV

三個命令都只看 full 的結果檔（baseline 最新的 `strag`，`--run` 可以另外指定）；
`report` 的 JEV 那一邊是那份結果檔原本的 baseline 評審檔（比較表的數字用的那一份）。
"""
import argparse
import asyncio
import csv
import json
import sys
from dataclasses import asdict
from pathlib import Path

from service.env import load_env
from service.jev import JEVClient

from ..baseline.__main__ import DB, ROOT, fragments_of, print_disagreements
from ..baseline.judge import Judge, Verdict, agreement, disagreements
from ..baseline.questions import load_questions
from ..baseline.results import RUNS as BASELINE_RUNS
from ..baseline.results import load_run, now
from .llm_judge import EFFORT, MODEL, LLMJudge
from .report import (COMPARE_HEAD, STABILITY_HEAD, compare_report, compare_rows, stability_report, stability_rows)

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
RESULT = HERE / "result"


def latest(folder: Path, pattern: str) -> Path | None:
    found = sorted(folder.glob(pattern))
    return found[-1] if found else None


def latest_full_run() -> Path | None:
    runs = [p for p in BASELINE_RUNS.glob("*_strag.json") if not p.name.endswith(".judge.json")]
    return sorted(runs)[-1] if runs else None


def save(name: str, run: Path, judge: Judge, run_config: dict, verdicts: list[Verdict]) -> Path:
    path = RUNS / f"{now()}_{name}.judge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x") as f:  # 不覆寫
        json.dump({"config": judge.config(), "run": run.name, "run_config": run_config,
                   "questions": [asdict(v) for v in verdicts],
                   "disagreements": disagreements(run_config["system"], verdicts),
                   "agreement": agreement(verdicts)}, f, ensure_ascii=False, indent=2)
    return path


def load_verdicts(path: Path) -> tuple[dict, list[Verdict]]:
    data = json.loads(path.read_text())
    return data, [Verdict(**v) for v in data["questions"]]


async def judge_cmd(which: str, run: Path, n: int, db: Path, concurrency: int) -> int:
    config, outcomes = load_run(run)
    fragments = fragments_of(db)
    questions = load_questions()
    if which == "jev":
        client = JEVClient()
        judge: Judge = Judge(client, n=n, concurrency=concurrency)
        try:
            verdicts = await judge.judge(questions, fragments, outcomes)
        finally:
            await client.aclose()
        name = f"jev_n{n}"
    else:
        judge = LLMJudge(n=n, concurrency=concurrency)
        verdicts = await judge.judge(questions, fragments, outcomes)
        name = "llm" if n == 1 else f"llm_n{n}"
    skipped = sum(v.skipped for v in verdicts)
    print(f"{run.name}：評了 {len(verdicts) - skipped} 題，沒有答案不送評審 {skipped} 題")
    if n > 1:
        print_disagreements(disagreements(config["system"], verdicts), agreement(verdicts))
    print(f"評審檔：{save(name, run, judge, config, verdicts)}")
    return 0


def write_csv(path: Path, head: tuple[str, ...], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(rows)


def report_cmd(run: Path) -> int:
    questions = load_questions()
    original = run.with_name(run.stem + ".judge.json")
    repeat_path = latest(RUNS, "*_jev_n*.judge.json")
    llm_path = latest(RUNS, "*_llm.judge.json")
    if repeat_path is None and llm_path is None:
        print("錯誤：runs/ 裡沒有評審檔，先跑 jev 或 llm", file=sys.stderr)
        return 1
    parts = [f"# 評審的可信度\n\n對象：`{run.name}`（full）的 {len(questions)} 題答案。"]
    sources = [f"- full 原本的 JEV 評審檔：`baseline/runs/{original.name}`"]
    repeat = None
    if repeat_path is not None:
        data, repeat = load_verdicts(repeat_path)
        parts.append(stability_report(questions, repeat, data["config"]["model"]))
        write_csv(RESULT / "jev_repeat.csv", STABILITY_HEAD, stability_rows(repeat))
        sources.append(f"- JEV 重問：`runs/{repeat_path.name}`")
    if llm_path is not None:
        if not original.exists():
            print(f"錯誤：找不到 {original}", file=sys.stderr)
            return 1
        (jev_data, jev), (llm_data, llm) = load_verdicts(original), load_verdicts(llm_path)
        c = llm_data["config"]
        parts.append(compare_report(questions, jev, llm, repeat, jev_data["config"]["model"],
                                    f"{c['model']}（{c['effort']}）"))
        u = c.get("usage")
        if u:
            parts.append(f"LLM 評審的用量：{u['calls']} 次呼叫，輸入 {u['input_tokens']:,} token，"
                         f"輸出 {u['output_tokens']:,} token，照 API 定價估 {u['cost_usd']:.2f} USD，"
                         f"API 時間合計 {u['duration_ms'] / 1000:.0f} 秒。")
        write_csv(RESULT / "jev_vs_llm.csv", COMPARE_HEAD, compare_rows(jev, llm, repeat))
        sources.append(f"- LLM 評審：`runs/{llm_path.name}`")
    parts.append("## 資料來源\n\n" + "\n".join(sources))
    text = "\n\n".join(p for p in parts if p) + "\n"
    RESULT.mkdir(parents=True, exist_ok=True)
    (RESULT / "report.md").write_text(text)
    print(text)
    print(f"報告：{RESULT / 'report.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m strag_v3.eval.judge", description="評審的可信度實驗")
    ap.add_argument("--db", type=Path, default=DB, help=f"片段所在的 DB（預設 {DB.relative_to(ROOT)}）")
    ap.add_argument("--run", type=Path, default=None, help="full 的結果檔（預設 baseline/runs 裡最新的 strag）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("jev", help="JEV 重問 n 次")
    s.add_argument("--n", type=int, default=3)
    s.add_argument("--concurrency", type=int, default=8)
    s = sub.add_parser("llm", help=f"LLM 評審（{MODEL}，{EFFORT}）")
    s.add_argument("--n", type=int, default=1)
    s.add_argument("--concurrency", type=int, default=8)
    sub.add_parser("report", help="寫 result/ 的報告和 CSV")
    a = ap.parse_args(argv)
    load_env()
    run = a.run or latest_full_run()
    if run is None:
        print("錯誤：baseline/runs 裡沒有 strag 的結果檔", file=sys.stderr)
        return 1
    if a.cmd == "report":
        return report_cmd(run)
    return asyncio.run(judge_cmd(a.cmd, run, a.n, a.db, a.concurrency))


if __name__ == "__main__":
    sys.exit(main())
