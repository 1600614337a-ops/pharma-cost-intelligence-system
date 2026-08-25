"""Source-verified hybrid retrieval over the governed knowledge index."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable

from .hybrid import bm25_scores, vector_scores
from .indexer import MANIFEST_FILENAME, RADICAL_TRANSLATION
from .models import (
    DocumentType,
    KnowledgeChunk,
    KnowledgeCitation,
    KnowledgeIndexManifest,
    KnowledgeSearchHit,
    KnowledgeSearchResult,
)


# Exact product, process parameter, amount, and article references must outrank
# broader semantic similarity in this evidence-sensitive domain.
BM25_WEIGHT = 0.70
VECTOR_WEIGHT = 0.30
MIN_HYBRID_SCORE = 1e-8


class KnowledgeRetrievalError(RuntimeError):
    """Raised when an index is missing, invalid, or stale."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _compact(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).translate(RADICAL_TRANSLATION).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _query_terms(query: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", query).strip()
    if not normalized:
        raise KnowledgeRetrievalError("检索词不能为空")
    raw_terms = re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*", normalized)
    terms: list[str] = []
    for term in raw_terms:
        compact = _compact(term)
        if len(compact) >= 2 and compact not in terms:
            terms.append(compact)
    if not terms:
        raise KnowledgeRetrievalError("检索词没有可用关键词")
    return terms


def _load_verified_index(index_dir: str | Path) -> tuple[KnowledgeIndexManifest, list[KnowledgeChunk]]:
    root = Path(index_dir).resolve()
    manifest_path = root / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise KnowledgeRetrievalError("索引清单不存在，请先执行build")
    manifest = KnowledgeIndexManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    if manifest.status == "FAIL":
        raise KnowledgeRetrievalError("知识索引状态为FAIL，禁止检索")
    index_path = root / manifest.index_file
    if not index_path.is_file():
        raise KnowledgeRetrievalError("知识块索引文件不存在，请重新构建")
    if _sha256_file(index_path) != manifest.index_file_sha256:
        raise KnowledgeRetrievalError("知识块索引哈希与清单不一致，请重新构建")
    if manifest.vector_file:
        vector_path = root / manifest.vector_file
        if not vector_path.is_file() or _sha256_file(vector_path) != manifest.vector_file_sha256:
            raise KnowledgeRetrievalError("语义向量文件缺失或哈希不一致，请重新构建")
    if manifest.catalog_file:
        catalog_path = Path(manifest.catalog_file)
        if not catalog_path.is_file() or _sha256_file(catalog_path) != manifest.catalog_file_sha256:
            raise KnowledgeRetrievalError("知识文档目录清单已变化或缺失，请重新构建")

    source_root = Path(manifest.source_root)
    for source in manifest.sources:
        path = source_root / Path(source.source_path)
        if not path.is_file() or _sha256_file(path) != source.source_sha256:
            raise KnowledgeRetrievalError(f"原始知识文档已变化或缺失，请重新构建索引：{source.source_path}")

    chunks: list[KnowledgeChunk] = []
    with index_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                chunks.append(KnowledgeChunk.model_validate_json(line))
            except Exception as exc:
                raise KnowledgeRetrievalError(f"知识块索引第{line_number}行无效：{exc}") from exc
    if len(chunks) != manifest.chunk_count:
        raise KnowledgeRetrievalError("知识块索引记录数与清单不一致，请重新构建")
    return manifest, chunks


def _legacy_score(chunk: KnowledgeChunk, terms: Iterable[str], product: str | None) -> tuple[int, list[str]]:
    text = _compact(chunk.text)
    title = _compact(chunk.document_title)
    section = _compact(chunk.section)
    matched: list[str] = []
    score = max(0, 12 - chunk.source_priority)
    product_term = _compact(product) if product else None
    for term in terms:
        metadata_product_match = bool(product_term and term == product_term and product in chunk.products)
        if term not in text and term not in title and term not in section and not metadata_product_match:
            continue
        matched.append(term)
        score += min(len(term), 16) * 10
        score += min(text.count(term), 3) * 3
        if term in title:
            score += 30
        if term in section:
            score += 15
    if product and product in chunk.products:
        score += 40
    return score, matched


def _excerpt(text: str, terms: list[str], limit: int = 300) -> str:
    display = " ".join(text.split())
    compact_display = _compact(display)
    position = -1
    for term in sorted(terms, key=len, reverse=True):
        position = compact_display.find(term)
        if position >= 0:
            break
    if position < 0 or len(display) <= limit:
        return display[:limit]
    start = max(0, int(position / max(1, len(compact_display)) * len(display)) - 80)
    end = min(len(display), start + limit)
    return ("…" if start else "") + display[start:end] + ("…" if end < len(display) else "")


def _eligible_indices(
    chunks: list[KnowledgeChunk],
    requested_types: list[DocumentType],
    product: str | None,
) -> list[int]:
    return [
        index
        for index, chunk in enumerate(chunks)
        if (not requested_types or chunk.document_type in requested_types)
        and (not product or not chunk.products or product in chunk.products)
    ]


