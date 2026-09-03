"""Build a reproducible, self-contained competition-demo GitHub repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT_FILES = (
    "Dockerfile", "compose.yaml", ".dockerignore", ".env.example", ".gitignore",
    ".gitattributes", "requirements.txt", "requirements-llamaindex.txt",
    "THIRD_PARTY_NOTICES.md", "启动演示系统.cmd", "停止演示系统.cmd",
    "演示前预检.cmd", "配置通义千问API.cmd", "启动跨环境演示.cmd",
    "停止跨环境演示.cmd", "start-docker.sh", "stop-docker.sh",
)
SOURCE_DIRECTORIES = ("app", "scripts", "tests")
DEMO_DIRECTORIES = (
    "01_成本明细数据",
    "02_行业参考数据",
    "03_制药知识文档",
    "04_报告模板",
    "05_RPA接口文档",
    "06_知识证据索引",
)
FORMAL_DOCUMENTS = (
    "（创灵境）成本智能分析系统技术方案文档.docx",
    "（创灵境）成本智能分析系统技术方案文档.pdf",
    "（创灵境）成本智能分析系统竞赛评测报告.docx",
    "（创灵境）成本智能分析系统竞赛评测报告.pdf",
)
PUBLIC_DOCUMENTS = {
    "00_项目规范/21_竞赛技术方案.md": "docs/竞赛技术方案.md",
    "00_项目规范/22_三场景竞赛评测报告.md": "docs/三场景竞赛评测报告.md",
    "00_项目规范/23_第三方依赖与许可证清单.md": "docs/第三方依赖与许可证清单.md",
    "00_项目规范/24_一键启动与演示前预检报告.md": "docs/一键启动与演示前预检报告.md",
    "00_项目规范/32_端到端验收与交付检查报告.md": "docs/端到端验收与交付检查报告.md",
    "00_项目规范/33_前三项交付与公开仓库检查报告.md": "docs/前三项交付与公开仓库检查报告.md",
}


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name for name in names
        if name in {"__pycache__", ".pytest_cache", ".DS_Store"}
        or name.endswith((".pyc", ".pyo"))
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _remove_readonly(function, path: str, _error) -> None:
    """Allow reproducible rebuilds after a Windows Git repository was created."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="整理可公开GitHub仓库")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--output", default="release/github-public")
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    output = Path(args.output)
    output = (root / output).resolve() if not output.is_absolute() else output.resolve()
    release_root = (root / "release").resolve()
    if output == release_root or not output.is_relative_to(release_root):
        raise RuntimeError("公开仓库输出必须位于项目release目录的子目录中")

    temporary = output.with_name(output.name + ".building")
    for target in (temporary, output):
        if target.exists():
            shutil.rmtree(target, onexc=_remove_readonly)
    temporary.mkdir(parents=True)

    for directory in SOURCE_DIRECTORIES:
        shutil.copytree(root / directory, temporary / directory, ignore=_ignore)
    for directory in DEMO_DIRECTORIES:
        shutil.copytree(root / directory, temporary / directory, ignore=_ignore)
    for filename in ROOT_FILES:
        shutil.copy2(root / filename, temporary / filename)
    shutil.copy2(root / "README_PUBLIC.md", temporary / "README.md")
    for filename in FORMAL_DOCUMENTS:
        shutil.copy2(root / filename, temporary / filename)
    for source_name, target_name in PUBLIC_DOCUMENTS.items():
        target = temporary / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / source_name, target)

    manifest = {
        "release_format": "github-public-self-contained-demo",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "competition_demo_data_included": True,
        "requires_secure_data_overlay": False,
        "api_key_included": False,
        "files": [],
    }
    for path in sorted(temporary.rglob("*")):
        if path.is_file():
            manifest["files"].append({
                "path": path.relative_to(temporary).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            })
    (temporary / "PUBLIC_RELEASE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    temporary.replace(output)

    audit = subprocess.run(
        [sys.executable, str(root / "scripts" / "audit_public_release.py"), str(output)],
        cwd=root,
        check=False,
    )
    if audit.returncode != 0:
        return audit.returncode
    print(f"公开仓库已整理：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
