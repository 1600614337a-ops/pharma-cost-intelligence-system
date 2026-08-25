"""Inspect or back up the review SQLite database without modifying the source."""

from __future__ import annotations

import argparse
import json

from app.review.operations import backup_sqlite_database, inspect_sqlite_database, restore_sqlite_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="审核数据库运行检查与安全备份")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="只读检查数据库")
    inspect_parser.add_argument("database")
    backup_parser = subparsers.add_parser("backup", help="创建一致性备份")
    backup_parser.add_argument("database")
    backup_parser.add_argument("output_directory")
    restore_parser = subparsers.add_parser("restore", help="恢复到一个不存在的新文件")
    restore_parser.add_argument("backup")
    restore_parser.add_argument("target")
    args = parser.parse_args()
    if args.operation == "inspect":
        result = inspect_sqlite_database(args.database)
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0 if result.status == "PASS" else 2
    if args.operation == "backup":
        result = backup_sqlite_database(args.database, args.output_directory)
    else:
        result = restore_sqlite_backup(args.backup, args.target)
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
