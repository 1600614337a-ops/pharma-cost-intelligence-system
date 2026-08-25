"""Deterministic BM25 and local TF-IDF/LSI vector retrieval primitives."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np

from .models import KnowledgeChunk


BM25_VERSION = "bm25-okapi-1.0"
VECTOR_VERSION = "local-tfidf-lsi-1.0"
VECTOR_FILENAME = "vectors.npz"
BM25_K1 = 1.5
BM25_B = 0.75


def tokenize(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    parts = re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[.-][a-z0-9]+)*", normalized)
    tokens: list[str] = []
    for part in parts:
        if re.fullmatch(r"[\u3400-\u9fff]+", part):
            if len(part) <= 16:
                tokens.append(part)
            for size in (2, 3):
                tokens.extend(part[index : index + size] for index in range(len(part) - size + 1))
        else:
            tokens.append(part)
    return tokens


def chunk_text(chunk: KnowledgeChunk) -> str:
    products = " ".join(chunk.products)
    return f"{chunk.document_title} {chunk.section} {products} {chunk.text}"


def bm25_scores(chunks: list[KnowledgeChunk], query: str) -> list[float]:
    documents = [tokenize(chunk_text(chunk)) for chunk in chunks]
    query_tokens = list(dict.fromkeys(tokenize(query)))
    if not query_tokens or not documents:
        return [0.0] * len(chunks)
    lengths = [len(tokens) for tokens in documents]
    average_length = sum(lengths) / max(1, len(lengths))
    frequencies = [Counter(tokens) for tokens in documents]
    document_frequency = Counter(
        token for token in query_tokens for tokens in documents if token in set(tokens)
    )
    scores: list[float] = []
    total_documents = len(documents)
    for length, counts in zip(lengths, frequencies):
        score = 0.0
        for token in query_tokens:
            frequency = counts.get(token, 0)
            if not frequency:
                continue
            df = document_frequency[token]
            inverse_frequency = math.log(1 + (total_documents - df + 0.5) / (df + 0.5))
            denominator = frequency + BM25_K1 * (
                1 - BM25_B + BM25_B * length / max(1.0, average_length)
            )
            score += inverse_frequency * frequency * (BM25_K1 + 1) / denominator
        scores.append(score)
    return scores


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms != 0)


def build_vector_index(
    chunks: list[KnowledgeChunk],
    output_path: Path,
    *,
    maximum_dimensions: int = 96,
    maximum_features: int = 8000,
) -> int:
    tokenized = [tokenize(chunk_text(chunk)) for chunk in chunks]
    document_frequency = Counter(token for tokens in tokenized for token in set(tokens))
    vocabulary = sorted(document_frequency, key=lambda token: (-document_frequency[token], token))[:maximum_features]
    if not chunks or not vocabulary:
        raise ValueError("知识块不足，无法建立本地语义向量")
    positions = {token: index for index, token in enumerate(vocabulary)}
    matrix = np.zeros((len(chunks), len(vocabulary)), dtype=np.float32)
    for row, tokens in enumerate(tokenized):
        for token, count in Counter(tokens).items():
            column = positions.get(token)
            if column is not None:
                matrix[row, column] = 1.0 + math.log(count)
    idf = np.array(
        [math.log((1 + len(chunks)) / (1 + document_frequency[token])) + 1 for token in vocabulary],
        dtype=np.float32,
    )
    matrix *= idf
    matrix = _normalize_rows(matrix)
    dimensions = min(maximum_dimensions, len(chunks), len(vocabulary))
    _, _, right = np.linalg.svd(matrix, full_matrices=False)
    components = right[:dimensions].astype(np.float32)
    vectors = _normalize_rows((matrix @ components.T).astype(np.float32))
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        np.savez_compressed(
            stream,
            version=np.array([VECTOR_VERSION]),
            vocabulary=np.asarray(vocabulary),
            idf=idf,
            components=components,
            vectors=vectors,
            chunk_ids=np.asarray([chunk.chunk_id for chunk in chunks]),
        )
    temporary.replace(output_path)
    return dimensions


def vector_scores(vector_path: Path, chunks: list[KnowledgeChunk], query: str) -> list[float]:
    try:
        with np.load(vector_path, allow_pickle=False) as stored:
            version = str(stored["version"][0])
            vocabulary = stored["vocabulary"].astype(str).tolist()
            idf = stored["idf"].astype(np.float32)
            components = stored["components"].astype(np.float32)
            vectors = stored["vectors"].astype(np.float32)
            chunk_ids = stored["chunk_ids"].astype(str).tolist()
    except Exception as exc:
        raise ValueError(f"本地语义向量文件无法读取：{exc}") from exc
    if version != VECTOR_VERSION:
        raise ValueError(f"不支持的向量版本：{version}")
    if chunk_ids != [chunk.chunk_id for chunk in chunks]:
        raise ValueError("语义向量与知识块顺序不一致")
    positions = {token: index for index, token in enumerate(vocabulary)}
    query_vector = np.zeros(len(vocabulary), dtype=np.float32)
    for token, count in Counter(tokenize(query)).items():
        column = positions.get(token)
        if column is not None:
            query_vector[column] = (1.0 + math.log(count)) * idf[column]
    norm = float(np.linalg.norm(query_vector))
    if norm == 0:
        return [0.0] * len(chunks)
    query_vector /= norm
    latent = query_vector @ components.T
    latent_norm = float(np.linalg.norm(latent))
    if latent_norm == 0:
        return [0.0] * len(chunks)
    latent /= latent_norm
    similarities = vectors @ latent
    return [max(0.0, float(value)) for value in similarities]
