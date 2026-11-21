"""讀 `docs/問題分類_*.csv` 的題目和人工標的題型（規格 19.1）。

題型從檔名來：`問題分類_1_點.csv` 是點，到 `問題分類_5_無法處理.csv`。CSV 裡的子類不用。
"""
import csv
import re
from dataclasses import dataclass
from pathlib import Path

from v4.rag.data_schema import QTYPES

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "docs"
PATTERN = "問題分類_*.csv"
_NAME = re.compile(r"問題分類_\d+_(.+)\.csv")


@dataclass(frozen=True)
class LabeledQuestion:
    key: str            # 「檔名#第幾列」（第幾列從 1 起算，不含標題列），重跑時用來找已經做過的題
    text: str           # 問題內容
    label: str          # 人工標的題型
    article_id: str
    no_in_article: str  # 篇內第幾題


def load(folder: Path = DOCS) -> list[LabeledQuestion]:
    """照檔名、再照列的順序。問題內容是空的列略過。"""
    out: list[LabeledQuestion] = []
    for path in sorted(folder.glob(PATTERN)):
        m = _NAME.fullmatch(path.name)
        if not m or m.group(1) not in QTYPES:
            raise ValueError(f"{path.name}：檔名看不出題型（要是 問題分類_N_{{{'、'.join(QTYPES)}}}.csv）")
        with path.open(encoding="utf-8-sig", newline="") as f:
            for i, row in enumerate(csv.DictReader(f), 1):
                text = (row.get("問題內容") or "").strip()
                if text:
                    out.append(LabeledQuestion(f"{path.name}#{i}", text, m.group(1),
                                               (row.get("article_id") or "").strip(),
                                               (row.get("篇內第幾題") or "").strip()))
    return out
