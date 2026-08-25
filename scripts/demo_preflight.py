"""Fail-fast preflight checks for the local competition demonstration."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Callable


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查竞赛演示所需的数据、索引、模板和本机工具")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--code-root", default=None)
    parser.add_argument("--web-port", type=int, default=8080)
    parser.add_argument("--rpa-port", type=int, default=8090)
    parser.add_argument("--skip-port-check", action="store_true")
    parser.add_argument("--json-output")
    return parser


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def main() -> int:
    args = _parser().parse_args()
    root = Path(args.project_root).resolve()
    code_root = Path(args.code_root).resolve() if args.code_root else root
    if str(code_root) not in sys.path:
        sys.path.insert(0, str(code_root))

    from app.data_quality import validate_data_dir
    from app.knowledge import search_knowledge
    from app.reporting import build_report_contract
    from app.reporting.renderers import _find_libreoffice

    checks: list[dict[str, object]] = []

    def run(name: str, operation: Callable[[], str]) -> None:
        try:
            detail = operation()
            checks.append({"name": name, "status": "PASS", "detail": detail})
        except Exception as exc:
            checks.append({"name": name, "status": "FAIL", "detail": str(exc)})

    def check_python() -> str:
        if sys.version_info < (3, 11):
            raise RuntimeError("需要Python 3.11或更高版本")
        return f"Python {sys.version.split()[0]} · {sys.executable}"

    def check_required_files() -> str:
        required = (
            code_root / "requirements.txt",
            root / "04_报告模板" / "月度成本分析报告模板.docx",
            root / "04_报告模板" / "月度成本分析报告模板.md",
            root / "06_知识证据索引" / "manifest.json",
            root / "06_知识证据索引" / "pages.jsonl",
            root / "06_知识证据索引" / "vectors.npz",
            code_root / "app" / "dashboard" / "static" / "index.html",
            code_root / "app" / "mock_rpa" / "server.py",
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise RuntimeError("缺少文件：" + "、".join(missing))
        return f"{len(required)}项关键文件齐全"

    def check_data() -> str:
        report = validate_data_dir(root)
        if report.errors:
            raise RuntimeError(f"数据质量存在{len(report.errors)}项阻断错误")
        return (
            f"{report.status} · {len(report.files)}个CSV · "
            f"{sum(item.valid_row_count for item in report.files.values())}条有效原始记录"
        )

    def check_knowledge() -> str:
        result = search_knowledge(
            root / "06_知识证据索引",
            "银黄口服液 金银花 配方",
            product="银黄口服液",
            top_k=1,
        )
        if result.status != "PASS" or not result.hits:
            raise RuntimeError("知识索引检索未返回受治理证据")
        return (
            f"{result.retrieval_mode} · BM25={result.bm25_weight:.2f} · "
            f"向量={result.vector_weight:.2f}"
        )

    def check_contract() -> str:
        contract = build_report_contract(
            root,
            root / "06_知识证据索引",
            "银黄口服液",
            "2026-05",
        )
        if contract.validation_status != "PASS" or len(contract.fields) != 107:
            raise RuntimeError("107字段报告契约未通过")
        return f"{contract.report_number} · 107字段 · PASS"

    def check_libreoffice() -> str:
        executable = _find_libreoffice()
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("LibreOffice版本检查失败")
        return (result.stdout or result.stderr).strip()

    def check_ports() -> str:
        occupied = [
            port
            for port in (args.web_port, args.rpa_port)
            if not _port_available(port)
        ]
        if occupied:
            raise RuntimeError("端口被占用：" + "、".join(str(port) for port in occupied))
        return f"127.0.0.1:{args.web_port}、127.0.0.1:{args.rpa_port}可用"

    run("Python运行时", check_python)
    run("关键文件", check_required_files)
    run("源数据质量", check_data)
    run("知识索引", check_knowledge)
    run("报告契约", check_contract)
    run("LibreOffice", check_libreoffice)
    if not args.skip_port_check:
        run("本机端口", check_ports)

    status = "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL"
    payload = {"status": status, "project_root": str(root), "checks": checks}
    if args.json_output:
        output = Path(args.json_output)
        if not output.is_absolute():
            output = root / output
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(output)

    print(f"演示前预检：{status}")
    for item in checks:
        print(f"[{item['status']}] {item['name']}：{item['detail']}")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
