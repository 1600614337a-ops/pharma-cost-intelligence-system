from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import runpy


ROOT = Path(__file__).resolve().parents[1]


class CompetitionDeliveryTests(unittest.TestCase):
    def test_judge_package_includes_formal_technical_solution(self) -> None:
        namespace = runpy.run_path(str(ROOT / "scripts" / "prepare_judge_package.py"))
        formal_documents = namespace["FORMAL_DOCUMENTS"]
        self.assertEqual(len(formal_documents), 2)
        self.assertTrue(any(path.endswith(".docx") for path in formal_documents))
        self.assertTrue(any(path.endswith(".pdf") for path in formal_documents))
        for source in formal_documents:
            self.assertTrue((ROOT / source).is_file(), source)

    def test_demo_preflight_passes_without_port_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output = Path(temporary_dir) / "preflight.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "demo_preflight.py"),
                    "--project-root",
                    str(ROOT),
                    "--skip-port-check",
                    "--json-output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(
                {item["name"] for item in payload["checks"]},
                {"Python运行时", "关键文件", "源数据质量", "知识索引", "报告契约", "LibreOffice"},
            )

    def test_three_scenario_evaluation_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output = Path(temporary_dir) / "evaluation.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "competition_evaluation.py"),
                    "--project-root",
                    str(ROOT),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["summary"]["passed_scenarios"], 3)
            self.assertEqual(payload["summary"]["automated_scenario_pass_rate_pct"], "100.00")
            self.assertTrue(all(item["status"] == "PASS" for item in payload["scenarios"]))

    def test_local_launchers_use_scoped_processes_and_loopback_rpa(self) -> None:
        start_script = (ROOT / "scripts" / "start_demo.ps1").read_text(encoding="utf-8")
        stop_script = (ROOT / "scripts" / "stop_demo.ps1").read_text(encoding="utf-8")
        rpa_wrapper = (ROOT / "scripts" / "run_mock_rpa_local.py").read_text(encoding="utf-8")
        qwen_setup = (ROOT / "scripts" / "configure_qwen.ps1").read_text(encoding="utf-8")
        self.assertIn("run_mock_rpa_local.py", start_script)
        self.assertIn("processes.json", start_script)
        self.assertIn("ConvertTo-SecureString", start_script)
        self.assertIn("COST_LLM_PROVIDER", start_script)
        self.assertIn("-m venv", start_script)
        self.assertIn("requirements.txt", start_script)
        self.assertIn(".requirements.sha256", start_script)
        self.assertIn("Find-AvailablePortPair", start_script)
        self.assertIn("Test-LocalPortAvailable", start_script)
        self.assertIn("port_auto_selected", start_script)
        self.assertIn("Get-OwnedDemoProcess", start_script)
        self.assertIn("Test-DemoHealth", start_script)
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("pywin32", requirements)
        self.assertIn("ChuangLingJingCostIntelligence", qwen_setup)
        self.assertIn("ConvertFrom-SecureString", qwen_setup)
        self.assertIn('response_format = @{ type = "json_object" }', qwen_setup)
        self.assertIn("enable_thinking = $false", qwen_setup)
        self.assertNotIn("sk-ws", qwen_setup)
        self.assertIn("Win32_Process", stop_script)
        self.assertIn('default="127.0.0.1"', rpa_wrapper)
        self.assertIn("host=args.host", rpa_wrapper)


if __name__ == "__main__":
    unittest.main()
