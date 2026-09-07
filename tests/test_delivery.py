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
    def test_judge_package_keeps_formal_documents_separate(self) -> None:
        namespace = runpy.run_path(str(ROOT / "scripts" / "prepare_judge_package.py"))
        evaluation_evidence = namespace["EVALUATION_EVIDENCE"]
        self.assertEqual(len(evaluation_evidence), 3)
        for source, destination in evaluation_evidence.items():
            self.assertTrue(
                (ROOT / source).is_file() or (ROOT / destination).is_file(),
                source,
            )
        script = (ROOT / "scripts" / "prepare_judge_package.py").read_text(encoding="utf-8")
        public_script = (ROOT / "scripts" / "prepare_public_release.py").read_text(encoding="utf-8")
        self.assertNotIn("FORMAL_DOCUMENTS", script)
        self.assertNotIn("FORMAL_DOCUMENTS", public_script)
        self.assertIn('output.with_name(output.name + ".zip")', script)

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
            self.assertEqual(payload["summary"]["report_structure_completeness_pct"], "100.00")
            self.assertEqual(payload["summary"]["report_structure_checks"], "9/9")
            self.assertEqual(payload["summary"]["benchmark_three_step_format_accuracy_pct"], "100.00")
            self.assertEqual(payload["summary"]["benchmark_three_step_checks"], "9/9")
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
        self.assertIn("initialize_python.ps1", start_script)
        preflight_script = (ROOT / "scripts" / "preflight_demo.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("initialize_python.ps1", preflight_script)
        bootstrap = (ROOT / "scripts" / "initialize_python.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("-m venv", bootstrap)
        self.assertIn("requirements.txt", bootstrap)
        self.assertIn(".requirements.sha256", bootstrap)
        self.assertIn("Test-PythonRuntime", bootstrap)
        self.assertIn(".environment-root", bootstrap)
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

    def test_docker_launchers_use_a_directory_independent_project_name(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        windows_start = (ROOT / "启动跨环境演示.cmd").read_text(encoding="utf-8")
        windows_stop = (ROOT / "停止跨环境演示.cmd").read_text(encoding="utf-8")
        unix_start = (ROOT / "start-docker.sh").read_text(encoding="utf-8")
        unix_stop = (ROOT / "stop-docker.sh").read_text(encoding="utf-8")
        self.assertTrue(compose.startswith("name: pharma-cost-intelligence\n"))
        self.assertNotIn("init: true", compose)
        for launcher in (windows_start, windows_stop, unix_start, unix_stop):
            self.assertIn("docker compose -p pharma-cost-intelligence", launcher)
        self.assertIn("--wait --wait-timeout 180", windows_start)
        self.assertIn("docker info", windows_start)

    def test_windows_entrypoints_preserve_exit_codes(self) -> None:
        for name in (
            "一键启动系统.cmd",
            "停止演示系统.cmd",
            "演示前预检.cmd",
            "启动跨环境演示.cmd",
            "停止跨环境演示.cmd",
        ):
            content = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("setlocal", content, name)
            self.assertIn("exit /b", content, name)

    def test_windows_entrypoints_use_archive_safe_crlf(self) -> None:
        for name in (
            "一键启动系统.cmd",
            "停止演示系统.cmd",
            "演示前预检.cmd",
            "启动跨环境演示.cmd",
            "停止跨环境演示.cmd",
            "配置通义千问API.cmd",
        ):
            content = (ROOT / name).read_bytes()
            self.assertIn(b"\r\n", content, name)
            self.assertNotIn(b"\n", content.replace(b"\r\n", b""), name)

    def test_docker_image_installs_and_refreshes_cjk_fonts(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        charts = (ROOT / "app" / "reporting" / "charts.py").read_text(encoding="utf-8")
        self.assertIn("fonts-noto-cjk", dockerfile)
        self.assertIn("fc-cache -f", dockerfile)
        self.assertIn("NotoSansCJK-Regular.ttc", charts)


if __name__ == "__main__":
    unittest.main()
