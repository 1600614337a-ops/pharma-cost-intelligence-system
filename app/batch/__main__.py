"""CLI for complete or resumed batch report runs."""

from __future__ import annotations

import argparse
from datetime import date, datetime

from . import BatchRunError, BatchRunFailed, run_batch


def main() -> int:
    parser = argparse.ArgumentParser(description="批量生成全部产品月份的报告和任务候选")
    parser.add_argument("--data-root", default=".")
    parser.add_argument("--index-root", default="06_知识证据索引")
    parser.add_argument("--output-root", default="10_批量运行")
    parser.add_argument("--run-id", default=f"RUN-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    parser.add_argument("--generated-date", default=date.today().isoformat())
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        manifest, path = run_batch(
            data_root=args.data_root,
            index_root=args.index_root,
            output_root=args.output_root,
            run_id=args.run_id,
            generated_date=args.generated_date,
            resume=args.resume,
        )
    except BatchRunFailed as exc:
        print(f"批次失败：{exc}")
        print(f"暂存目录：{exc.staging_path}")
        return 2
    except BatchRunError as exc:
        print(f"批次被阻断：{exc}")
        return 3
    print(f"批次状态：{manifest.status}")
    print(f"场景：{manifest.passed_scenarios}/{manifest.total_scenarios}")
    print(f"输出目录：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
