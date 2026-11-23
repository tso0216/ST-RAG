"""命令列：`python -m v4.web [--db 路徑] [--port 8765]`，只開在本機。"""
import argparse
import sys
from pathlib import Path

import uvicorn

from service.env import load_env
from v4.rag import config

from .app import create_app


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m v4.web", description="v4 rag 的聊天加地圖介面")
    parser.add_argument("--db", type=Path, default=config.DB_PATH, help=f"資料庫（預設 {config.DB_PATH}）")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not args.db.exists():
        print(f"錯誤：找不到資料庫 {args.db}", file=sys.stderr)
        return 1
    load_env()
    uvicorn.run(create_app(args.db), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
