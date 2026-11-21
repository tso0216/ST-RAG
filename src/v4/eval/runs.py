"""結果檔的讀寫（規格 19.3）：`src/v4/eval/runs/<時間>_<名稱>.json`，不覆寫；重跑時找出已經跑完的。

- 分流評估一次一個檔，名稱是 `route`，內容有 `kind: "route"`、每一題的結果、正確率、混淆矩陣。
- 回答評估一篇一個檔，名稱是文章 id，內容有 `kind: "run"`、文章 id、完整紀錄；出錯的那篇記下錯誤。
"""
import json
import re
from datetime import datetime
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent / "runs"
ROUTE = "route"
RUN = "run"


def new_path(name: str, folder: Path = RUNS_DIR, now: datetime | None = None) -> Path:
    """還不存在的檔名：時間加名稱；同一秒有同名的就加 -2、-3。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^\w.\-]", "_", name)
    path = folder / f"{stamp}_{safe}.json"
    n = 2
    while path.exists():
        path = folder / f"{stamp}_{safe}-{n}.json"
        n += 1
    return path


def save(name: str, data: dict, folder: Path = RUNS_DIR, now: datetime | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = new_path(name, folder, now)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    return path


def load_all(folder: Path = RUNS_DIR, kind: str | None = None) -> list[tuple[Path, dict]]:
    """照檔名（時間）排；讀不了的檔略過。"""
    out = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and (kind is None or data.get("kind") == kind):
            out.append((path, data))
    return out


def finished_articles(folder: Path = RUNS_DIR) -> set[str]:
    """已經有結果（沒出錯）的文章 id。"""
    return {d["文章"] for _, d in load_all(folder, RUN) if not d.get("錯誤") and d.get("文章")}


def finished_routes(folder: Path = RUNS_DIR) -> dict[str, dict]:
    """之前的分流評估裡已經有結果的題：key → 那一題的結果（新的檔蓋過舊的）。"""
    out: dict[str, dict] = {}
    for _, d in load_all(folder, ROUTE):
        for r in d.get("每一題", []):
            if r.get("key") and r.get("預測") and not r.get("錯誤"):
                out[r["key"]] = r
    return out
