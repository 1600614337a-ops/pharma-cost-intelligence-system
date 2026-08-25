"""Atomic batch generation and globally unique task-number tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.batch import BatchCandidateBundle, BatchRunFailed, BatchScenario, run_batch
from app.reporting import build_report_contract
from app.rpa import build_task_candidates


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_ROOT = PROJECT_ROOT / "06_知识证据索引"


class BatchRunTests(unittest.TestCase):
    def test_same_month_products_have_globally_unique_task_ids(self) -> None:
        task_ids = []
        for product in ("银黄口服液", "板蓝根颗粒", "六味地黄胶囊"):
            contract = build_report_contract(
                PROJECT_ROOT,
                INDEX_ROOT,
                product,
                "2026-05",
                generated_date="2026-08-03",
            )
            task_ids.extend(item.task_id for item in build_task_candidates(contract).candidates)
        self.assertEqual(
            task_ids,
            ["TASK-202605-101", "TASK-202605-201", "TASK-202605-301"],
        )
        self.assertEqual(len(task_ids), len(set(task_ids)))

    def test_failed_staging_run_can_resume_without_overwriting_final_output(self) -> None:
        scenario = BatchScenario(product="银黄口服液", month="2026-05")
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "runs"

            def fail_builder(*args, **kwargs):
                raise RuntimeError("受控测试故障")

            with self.assertRaises(BatchRunFailed) as caught:
                run_batch(
                    data_root=PROJECT_ROOT,
                    index_root=INDEX_ROOT,
                    output_root=output_root,
                    run_id="RUN-TEST-RESUME",
                    generated_date="2026-08-03",
                    scenarios=[scenario],
                    contract_builder=fail_builder,
                )
            staging = caught.exception.staging_path
            self.assertTrue(staging.is_dir())
            failed_manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(failed_manifest["status"], "FAIL")
            self.assertEqual(failed_manifest["failed_scenarios"], 1)

            manifest, final_path = run_batch(
                data_root=PROJECT_ROOT,
                index_root=INDEX_ROOT,
                output_root=output_root,
                run_id="RUN-TEST-RESUME",
                generated_date="2026-08-03",
                scenarios=[scenario],
                resume=True,
            )
            self.assertEqual(manifest.status, "PASS")
            self.assertTrue(final_path.is_dir())
            self.assertFalse(staging.exists())
            aggregate = BatchCandidateBundle.model_validate_json(
                (final_path / "all_candidates.json").read_text(encoding="utf-8")
            )
            self.assertEqual(aggregate.candidate_count, 1)
            self.assertEqual(aggregate.candidates[0].task_id, "TASK-202605-101")
            item = manifest.items[0]
            self.assertIsNotNone(item.pdf_path)
            self.assertTrue((final_path / str(item.pdf_path)).is_file())
            self.assertIn("pdf", item.hashes)
            with self.assertRaises(Exception):
                run_batch(
                    data_root=PROJECT_ROOT,
                    index_root=INDEX_ROOT,
                    output_root=output_root,
                    run_id="RUN-TEST-RESUME",
                    generated_date="2026-08-03",
                    scenarios=[scenario],
                )


if __name__ == "__main__":
    unittest.main()
