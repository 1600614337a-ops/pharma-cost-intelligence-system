"""Build a confidential, self-contained judge package and ZIP archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path


CONTROLLED_DIRECTORIES = (
    "01_成本明细数据",
    "02_行业参考数据",
    "03_制药知识文档",
    "04_报告模板",
    "05_RPA接口文档",
    "06_知识证据索引",
)
FORMAL_DOCUMENTS = {
    "00_项目规范/成本智能分析系统竞赛技术方案_V1.7.docx": "docs/成本智能分析系统竞赛技术方案_V1.7.docx",
    "00_项目规范/成本智能分析系统竞赛技术方案_V1.7.pdf": "docs/成本智能分析系统竞赛技术方案_V1.7.pdf",
}
EXCLUDED_NAMES = {
    ".git", ".env", "__pycache__", ".pytest_cache", "tmp", "output",
    "07_报告输出", "08_RPA任务输出", "09_审核工作台", "10_批量运行",
}
TEXT_SUFFIXES = {
    ".py", ".ps1", ".cmd", ".sh", ".md", ".txt", ".json", ".yaml",
    ".yml", ".toml", ".html", ".css", ".js", ".csv",
}
SECRET_PATTERNS = {
    "DashScope/OpenAI样式密钥": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "GitHub令牌": re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
}


def _remove_readonly(function, path: str, _error) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name for name in names
        if name in EXCLUDED_NAMES or name.endswith((".pyc", ".pyo"))
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _audit_secrets(root: Path) -> list[str]:
    issues: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in EXCLUDED_NAMES for part in relative.parts):
            issues.append(f"发现禁止内容：{relative.as_posix()}")
            continue
        if path.suffix.casefold() not in TEXT_SUFFIXES and path.name not in {
            "Dockerfile", ".gitignore", ".gitattributes", ".dockerignore", ".env.example",
        }:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8-sig", errors="replace")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                issues.append(f"{label}：{relative.as_posix()}")
    return sorted(set(issues))


def main() -> int:
    parser = argparse.ArgumentParser(description="整理评委完整一键运行包")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--public-repo", default="release/github-public")
    parser.add_argument("--output", default="release/judge-complete-package")
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    public_repo = (root / args.public_repo).resolve()
    output = (root / args.output).resolve()
    release_root = (root / "release").resolve()
    if output == release_root or not output.is_relative_to(release_root):
        raise RuntimeError("评委包输出必须位于项目release目录的子目录中")
    if not public_repo.is_dir():
        raise RuntimeError(f"公开代码目录不存在：{public_repo}")

    missing = [name for name in CONTROLLED_DIRECTORIES if not (root / name).is_dir()]
    if missing:
        raise RuntimeError("缺少受控目录：" + "、".join(missing))
    missing_documents = [source for source in FORMAL_DOCUMENTS if not (root / source).is_file()]
    if missing_documents:
        raise RuntimeError("缺少正式交付文档：" + "、".join(missing_documents))

    temporary = output.with_name(output.name + ".building")
    for target in (temporary, output):
        if target.exists():
            shutil.rmtree(target, onexc=_remove_readonly)

    shutil.copytree(public_repo, temporary, ignore=_ignore)
    for directory in CONTROLLED_DIRECTORIES:
        shutil.copytree(root / directory, temporary / directory, ignore=_ignore)
    for source, destination in FORMAL_DOCUMENTS.items():
        target = temporary / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / source, target)
    shutil.copy2(root / "README_JUDGE.md", temporary / "README_评委运行说明.md")

    issues = _audit_secrets(temporary)
    if issues:
        print("评委包安全审计：FAIL")
        for issue in issues:
            print(f"- {issue}")
        shutil.rmtree(temporary, onexc=_remove_readonly)
        return 1

    manifest = {
        "package_type": "confidential-judge-complete-package",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "contains_controlled_competition_data": True,
        "public_distribution_allowed": False,
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
    manifest_path = temporary / "JUDGE_PACKAGE_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)

    zip_path = output.with_name(output.name + ".zip")
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(output), "zip", root_dir=output)

    total_bytes = sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
    print(
        f"评委完整包：PASS · {len(manifest['files']) + 1}个文件 · "
        f"{total_bytes / 1024 / 1024:.2f} MiB · 未发现API Key或运行制品"
    )
    print(f"目录：{output}")
    print(f"ZIP：{zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
