"""Build a governed multi-format index with local semantic vectors."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from docx import __version__ as docx_version
from pypdf import __version__ as pypdf_version

from .hybrid import BM25_VERSION, VECTOR_FILENAME, VECTOR_VERSION, build_vector_index
from .models import (
    DocumentType,
    KnowledgeChunk,
    KnowledgeDocumentSummary,
    KnowledgeIndexManifest,
    KnowledgeIssue,
)
from .parsers import DocumentParseError, SUPPORTED_SUFFIXES, extract_document


INDEX_VERSION = "2.1.0"
SOURCE_FOLDER = Path("03_制药知识文档")
INDEX_FILENAME = "pages.jsonl"
MANIFEST_FILENAME = "manifest.json"
DOCUMENT_TYPES = {
    "配方",
    "工艺",
    "设备",
    "GMP原文",
    "GMP摘要",
    "对标基线",
    "异常处理",
}


@dataclass(frozen=True)
class DocumentSpec:
    filename: str
    title: str
    document_type: DocumentType
    version: str
    effective_date: str
    confidentiality: str
    source_priority: int
    default_products: tuple[str, ...] = ()
    page_products: dict[int, tuple[str, ...]] | None = None

    def products_for_page(self, page: int) -> list[str]:
        if self.page_products and page in self.page_products:
            return list(self.page_products[page])
        return list(self.default_products)


ALL_PRODUCTS = ("银黄口服液", "板蓝根颗粒", "六味地黄胶囊")

DOCUMENT_CATALOG = (
    DocumentSpec("产品配方文档_银黄口服液.pdf", "银黄口服液 产品配方文档", "配方", "V2.0", "2025-01-01", "内部", 3, ("银黄口服液",)),
    DocumentSpec("产品配方文档_板蓝根颗粒.pdf", "板蓝根颗粒 产品配方文档", "配方", "V1.5", "2025-03-01", "内部", 3, ("板蓝根颗粒",)),
    DocumentSpec("产品配方文档_六味地黄胶囊.pdf", "六味地黄胶囊 产品配方文档", "配方", "V2.2", "2025-06-01", "内部", 3, ("六味地黄胶囊",)),
    DocumentSpec(
        "生产工艺文档_中药一厂.pdf", "中药一厂 生产工艺路线文档", "工艺", "V3.0", "2025-06-01", "内部", 3,
        page_products={1: ("银黄口服液",), 2: ("银黄口服液", "板蓝根颗粒"), 3: ("板蓝根颗粒",), 4: ("板蓝根颗粒", "六味地黄胶囊"), 5: ("六味地黄胶囊",)},
    ),
    DocumentSpec(
        "车间设备清单_中药一厂.pdf", "中药一厂 车间设备清单", "设备", "V2.1", "2026-03-15", "内部", 3,
        page_products={1: ("银黄口服液",), 2: ("银黄口服液", "板蓝根颗粒"), 3: ("六味地黄胶囊",), 4: ALL_PRODUCTS},
    ),
    DocumentSpec("药品生产质量管理规范GMP.pdf", "药品生产质量管理规范（2010年修订）", "GMP原文", "2010年修订", "2011-03-01", "公开", 4),
    DocumentSpec("GMP法规核心摘要_2010修订版.pdf", "药品生产质量管理规范（GMP）核心摘要", "GMP摘要", "2010年修订版核心摘要", "2011-03-01", "公开学习参考", 5),
    DocumentSpec(
        "派生知识/同集团工厂对标基线_中药二厂_2025-2026H1.md",
        "同集团工厂对标基线：中药二厂（2025-2026年上半年）",
        "对标基线",
        "V1.0",
        "2026-06-30",
        "内部派生",
        4,
        ALL_PRODUCTS,
    ),
    DocumentSpec(
        "派生知识/历史成本异常处理记录_中药一厂.md",
        "历史成本异常处理记录：中药一厂（事件与规则分层版）",
        "异常处理",
        "V1.0",
        "2026-08-22",
        "内部派生",
        4,
        ALL_PRODUCTS,
    ),
)


HEADING_PATTERNS = (
    re.compile(r"^第[一二三四五六七八九十百0-9]+章\s*.+$"),
    re.compile(r"^第[一二三四五六七八九十百0-9]+节\s*.+$"),
    re.compile(r"^[一二三四五六七八九十]+、\s*.+$"),
    re.compile(r"^\d+\.\d+\s+.+$"),
)

RADICAL_TRANSLATION = str.maketrans(
    {"⺠": "民", "⻄": "西", "⻅": "见", "⻆": "角", "⻋": "车", "⻓": "长", "⻛": "风", "⻜": "飞", "⻣": "骨", "⻩": "黄", "⻰": "龙"}
)


class KnowledgeIndexError(RuntimeError):
    """Raised when the governed index cannot be built safely."""


def normalize_pdf_text(text: str) -> str:
    """Normalize compatibility glyphs and whitespace without changing meaning."""

    normalized = unicodedata.normalize("NFKC", text).translate(RADICAL_TRANSLATION).replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.splitlines()]
    return "\n".join(line for line in lines if line)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _section(text: str, fallback: str) -> str:
    headings: list[str] = []
    for line in text.splitlines():
        if any(pattern.match(line) for pattern in HEADING_PATTERNS) and line not in headings:
            headings.append(line)
        if len(headings) == 4:
            break
    return " / ".join(headings) if headings else fallback


def _validate_output_location(source_root: Path, output_root: Path) -> None:
    governed_source = (source_root / SOURCE_FOLDER).resolve()
    if output_root == governed_source or output_root.is_relative_to(governed_source):
        raise KnowledgeIndexError("索引输出目录不得位于原始知识文档目录内")


def _catalog_specs(path: Path) -> list[DocumentSpec]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise KnowledgeIndexError(f"知识文档目录清单无法读取：{exc}") from exc
    documents = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(documents, list):
        raise KnowledgeIndexError("知识文档目录清单必须包含documents数组")
    specs: list[DocumentSpec] = []
    required = {"filename", "title", "document_type", "version", "effective_date", "confidentiality", "source_priority"}
    for number, item in enumerate(documents, start=1):
        if not isinstance(item, dict) or required - set(item):
            raise KnowledgeIndexError(f"目录清单第{number}项缺少必填元数据：{sorted(required - set(item or {}))}")
        if item["document_type"] not in DOCUMENT_TYPES:
            raise KnowledgeIndexError(f"目录清单第{number}项文档类型无效：{item['document_type']}")
        suffix = Path(str(item["filename"])).suffix.casefold()
        if suffix not in SUPPORTED_SUFFIXES:
            raise KnowledgeIndexError(f"目录清单第{number}项格式不支持：{suffix or '无扩展名'}")
        page_products = item.get("page_products")
        converted_pages = None
        if page_products is not None:
            if not isinstance(page_products, dict):
                raise KnowledgeIndexError(f"目录清单第{number}项page_products必须是对象")
            converted_pages = {int(key): tuple(value) for key, value in page_products.items()}
        priority = int(item["source_priority"])
        if priority < 1:
            raise KnowledgeIndexError(f"目录清单第{number}项source_priority必须大于等于1")
        specs.append(
            DocumentSpec(
                filename=str(item["filename"]), title=str(item["title"]), document_type=item["document_type"],
                version=str(item["version"]), effective_date=str(item["effective_date"]), confidentiality=str(item["confidentiality"]),
                source_priority=priority, default_products=tuple(item.get("default_products", [])), page_products=converted_pages,
            )
        )
    return specs


def _resolve_specs(source_root: Path, catalog_path: str | Path | None, include_default_catalog: bool) -> tuple[list[DocumentSpec], Path | None]:
    specs = list(DOCUMENT_CATALOG) if include_default_catalog else []
    resolved_catalog: Path | None = None
    if catalog_path:
        resolved_catalog = Path(catalog_path)
        if not resolved_catalog.is_absolute():
            resolved_catalog = (source_root / resolved_catalog).resolve()
        else:
            resolved_catalog = resolved_catalog.resolve()
        if not resolved_catalog.is_file():
            raise KnowledgeIndexError("知识文档目录清单不存在")
        specs.extend(_catalog_specs(resolved_catalog))
    if not specs:
        raise KnowledgeIndexError("知识文档目录清单为空")
    filenames = [spec.filename.casefold() for spec in specs]
    duplicates = sorted({name for name in filenames if filenames.count(name) > 1})
    if duplicates:
        raise KnowledgeIndexError(f"知识文档目录存在重复文件：{duplicates}")
    governed_root = (source_root / SOURCE_FOLDER).resolve()
    for spec in specs:
        candidate = (governed_root / spec.filename).resolve()
        if not candidate.is_relative_to(governed_root):
            raise KnowledgeIndexError(f"知识文档路径越界：{spec.filename}")
    return specs, resolved_catalog


def build_knowledge_index(
    data_dir: str | Path,
    output_dir: str | Path,
    *,
    catalog_path: str | Path | None = None,
    include_default_catalog: bool = True,
) -> KnowledgeIndexManifest:
    """Extract governed documents and atomically publish text plus vector indexes."""

    source_root = Path(data_dir).resolve()
    output_root = Path(output_dir)
    output_root = (source_root / output_root).resolve() if not output_root.is_absolute() else output_root.resolve()
    _validate_output_location(source_root, output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    specs, resolved_catalog = _resolve_specs(source_root, catalog_path, include_default_catalog)

    chunks: list[KnowledgeChunk] = []
    summaries: list[KnowledgeDocumentSummary] = []
    issues: list[KnowledgeIssue] = []
    governed_root = source_root / SOURCE_FOLDER

    for spec in specs:
        relative_path = SOURCE_FOLDER / spec.filename
        path = governed_root / spec.filename
        source_format = SUPPORTED_SUFFIXES.get(path.suffix.casefold(), "pdf")
        if not path.is_file():
            issues.append(KnowledgeIssue(severity="ERROR", code="K01", message="必需知识文档不存在", source_path=relative_path.as_posix()))
            summaries.append(KnowledgeDocumentSummary(source_path=relative_path.as_posix(), source_sha256="", source_size=0, document_title=spec.title, document_type=spec.document_type, version=spec.version, effective_date=spec.effective_date, page_count=0, chunk_count=0, status="FAIL", source_format=source_format))
            continue

        raw = path.read_bytes()
        source_hash = _sha256_bytes(raw)
        try:
            parsed_format, units = extract_document(path, normalize_pdf_text, spec.title)
        except DocumentParseError as exc:
            issues.append(KnowledgeIssue(severity="ERROR", code="K02", message=str(exc), source_path=relative_path.as_posix()))
            summaries.append(KnowledgeDocumentSummary(source_path=relative_path.as_posix(), source_sha256=source_hash, source_size=len(raw), document_title=spec.title, document_type=spec.document_type, version=spec.version, effective_date=spec.effective_date, page_count=0, chunk_count=0, status="FAIL", source_format=source_format))
            continue

        document_chunks = 0
        for unit in units:
            text = unit.text
            if not text:
                issues.append(KnowledgeIssue(severity="ERROR", code="K04", message=f"{unit.location_type}没有可检索文本", source_path=relative_path.as_posix(), page=unit.number))
                continue
            content_hash = _sha256_bytes(text.encode("utf-8"))
            chunk_id = _sha256_bytes(f"{source_hash}:{unit.number}:{content_hash}".encode("utf-8"))
            chunks.append(
                KnowledgeChunk(
                    index_version=INDEX_VERSION, chunk_id=chunk_id, source_path=relative_path.as_posix(), source_sha256=source_hash,
                    source_size=len(raw), document_title=spec.title, document_type=spec.document_type, products=spec.products_for_page(unit.number),
                    version=spec.version, effective_date=spec.effective_date, page=unit.number, page_count=len(units),
                    section=unit.section or _section(text, spec.title), confidentiality=spec.confidentiality, source_priority=spec.source_priority,
                    content_hash=content_hash, text=text, source_format=parsed_format, location_type=unit.location_type,
                )
            )
            document_chunks += 1
        summaries.append(
            KnowledgeDocumentSummary(
                source_path=relative_path.as_posix(), source_sha256=source_hash, source_size=len(raw), document_title=spec.title,
                document_type=spec.document_type, version=spec.version, effective_date=spec.effective_date, page_count=len(units),
                chunk_count=document_chunks, status="PASS" if document_chunks == len(units) else "FAIL", source_format=parsed_format,
            )
        )

    chunks.sort(key=lambda item: (item.source_priority, item.source_path, item.page))
    index_path = output_root / INDEX_FILENAME
    temporary_index = output_root / f"{INDEX_FILENAME}.tmp"
    with temporary_index.open("w", encoding="utf-8", newline="\n") as stream:
        for chunk in chunks:
            stream.write(json.dumps(chunk.model_dump(mode="json"), ensure_ascii=False) + "\n")
    temporary_index.replace(index_path)

    vector_path = output_root / VECTOR_FILENAME
    vector_dimensions = build_vector_index(chunks, vector_path) if chunks else None
    has_error = any(issue.severity == "ERROR" for issue in issues)
    has_warning = any(issue.severity == "WARNING" for issue in issues)
    status = "FAIL" if has_error else "PASS_WITH_WARNING" if has_warning else "PASS"
    manifest = KnowledgeIndexManifest(
        index_version=INDEX_VERSION,
        extractor=f"pypdf {pypdf_version}; python-docx {docx_version}; UTF-8 text; {VECTOR_VERSION}",
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"), source_root=str(source_root), output_root=str(output_root),
        index_file=INDEX_FILENAME, index_file_sha256=_sha256_file(index_path), status=status, document_count=len(summaries),
        page_count=sum(item.page_count for item in summaries), chunk_count=len(chunks), sources=summaries, issues=issues,
        bm25_version=BM25_VERSION, vector_model=VECTOR_VERSION, vector_file=VECTOR_FILENAME if chunks else None,
        vector_file_sha256=_sha256_file(vector_path) if chunks else None, vector_dimensions=vector_dimensions,
        catalog_file=str(resolved_catalog) if resolved_catalog else None,
        catalog_file_sha256=_sha256_file(resolved_catalog) if resolved_catalog else None,
    )
    manifest_path = output_root / MANIFEST_FILENAME
    temporary_manifest = output_root / f"{MANIFEST_FILENAME}.tmp"
    temporary_manifest.write_text(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    temporary_manifest.replace(manifest_path)
    return manifest
