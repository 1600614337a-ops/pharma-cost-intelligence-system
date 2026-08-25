"""Optional LlamaIndex adapter for the governed knowledge retriever.

The adapter deliberately wraps :func:`search_knowledge` instead of rebuilding
or reranking the index.  Source/hash checks, governance filters, and the
validated BM25/vector ranking therefore remain the single source of truth.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from typing import Any

from .models import DocumentType, KnowledgeSearchResult
from .retrieval import search_knowledge


class LlamaIndexUnavailableError(RuntimeError):
    """Raised when the optional LlamaIndex runtime is not installed."""


def llamaindex_available() -> bool:
    """Return whether the optional ``llama-index-core`` package is importable."""

    try:
        return importlib.util.find_spec("llama_index.core") is not None
    except ModuleNotFoundError:
        return False


def _node_id(source_path: str, page: int, content_hash: str) -> str:
    value = f"{source_path}|{page}|{content_hash}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def create_llamaindex_retriever(
    index_dir: str | Path,
    *,
    product: str | None = None,
    document_types: list[DocumentType] | None = None,
    regulatory_claim: bool = False,
    top_k: int = 5,
) -> Any:
    """Create a LlamaIndex ``BaseRetriever`` backed by governed retrieval.

    Imports are intentionally lazy so the core application remains runnable
    without the optional dependency.  The returned retriever does not call an
    LLM and does not perform an additional ranking pass.
    """

    if not llamaindex_available():
        raise LlamaIndexUnavailableError(
            "LlamaIndex适配层未安装；请安装requirements-llamaindex.txt，"
            "或继续使用默认原生检索"
        )

    from llama_index.core import QueryBundle
    from llama_index.core.retrievers import BaseRetriever
    from llama_index.core.schema import NodeWithScore, TextNode

    root = Path(index_dir).resolve()
    requested_types = list(document_types or [])

    class GovernedLlamaIndexRetriever(BaseRetriever):
        """LlamaIndex interface over the project's verified search result."""

        def __init__(self) -> None:
            super().__init__()
            self.last_result: KnowledgeSearchResult | None = None

        def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
            result = search_knowledge(
                root,
                query_bundle.query_str,
                product=product,
                document_types=requested_types,
                regulatory_claim=regulatory_claim,
                top_k=top_k,
            )
            self.last_result = result
            nodes: list[NodeWithScore] = []
            for hit in result.hits:
                citation = hit.citation
                metadata = {
                    "rank": hit.rank,
                    "source_path": citation.source_path,
                    "absolute_path": citation.absolute_path,
                    "document_title": citation.document_title,
                    "document_type": citation.document_type,
                    "version": citation.version,
                    "effective_date": citation.effective_date,
                    "page": citation.page,
                    "section": citation.section,
                    "content_hash": citation.content_hash,
                    "authority": citation.authority,
                    "citation_display": citation.display,
                    "source_format": citation.source_format,
                    "location_type": citation.location_type,
                    "matched_terms": hit.matched_terms,
                    "bm25_score": hit.bm25_score,
                    "vector_score": hit.vector_score,
                    "fused_score": hit.fused_score,
                    "retrieval_mode": result.retrieval_mode,
                    "index_version": result.index_version,
                }
                node = TextNode(
                    id_=_node_id(
                        citation.source_path,
                        citation.page,
                        citation.content_hash,
                    ),
                    text=hit.excerpt,
                    metadata=metadata,
                    excluded_llm_metadata_keys=["absolute_path"],
                )
                nodes.append(NodeWithScore(node=node, score=hit.score))
            return nodes

    return GovernedLlamaIndexRetriever()


def compare_native_and_llamaindex(
    index_dir: str | Path,
    query: str,
    *,
    product: str | None = None,
    document_types: list[DocumentType] | None = None,
    regulatory_claim: bool = False,
    top_k: int = 5,
) -> dict[str, Any]:
    """Run an exact-order A/B comparison for regression evidence."""

    native = search_knowledge(
        index_dir,
        query,
        product=product,
        document_types=document_types,
        regulatory_claim=regulatory_claim,
        top_k=top_k,
    )
    retriever = create_llamaindex_retriever(
        index_dir,
        product=product,
        document_types=document_types,
        regulatory_claim=regulatory_claim,
        top_k=top_k,
    )
    nodes = retriever.retrieve(query)
    native_keys = [
        [hit.citation.source_path, hit.citation.page, hit.citation.content_hash]
        for hit in native.hits
    ]
    adapter_keys = [
        [
            node.metadata["source_path"],
            node.metadata["page"],
            node.metadata["content_hash"],
        ]
        for node in nodes
    ]
    return {
        "query": query,
        "product": product,
        "top_k": top_k,
        "native": native_keys,
        "llamaindex": adapter_keys,
        "identical": native_keys == adapter_keys,
    }
