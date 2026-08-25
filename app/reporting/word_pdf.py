"""Isolated Microsoft Word/WPS PDF exporter for Windows."""

from __future__ import annotations

import sys
from pathlib import Path


def export_pdf(source: Path, output: Path) -> str:
    import pythoncom
    from win32com.client import DispatchEx

    errors: list[str] = []
    pythoncom.CoInitialize()
    try:
        for program_id in ("Word.Application", "KWPS.Application"):
            application = None
            document = None
            try:
                application = DispatchEx(program_id)
                application.Visible = False
                application.DisplayAlerts = 0
                document = application.Documents.Open(
                    str(source),
                    ConfirmConversions=False,
                    ReadOnly=True,
                    AddToRecentFiles=False,
                    Visible=False,
                )
                document.ExportAsFixedFormat(
                    OutputFileName=str(output),
                    ExportFormat=17,
                    OpenAfterExport=False,
                )
                if output.is_file() and output.stat().st_size >= 1024:
                    return program_id
                errors.append(f"{program_id}: 未生成有效PDF")
            except Exception as exc:  # COM error details are returned to the parent process.
                errors.append(f"{program_id}: {exc}")
            finally:
                if document is not None:
                    try:
                        document.Close(False)
                    except Exception:
                        pass
                if application is not None:
                    try:
                        application.Quit()
                    except Exception:
                        pass
        raise RuntimeError("；".join(errors))
    finally:
        pythoncom.CoUninitialize()


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: word_pdf.py SOURCE.docx OUTPUT.pdf")
    source = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    if not source.is_file() or source.suffix.lower() != ".docx":
        raise SystemExit("转换源必须是DOCX文件")
    output.parent.mkdir(parents=True, exist_ok=True)
    renderer = export_pdf(source, output)
    print(renderer)


if __name__ == "__main__":
    main()