def _hybrid_rank(
    chunks: list[KnowledgeChunk],
    indices: list[int],
    terms: list[str],
    bm25: list[float],
    vectors: list[float],
    product: str | None,
) -> list[tuple[float, int, list[str], float, float]]:
    maximum_bm25 = max((bm25[index] for index in indices), default=0.0)
    ranked: list[tuple[float, int, list[str], float, float]] = []
    for index in indices:
        chunk = chunks[index]
        text_fields = _compact(f"{chunk.document_title} {chunk.section} {chunk.text}")
        matched = [term for term in terms if term in text_fields or (product and term == _compact(product) and product in chunk.products)]
        normalized_bm25 = bm25[index] / maximum_bm25 if maximum_bm25 else 0.0
        vector_score = vectors[index]
        fused = BM25_WEIGHT * normalized_bm25 + VECTOR_WEIGHT * vector_score
        if fused <= MIN_HYBRID_SCORE:
            continue
        ranked.append((fused, index, matched, bm25[index], vector_score))
    ranked.sort(
        key=lambda item: (
            -(len(item[2]) / max(1, len(terms))),
            -item[0],
            chunks[item[1]].source_priority,
            chunks[item[1]].source_path,
            chunks[item[1]].page,
        )
    )
    return ranked


def _citation(chunk: KnowledgeChunk, source_root: Path) -> KnowledgeCitation:
    authority = (
        "supporting_only"
        if chunk.document_type in {"GMP摘要", "对标基线", "异常处理"}
        else "primary"
    )
    location = f"第{chunk.page}页" if chunk.location_type == "page" else f"第{chunk.page}节"
    return KnowledgeCitation(
        source_path=chunk.source_path, absolute_path=str(source_root / Path(chunk.source_path)), document_title=chunk.document_title,
        document_type=chunk.document_type, version=chunk.version, effective_date=chunk.effective_date, page=chunk.page,
        section=chunk.section, content_hash=chunk.content_hash, authority=authority,
        display=f"《{chunk.document_title}》{chunk.version}，{location}，{chunk.section}",
        source_format=chunk.source_format, location_type=chunk.location_type,
    )


def search_knowledge(
    index_dir: str | Path,
    query: str,
    *,
    product: str | None = None,
    document_types: list[DocumentType] | None = None,
    regulatory_claim: bool = False,
    top_k: int = 5,
) -> KnowledgeSearchResult:
    """Return hybrid-ranked citations after checking all index and source hashes."""

    if not 1 <= top_k <= 20:
        raise KnowledgeRetrievalError("top_k必须位于1至20")
    manifest, chunks = _load_verified_index(index_dir)
    terms = _query_terms(query)
    requested_types = list(document_types or [])
    eligible = _eligible_indices(chunks, requested_types, product)
    warnings: list[str] = []
    status = "PASS"
    source_root = Path(manifest.source_root)

    if not manifest.vector_file:
        candidates: list[tuple[float, int, list[str], float, float]] = []
        summaries: list[tuple[float, int, list[str], float, float]] = []
        for index in eligible:
            chunk = chunks[index]
            score, matched = _legacy_score(chunk, terms, product)
            if not matched:
                continue
            candidate = (float(score), index, matched, float(score), 0.0)
            if regulatory_claim and chunk.document_type == "GMP摘要":
                summaries.append(candidate)
            elif not regulatory_claim or chunk.document_type == "GMP原文":
                candidates.append(candidate)
        if regulatory_claim and not candidates and summaries:
            candidates = summaries
            warnings.append("W07：仅命中GMP摘要，法规结论必须降级且不得输出精确条款断言")
            status = "PASS_WITH_WARNING"
        candidates.sort(key=lambda item: (-item[0], chunks[item[1]].source_priority, chunks[item[1]].source_path, chunks[item[1]].page))
        ranked = candidates
        retrieval_mode = "lexical"
        bm25_weight, vector_weight = 1.0, 0.0
    else:
        bm25 = bm25_scores(chunks, query)
        try:
            vectors = vector_scores(Path(index_dir).resolve() / manifest.vector_file, chunks, query)
        except ValueError as exc:
            raise KnowledgeRetrievalError(str(exc)) from exc
        if regulatory_claim:
            primary = [index for index in eligible if chunks[index].document_type == "GMP原文"]
            summaries = [index for index in eligible if chunks[index].document_type == "GMP摘要"]
            ranked = _hybrid_rank(chunks, primary, terms, bm25, vectors, product)
            if not ranked and summaries:
                ranked = _hybrid_rank(chunks, summaries, terms, bm25, vectors, product)
                if ranked:
                    warnings.append("W07：仅命中GMP摘要，法规结论必须降级且不得输出精确条款断言")
                    status = "PASS_WITH_WARNING"
        else:
            ranked = _hybrid_rank(chunks, eligible, terms, bm25, vectors, product)
        retrieval_mode = "hybrid"
        bm25_weight, vector_weight = BM25_WEIGHT, VECTOR_WEIGHT

    hits: list[KnowledgeSearchHit] = []
    for rank, (fused, index, matched, bm25_score, vector_score) in enumerate(ranked[:top_k], start=1):
        chunk = chunks[index]
        hits.append(
            KnowledgeSearchHit(
                rank=rank, score=round(fused * (1000 if retrieval_mode == "hybrid" else 1), 6), matched_terms=matched,
                excerpt=_excerpt(chunk.text, [term for term in matched if term != _compact(product or "")] or matched),
                citation=_citation(chunk, source_root), bm25_score=round(bm25_score, 6), vector_score=round(vector_score, 6),
                fused_score=round(fused, 6),
            )
        )
    if not hits:
        status = "NO_RESULTS"
    return KnowledgeSearchResult(
        query=query, product=product, document_types=requested_types, regulatory_claim=regulatory_claim,
        index_version=manifest.index_version, index_generated_at=manifest.generated_at, status=status, warnings=warnings, hits=hits,
        retrieval_mode=retrieval_mode, bm25_weight=bm25_weight, vector_weight=vector_weight,
    )
