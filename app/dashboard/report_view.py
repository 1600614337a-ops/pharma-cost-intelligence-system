"""Present the governed report wording needed by the dashboard after export."""

from __future__ import annotations

from app.llm.models import NARRATIVE_FIELDS
from app.reporting.models import ReportContract


def build_report_web_content(contract: ReportContract) -> dict[str, object]:
    """Return only report-generated text that the current dashboard can display."""

    narratives = {
        name: str(contract.fields[name].value)
        for name in NARRATIVE_FIELDS
        if name in contract.fields
    }
    recommendation_table = contract.dynamic_tables.get("改进建议表格")
    recommendations: list[dict[str, str]] = []
    if recommendation_table is not None:
        for row in recommendation_table.rows:
            if len(row) < 6:
                continue
            recommendations.append(
                {
                    "sequence": str(row[0]),
                    "action": str(row[1]),
                    "owner": str(row[2]),
                    "priority": str(row[3]),
                    "expected_effect": str(row[4]),
                    "due": str(row[5]),
                }
            )
    return {"narratives": narratives, "recommendations": recommendations}
