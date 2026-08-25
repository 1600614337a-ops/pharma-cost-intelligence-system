"""Contract tests for the optional LlamaIndex compatibility layer."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.knowledge import (
    compare_native_and_llamaindex,
    create_llamaindex_retriever,
    llamaindex_available,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_ROOT = PROJECT_ROOT / "06_知识证据索引"


@unittest.skipUnless(llamaindex_available(), "optional llama-index-core is not installed")
class LlamaIndexAdapterTests(unittest.TestCase):
    def test_adapter_preserves_governed_ranking_and_citations(self) -> None:
        comparison = compare_native_and_llamaindex(
            INDEX_ROOT,
            "银黄口服液 提取收率 0.08元/盒",
            product="银黄口服液",
            document_types=["工艺"],
            top_k=3,
        )
        self.assertTrue(comparison["identical"])

        retriever = create_llamaindex_retriever(
            INDEX_ROOT,
            product="六味地黄胶囊",
            document_types=["设备"],
            top_k=1,
        )
        nodes = retriever.retrieve("胶囊填充机 NJP-3200 维修费 8500")
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].metadata["page"], 4)
        self.assertEqual(nodes[0].metadata["authority"], "primary")
        self.assertEqual(len(nodes[0].metadata["content_hash"]), 64)

    def test_golden_queries_are_exactly_identical(self) -> None:
        cases = [
            ("板蓝根颗粒 提取收率 0.03元/盒", "板蓝根颗粒", ["工艺"], False),
            ("生产全过程 记录 偏差 调查 批记录 追溯", None, None, True),
            ("银黄口服液 金银花 黄芩提取物 处方", "银黄口服液", ["配方"], False),
        ]
        for query, product, document_types, regulatory in cases:
            with self.subTest(query=query):
                result = compare_native_and_llamaindex(
                    INDEX_ROOT,
                    query,
                    product=product,
                    document_types=document_types,
                    regulatory_claim=regulatory,
                    top_k=3,
                )
                self.assertTrue(result["identical"])


if __name__ == "__main__":
    unittest.main()
