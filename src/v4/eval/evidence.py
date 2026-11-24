"""證據召回：不重跑系統，從已經跑完的 log 算每題的關鍵證據有沒有被找到、有沒有被引用。

`PYTHONPATH=.:src .venv/bin/python -m v4.eval.evidence ptt_bank_1-10 --system S1-sonnet,S3-sonnet`

題庫每題的 `evidence` 是幾個關鍵事實，每個事實列出能佐證它的編號：原文段落（`chunk M.….A.…#k`，S1 用）
和結構化經驗（`place_note`／`move`／`day_note`／`day` 編號，S2、S3 用）。一個事實只要其中一個編號出現就算找到。
- 找到：編號出現在答案之前（S1 是送給模型的段落，S3 是工具回傳）。
- 引用：編號出現在答案裡。
"""
import argparse
import json
import re
import sys
from pathlib import Path

from .__main__ import RESULT_DIR, bank_path

ANSWER = re.compile(r"^#{1,2} 答案$", re.M)
CHUNK = re.compile(r"(?:chunk )?M?\.?(\d{10}\.A\.[0-9A-F]{3}#\d+)")
SHORT_CHUNK = re.compile(r"(?<![\w.])A\.([0-9A-F]{3}#\d+)")       # 答案裡偶爾簡寫成 [A.094#3]
RECORD = re.compile(r"\b(place_note|move|day_note|day) (\d+-\d+)")


def split_log(text: str) -> tuple[str, str]:
    """（答案之前、答案）。S3 一題可能有多個「## 答案」，取最後一個。"""
    marks = list(ANSWER.finditer(text))
    if not marks:
        return text, ""
    return text[:marks[-1].start()], text[marks[-1].end():]


def ids_in(text: str, known_chunks: set[str] = frozenset()) -> set[str]:
    out = {f"chunk M.{m}" for m in CHUNK.findall(text)}
    out |= {f"{kind} {no}" for kind, no in RECORD.findall(text)}
    for tail in SHORT_CHUNK.findall(text):
        hits = [c for c in known_chunks if c.endswith(f".A.{tail}")]
        if len(hits) == 1:
            out.add(hits[0])
    return out


def score(q: dict, log: Path) -> tuple[list[bool], list[bool]]:
    """每個事實（找到、引用）。"""
    before, answer = split_log(log.read_text())
    found = ids_in(before)
    cited = ids_in(answer, {i for i in found if i.startswith("chunk ")})
    facts = q["evidence"]
    return [bool(found & set(f["ids"])) for f in facts], [bool(cited & set(f["ids"])) for f in facts]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m v4.eval.evidence", description="從現有 log 算證據召回")
    parser.add_argument("bank", help="有 evidence 欄位的題庫")
    parser.add_argument("--system", required=True, help="逗號分隔，對應 log 資料夾名稱")
    parser.add_argument("--log-dir", type=Path, help=f"log 根目錄（預設 {RESULT_DIR}/<題庫名稱>）")
    args = parser.parse_args(argv)
    path = bank_path(args.bank)
    questions = json.loads(path.read_text())
    if any("evidence" not in q for q in questions):
        parser.error(f"{path.name} 有題目沒有 evidence 欄位")
    root = args.log_dir or RESULT_DIR / path.stem
    for s in [x.strip() for x in args.system.split(",") if x.strip()]:
        found_n = cited_n = full_q = 0
        rows, missing = [], 0
        facts = questions_done = 0
        for n, q in enumerate(questions, 1):
            log = root / s / f"Q{n}.log"
            if not log.exists():
                rows.append(f"  Q{n} 沒有 log，不計")
                missing += 1
                continue
            found, cited = score(q, log)
            facts, questions_done = facts + len(found), questions_done + 1
            found_n, cited_n, full_q = found_n + sum(found), cited_n + sum(cited), full_q + all(found)
            mark = lambda xs: "".join("✓" if x else "✗" for x in xs)
            rows.append(f"  Q{n} 找到 {mark(found)} 引用 {mark(cited)}")
        if not questions_done:
            print(f"{s}｜{root / s} 沒有任何 log")
            continue
        print(f"{s}｜找到 {found_n}/{facts} 個事實｜每個事實都找到的題目 {full_q}/{questions_done}｜引用 {cited_n}/{facts}"
              + (f"｜缺 {missing} 題 log" if missing else ""))
        print("\n".join(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
