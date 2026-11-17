"""`data/v4_pending/`：存、讀、刪還沒寫入的文章的 llm 解析結果。一篇一個 `<文章 id>.json`。"""
import json
from pathlib import Path

from .data_schema import Parsed


def path_of(pending_dir: Path, article_id: str) -> Path:
    return pending_dir / f"{article_id}.json"


def save(pending_dir: Path, article_id: str, parsed: Parsed) -> None:
    pending_dir.mkdir(parents=True, exist_ok=True)
    path_of(pending_dir, article_id).write_text(
        json.dumps(parsed.to_dict(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def load(pending_dir: Path, article_id: str) -> Parsed | None:
    """沒有這篇的檔案是 None。"""
    path = path_of(pending_dir, article_id)
    if not path.exists():
        return None
    return Parsed.from_dict(json.loads(path.read_text(encoding="utf-8")))


def delete(pending_dir: Path, article_id: str) -> None:
    path_of(pending_dir, article_id).unlink(missing_ok=True)
