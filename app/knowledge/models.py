"""Typed models for the page-level pharmaceutical knowledge index."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


DocumentType = Literal[
    "配方",
    "工艺",
    "设备",
    "GMP原文",
    "GMP摘要",
    "对标基线",
    "异常处理",
]
SourceFormat = Literal["pdf", "docx", "txt", "md"]
LocationType = Literal["page", "section"]


class KnowledgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeChunk(KnowledgeModel):
    index_version: str
    chunk_id: str
    source_path: str
    source_sha256: str
    source_size: int
    document_title: str
    document_type: DocumentType
    products: list[str]
    version: str
    effective_date: str
    page: int = Field(ge=1)
    page_count: int = Field(ge=1)
    section: str
    confidentiality: str
    source_priority: int = Field(ge=1)
    content_hash: str
    text: str
    source_format: SourceFormat = "pdf"
    location_type: LocationType = "page"


class KnowledgeIssue(KnowledgeModel):
    severity: Literal["ERROR", "WARNING"]
    code: str
    message: str
    source_path: str | None = None
    page: int | None = None


class KnowledgeDocumentSummary(KnowledgeModel):
    source_path: str
    source_sha256: str
    source_size: int
    document_title: str
    document_type: DocumentType
    version: str
    effective_date: str
    page_count: int
    chunk_count: int
    status: Literal["PASS", "FAIL"]
    source_format: SourceFormat = "pdf"


class KnowledgeIndexManifest(KnowledgeModel):
    index_version: str
    extractor: str
    generated_at: str
    source_root: str
    output_root: str
    index_file: str
    index_file_sha256: str
    status: Literal["PASS", "PASS_WITH_WARNING", "FAIL"]
    document_count: int
    page_count: int
    chunk_count: int
    sources: list[KnowledgeDocumentSummary]
    issues: list[KnowledgeIssue]
    bm25_version: str = "legacy"
    vector_model: str | None = None
    vector_file: str | None = None
    vector_file_sha256: str | None = None
    vector_dimensions: int | None = None
    catalog_file: str | None = None
    catalog_file_sha256: str | None = None


class KnowledgeCitation(KnowledgeModel):
    source_path: str
    absolute_path: str
    document_title: str
    document_type: DocumentType
    version: str
    effective_date: str
    page: int
    section: str
    content_hash: str
    authority: Literal["primary", "supporting_only"]
    display: str
    source_format: SourceFormat = "pdf"
    location_type: LocationType = "page"


class KnowledgeSearchHit(KnowledgeModel):
    rank: int
    score: float
    matched_terms: list[str]
    excerpt: str
    citation: KnowledgeCitation
    bm25_score: float = 0.0
    vector_score: float = 0.0
    fused_score: float = 0.0


class KnowledgeSearchResult(KnowledgeModel):
    query: str
    product: str | None
    document_types: list[DocumentType]
    regulatory_claim: bool
    index_version: str
    index_generated_at: str
    status: Literal["PASS", "PASS_WITH_WARNING", "NO_RESULTS"]
    warnings: list[str]
    hits: list[KnowledgeSearchHit]
    retrieval_mode: Literal["lexical", "hybrid"] = "lexical"
    bm25_weight: float = 1.0
    vector_weight: float = 0.0
