"""Public reporting contract and renderer APIs."""

from .contract import build_report_contract, template_placeholders
from .models import ReportContract
from .renderers import (
    ReportRenderError,
    render_contract_json,
    render_docx,
    render_markdown,
    render_pdf,
    scan_docx_placeholders,
    scan_unresolved_placeholders,
)

__all__ = ["ReportContract", "ReportRenderError", "build_report_contract", "render_contract_json", "render_docx", "render_markdown", "render_pdf", "scan_docx_placeholders", "scan_unresolved_placeholders", "template_placeholders"]
