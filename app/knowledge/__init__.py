"""Public interfaces for governed knowledge indexing and retrieval."""

from .indexer import KnowledgeIndexError, build_knowledge_index
from .llamaindex_adapter import (
    LlamaIndexUnavailableError,
    compare_native_and_llamaindex,
    create_llamaindex_retriever,
    llamaindex_available,
)
from .models import KnowledgeIndexManifest, KnowledgeSearchResult
from .retrieval import KnowledgeRetrievalError, search_knowledge

__all__ = [
    "KnowledgeIndexError",
    "KnowledgeIndexManifest",
    "KnowledgeRetrievalError",
    "KnowledgeSearchResult",
    "LlamaIndexUnavailableError",
    "build_knowledge_index",
    "compare_native_and_llamaindex",
    "create_llamaindex_retriever",
    "llamaindex_available",
    "search_knowledge",
]
