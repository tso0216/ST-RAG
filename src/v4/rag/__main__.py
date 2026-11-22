"""命令列：`python -m v4.rag ask "問題"` 或 `python -m v4.rag ask --article <PTT 文章 JSON>`。"""
import argparse
import asyncio
import sys
from pathlib import Path

from service.env import load_env

from . import config, pipeline
from .log import Log


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m v4.rag", description="用資料庫裡的旅客經驗回答發問文")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="回答一句問題或一篇 PTT 發問文")
    ask.add_argument("question", nargs="?", help="一句問題")
    ask.add_argument("--article", type=Path, help="PTT 文章 JSON")
    ask.add_argument("--db", type=Path, default=config.DB_PATH, help=f"資料庫（預設 {config.DB_PATH}）")
    ask.add_argument("--log", type=Path, help="log 檔的路徑；沒給就不記")
    ask.add_argument("--verbose", action="store_true", help="邊跑邊印步驟")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if bool(args.question) == bool(args.article):
        parser.error("一句問題和 --article 要給其中一個")
    if not args.db.exists():
        print(f"錯誤：找不到資料庫 {args.db}", file=sys.stderr)
        return 1
    load_env()
    log = Log(verbose=args.verbose, path=args.log)
    try:
        article = pipeline.from_file(args.article) if args.article else pipeline.from_question(args.question)
        asyncio.run(pipeline.run(article, args.db, log, question_only=not args.article))
    except KeyboardInterrupt:
        print("中斷", file=sys.stderr)
        return 130
    except Exception as e:
        text = f"{type(e).__name__}: {e}"
        print(f"錯誤：{text}", file=sys.stderr)
        log.error(text)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
