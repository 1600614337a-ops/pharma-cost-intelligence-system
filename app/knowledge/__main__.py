"""CLI for building and querying the governed knowledge index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import (
    KnowledgeIndexError,
    KnowledgeRetrievalError,
    build_knowledge_index,
    compare_native_and_llamaindex,
    llamaindex_available,
    search_knowledge,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建和检索制药知识文档页级证据索引。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="从受治理的PDF/DOCX/TXT/Markdown重建混合索引。")
    build.add_argument("--data-dir", default=".", help="项目根目录。")
    build.add_argument(
        "--output-dir", default="06_知识证据索引", help="派生索引输出目录。"
    )
    build.add_argument("--catalog", help="可选JSON文档目录清单；其中路径相对于03_制药知识文档。")
    build.add_argument(
        "--catalog-only",
        action="store_true",
        help="仅导入--catalog中的文档，不包含项目预置的7份PDF。",
    )
    build.add_argument("--json", action="store_true", help="输出JSON清单。")

    search = subparsers.add_parser("search", help="检索页级证据。")
    search.add_argument(
        "--index-dir", default="06_知识证据索引", help="索引目录。"
    )
    search.add_argument("--query", required=True, help="空格分隔的检索词。")
    search.add_argument("--product", help="可选产品过滤。")
    search.add_argument(
        "--document-type",
        action="append",
        choices=["配方", "工艺", "设备", "GMP原文", "GMP摘要", "对标基线", "异常处理"],
        help="可重复指定文档类型。",
    )
    search.add_argument(
        "--regulatory", action="store_true", help="按法规断言规则优先回引GMP原文。"
    )
    search.add_argument("--top-k", type=int, default=5, help="返回1至20条结果。")
    search.add_argument("--json", action="store_true", help="输出完整JSON结果。")
    compare = subparsers.add_parser("compare-frameworks", help="对比原生检索与LlamaIndex适配结果。")
    compare.add_argument("--index-dir", default="06_知识证据索引", help="索引目录。")
    compare.add_argument("--query", required=True, help="检索词。")
    compare.add_argument("--product", help="可选产品过滤。")
    compare.add_argument(
        "--document-type",
        action="append",
        choices=["配方", "工艺", "设备", "GMP原文", "GMP摘要", "对标基线", "异常处理"],
    )
    compare.add_argument("--regulatory", action="store_true")
    compare.add_argument("--top-k", type=int, default=5)
    return parser


def _print_manifest(manifest) -> None:
    print(f"索引状态：{manifest.status}")
    print(f"索引版本：{manifest.index_version}")
    print(
        f"文档：{manifest.document_count}份；页面：{manifest.page_count}；"
        f"知识块：{manifest.chunk_count}"
    )
    print(f"输出目录：{manifest.output_root}")
    for source in manifest.sources:
        print(
            f"- {source.document_type} | {source.document_title} | "
            f"{source.version} | {source.page_count}页 | {source.status}"
        )


def _print_search(result) -> None:
    print(f"检索状态：{result.status}")
    print(
        f"检索模式：{result.retrieval_mode}；"
        f"BM25权重={result.bm25_weight:.2f}；向量权重={result.vector_weight:.2f}"
    )
    for warning in result.warnings:
        print(f"警告：{warning}")
    for hit in result.hits:
        print(f"[{hit.rank}] score={hit.score} {hit.citation.display}")
        print(f"    {hit.excerpt}")


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "build":
            result = build_knowledge_index(
                Path(args.data_dir),
                Path(args.output_dir),
                catalog_path=Path(args.catalog) if args.catalog else None,
                include_default_catalog=not args.catalog_only,
            )
            if args.json:
                print(
                    json.dumps(
                        result.model_dump(mode="json"), ensure_ascii=False, indent=2
                    )
                )
            else:
                _print_manifest(result)
            return 1 if result.status == "FAIL" else 0

        if args.command == "compare-frameworks":
            if not llamaindex_available():
                print("LlamaIndex适配层未安装；请安装requirements-llamaindex.txt")
                return 3
            comparison = compare_native_and_llamaindex(
                Path(args.index_dir),
                args.query,
                product=args.product,
                document_types=args.document_type,
                regulatory_claim=args.regulatory,
                top_k=args.top_k,
            )
            print(json.dumps(comparison, ensure_ascii=False, indent=2))
            return 0 if comparison["identical"] else 1

        result = search_knowledge(
            Path(args.index_dir),
            args.query,
            product=args.product,
            document_types=args.document_type,
            regulatory_claim=args.regulatory,
            top_k=args.top_k,
        )
        if args.json:
            print(
                json.dumps(
                    result.model_dump(mode="json"), ensure_ascii=False, indent=2
                )
            )
        else:
            _print_search(result)
        return 0
    except (KnowledgeIndexError, KnowledgeRetrievalError) as exc:
        print(f"知识索引操作失败：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
