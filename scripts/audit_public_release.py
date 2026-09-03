"""Fail a public demo release when runtime artifacts or secrets leak in."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


FORBIDDEN_TOP_LEVEL = {
    "07_报告输出", "08_RPA任务输出",
    "09_审核工作台", "10_批量运行", "8.16调整更新", "tmp", "output",
}
FORBIDDEN_SUFFIXES = {".sqlite3", ".pem", ".p12"}
IGNORED_TOP_LEVEL = {".git"}
TEXT_SUFFIXES = {
    ".py", ".ps1", ".cmd", ".sh", ".md", ".txt", ".json", ".yaml",
    ".yml", ".toml", ".html", ".css", ".js", ".csv",
}
SECRET_PATTERNS = {
    "DashScope/OpenAI样式密钥": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "GitHub令牌": re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    "本机绝对用户路径": re.compile(r"(?i)C:[\\/]Users[\\/]16006(?:[\\/]|$)"),
}


def audit(root: Path) -> list[str]:
    issues: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in IGNORED_TOP_LEVEL:
            continue
        if relative.parts and relative.parts[0] in FORBIDDEN_TOP_LEVEL:
            issues.append(f"禁止目录：{relative.as_posix()}")
            continue
        if not path.is_file():
            continue
        if path.name == ".env":
            issues.append("发现真实.env文件")
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            issues.append(f"禁止文件类型：{relative.as_posix()}")
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
    parser = argparse.ArgumentParser(description="审计可公开GitHub仓库")
    parser.add_argument("root", nargs="?", default="release/github-public")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"公开仓库目录不存在：{root}")
        return 2
    issues = audit(root)
    if issues:
        print("公开仓库审计：FAIL")
        for issue in issues:
            print(f"- {issue}")
        return 1
    file_count = sum(
        1 for path in root.rglob("*")
        if path.is_file()
        and not (path.relative_to(root).parts and path.relative_to(root).parts[0] in IGNORED_TOP_LEVEL)
    )
    print(f"公开仓库审计：PASS · {file_count}个文件 · 未发现运行制品或密钥")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
