"""選擇題評測：`PYTHONPATH=.:src .venv/bin/python -m v4.eval mc_bank --system S0,S1,S2,S3`。

每題把題目和四個選項當成一句問題丟給系統（見 `systems.py`），再讓 Sonnet 讀答案、判斷選了哪個選項（每個系統都一樣），
跟 `ans` 比對。最後印出各系統的答對率。
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from service import llm
from service.env import load_env
from v4.rag import config

from .systems import NAMES, SONNET, SYSTEMS

QUESTION_DIR = Path(__file__).resolve().parent / "question"
RESULT_DIR = Path(__file__).resolve().parent / "result"
OPTIONS = ("op1", "op2", "op3", "op4")

PICK_SYSTEM = """你會讀到一題選擇題（題目、四個選項）和一個回答。判斷這個回答選的是哪一個選項。
- 只看回答的結論明確對應到哪一個選項，不要自己判斷哪個選項才對。
- 回答沒有選、同時選了不只一個、或結論對應不到任何選項時，回 0。
輸出 `{"choice": 0～4}`。"""
PICK_SCHEMA = {
    "type": "object",
    "properties": {"choice": {"type": "integer", "enum": [0, 1, 2, 3, 4]}},
    "required": ["choice"],
    "additionalProperties": False,
}


def bank_path(name: str) -> Path:
    """給檔名（`mc_bank`、`mc_bank.json`）就到 `question/` 找；給路徑就用路徑。"""
    p = Path(name)
    if p.exists():
        return p
    return QUESTION_DIR / (name if name.endswith(".json") else f"{name}.json")


def with_options(q: dict) -> str:
    lines = [q["question"], "", "選項："] + [f"{i}. {q[k]}" for i, k in enumerate(OPTIONS, 1)]
    return "\n".join(lines + ["", "請從上面四個選項選一個。"])


async def pick(q: dict, answer: str) -> int:
    user = f"<question>\n{with_options(q)}\n</question>\n\n<answer>\n{answer}\n</answer>"
    out = await llm.call(PICK_SYSTEM, user, PICK_SCHEMA, SONNET, "low")
    return int(out["choice"])


def build(name: str, db: Path):
    """建不起來（例如 S1、S2 算 embedding 失敗）時回傳例外，這個系統每題都算出錯，其他系統照跑。"""
    try:
        return SYSTEMS[name](db)
    except Exception as e:
        return e


async def one(system, n: int, q: dict, log_dir: Path) -> tuple[bool, str]:
    """回傳（答對沒、進度那一行要接的說明）。出錯算錯。"""
    if isinstance(system, Exception):
        return False, f"出錯，算錯：系統建不起來：{type(system).__name__}: {system}"
    try:
        answer = await system.answer(with_options(q), log_dir / f"Q{n}.log")
        choice = await pick(q, answer)
    except Exception as e:
        return False, f"出錯，算錯：{type(e).__name__}: {e}"
    ok = choice == int(q["ans"])
    return ok, f"{'✓' if ok else '✗'} 選 {choice or '（沒選）'}，正解 {q['ans']}"


async def run(names: list[str], questions: list[dict], db: Path, log_dir: Path, jobs: int) -> dict[str, int]:
    systems = {s: build(s, db) for s in names}
    gate = asyncio.Semaphore(jobs)
    total, done = len(names) * len(questions), 0
    correct = dict.fromkeys(names, 0)

    async def guarded(s: str, n: int, q: dict) -> None:
        nonlocal done
        async with gate:
            ok, note = await one(systems[s], n, q, log_dir / s)
        done += 1
        correct[s] += ok
        print(f"[{done}/{total}] {s} Q{n} {note}", flush=True)

    await asyncio.gather(*(guarded(s, n, q) for s in names for n, q in enumerate(questions, 1)))
    return correct


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m v4.eval", description="用選擇題庫測各系統的答對率")
    parser.add_argument("bank", help="題庫：question/ 裡的檔名（例如 mc_bank），或 JSON 路徑")
    parser.add_argument("--system", default="S3",
                        help="跑哪些系統，逗號分隔（預設 S3）：" + "、".join(f"{k} {v}" for k, v in NAMES.items()))
    parser.add_argument("--db", type=Path, default=config.DB_PATH, help=f"資料庫（預設 {config.DB_PATH}）")
    parser.add_argument("--log-dir", type=Path, help=f"每題的 log 寫到這個資料夾（預設 {RESULT_DIR}/<題庫名稱>/<系統>）")
    parser.add_argument("--jobs", type=int, default=1, help="同時跑幾題（預設 1）")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs 至少 1")
    canonical = {k.upper(): k for k in SYSTEMS}   # 大小寫不拘：s3-sonnet、S3-SONNET 都算 S3-sonnet
    names = [canonical.get(s.strip().upper(), s.strip()) for s in args.system.split(",") if s.strip()]
    if bad := [s for s in names if s not in SYSTEMS]:
        parser.error(f"沒有這些系統：{'、'.join(bad)}")

    path = bank_path(args.bank)
    if not path.exists():
        parser.error(f"找不到題庫 {path}")
    questions = json.loads(path.read_text())
    log_dir = args.log_dir or RESULT_DIR / path.stem
    load_env()
    correct = asyncio.run(run(names, questions, args.db, log_dir, args.jobs))
    for s in names:
        print(f"{s} {NAMES[s]} 答對率 {correct[s]}/{len(questions)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
