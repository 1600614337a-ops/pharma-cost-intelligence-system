"""Run the repository mock RPA application."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="仅在本机回环地址启动赛题模拟RPA")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from app.mock_rpa.server import app

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
