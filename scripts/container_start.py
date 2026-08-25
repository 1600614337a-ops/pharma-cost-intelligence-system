"""Supervise the dashboard and mock RPA inside the cross-platform container."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动容器化竞赛演示服务")
    parser.add_argument("--project-root", default="/workspace")
    parser.add_argument("--code-root", default="/opt/app")
    parser.add_argument("--web-port", type=int, default=8080)
    parser.add_argument("--rpa-port", type=int, default=8090)
    return parser


def _wait_for(url: str, process: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"服务提前退出，返回码{process.returncode}：{url}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"服务在{timeout:.0f}秒内未就绪：{url}")


def main() -> int:
    args = _parser().parse_args()
    project_root = Path(args.project_root).resolve()
    code_root = Path(args.code_root).resolve()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(code_root)
    environment.setdefault("PYTHONUNBUFFERED", "1")

    preflight = subprocess.run(
        [
            sys.executable,
            str(code_root / "scripts" / "demo_preflight.py"),
            "--project-root",
            str(project_root),
            "--code-root",
            str(code_root),
            "--web-port",
            str(args.web_port),
            "--rpa-port",
            str(args.rpa_port),
            "--skip-port-check",
            "--json-output",
            "tmp/demo_runtime/container-preflight.json",
        ],
        cwd=project_root,
        env=environment,
        check=False,
    )
    if preflight.returncode != 0:
        return preflight.returncode

    children: list[subprocess.Popen] = []

    def stop_children(*_args) -> None:
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop_children)
    signal.signal(signal.SIGINT, stop_children)

    rpa = subprocess.Popen(
        [
            sys.executable,
            str(code_root / "scripts" / "run_mock_rpa_local.py"),
            "--project-root",
            str(code_root),
            "--host",
            "127.0.0.1",
            "--port",
            str(args.rpa_port),
        ],
        cwd=project_root,
        env=environment,
    )
    children.append(rpa)
    _wait_for(f"http://127.0.0.1:{args.rpa_port}/health", rpa, 30)

    web = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app.dashboard",
            "--data-dir",
            str(project_root),
            "--host",
            "0.0.0.0",
            "--port",
            str(args.web_port),
            "--rpa-base-url",
            f"http://127.0.0.1:{args.rpa_port}",
        ],
        cwd=project_root,
        env=environment,
    )
    children.append(web)
    _wait_for(f"http://127.0.0.1:{args.web_port}/health", web, 60)
    print(f"系统已就绪：http://127.0.0.1:{args.web_port}", flush=True)

    try:
        while all(child.poll() is None for child in children):
            time.sleep(1)
    finally:
        stop_children()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
    failed = [child.returncode for child in children if child.returncode not in (0, -15)]
    return failed[0] if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
