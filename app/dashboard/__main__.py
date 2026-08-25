"""Run the unified dashboard on localhost."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .web import create_dashboard_app


def main() -> None:
    parser = argparse.ArgumentParser(description="启动制药成本智能分析主页面")
    parser.add_argument("--data-dir", default=".", help="项目数据根目录")
    parser.add_argument("--index-dir", default=None, help="知识证据索引目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--rpa-base-url",
        default="http://127.0.0.1:8090",
        help="本机模拟RPA根地址，仅允许localhost/回环地址",
    )
    args = parser.parse_args()
    app = create_dashboard_app(
        data_dir=Path(args.data_dir),
        index_dir=Path(args.index_dir) if args.index_dir else None,
        rpa_base_url=args.rpa_base_url,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
