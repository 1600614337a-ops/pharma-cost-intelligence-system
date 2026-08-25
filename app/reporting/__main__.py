"""CLI for building a validated report contract and materializing outputs."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.llm import LlmSettings, enhance_report_contract

from . import build_report_contract, render_contract_json, render_docx, render_markdown, render_pdf


def main() -> int:
    parser=argparse.ArgumentParser(description="生成受证据约束的月度成本分析报告。")
    parser.add_argument("--data-dir",default=".")
    parser.add_argument("--index-dir",default="06_知识证据索引")
    parser.add_argument("--product",required=True)
    parser.add_argument("--month",required=True)
    parser.add_argument("--generated-date")
    parser.add_argument("--output-dir",default="07_报告输出")
    parser.add_argument("--llm",action="store_true",help="启用OpenAI兼容大模型受控改写；失败时自动使用确定性文本")
    args=parser.parse_args()
    contract=build_report_contract(args.data_dir,args.index_dir,args.product,args.month,generated_date=args.generated_date)
    if args.llm:
        contract=enhance_report_contract(contract,LlmSettings.from_env(force_enabled=True))
    if contract.validation_status!="PASS":
        print(f"报告契约失败：{contract.validation_issues}"); return 2
    output=Path(args.output_dir); stem=f"{args.month}_{args.product}_月度成本分析报告"
    json_path=render_contract_json(contract,output/f"{stem}.json")
    md_path=render_markdown(contract,output/f"{stem}.md")
    docx_path=render_docx(contract,output/f"{stem}.docx")
    pdf_path=render_pdf(contract,output/f"{stem}.pdf",source_docx=docx_path)
    print(f"报告契约：{json_path.resolve()}")
    print(f"Markdown：{md_path.resolve()}")
    print(f"Word：{docx_path.resolve()}")
    print(f"PDF：{pdf_path.resolve()}")
    print(f"文本生成：{contract.generation.status}（{contract.generation.mode}）")
    for warning in contract.generation.warnings:
        print(f"生成提示：{warning}")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
