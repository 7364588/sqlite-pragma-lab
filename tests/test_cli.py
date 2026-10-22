import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.schema = self.root / "schema.sql"
        self.seed = self.root / "seed.sql"
        self.migration = self.root / "migration.sql"
        self.schema.write_text("CREATE TABLE t(x);", encoding="utf-8")
        self.seed.write_text("INSERT INTO t VALUES('PRIVATE_ROW_VALUE');", encoding="utf-8")
        self.migration.write_text("ALTER TABLE t ADD COLUMN y;", encoding="utf-8")

    def args(self, *extra):
        return [sys.executable, "-m", "sqlite_pragma_lab", "run", "--schema", str(self.schema), "--seed", str(self.seed), "--migration", str(self.migration), *extra]

    def run_cli(self, *extra):
        return subprocess.run(self.args(*extra), capture_output=True, text=True, encoding="utf-8", timeout=20)

    def test_clean_json_and_unchanged_inputs(self):
        paths = [self.schema, self.seed, self.migration]
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        result = self.run_cli("--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "pass")
        self.assertNotIn("PRIVATE_ROW_VALUE", result.stdout)
        self.assertNotIn(str(self.root), result.stdout)
        self.assertEqual(hashes, [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])

    def test_review_exit_and_text_summary(self):
        self.migration.write_text("DELETE FROM t;", encoding="utf-8")
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FINAL_ROW_DECREASE", result.stdout)
        self.assertIn('"t": 1 -> 0', result.stdout)

    def test_missing_input_private_error(self):
        self.schema.unlink()
        result = self.run_cli("--format", "json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "error")
        self.assertNotIn(str(self.root), result.stdout + result.stderr)

    def test_bad_baseline_does_not_echo_sql(self):
        self.schema.write_text("SELECT THIS_PRIVATE_IDENTIFIER_DOES_NOT_EXIST;", encoding="utf-8")
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("THIS_PRIVATE_IDENTIFIER", result.stdout + result.stderr)
        self.assertIn("Baseline schema failed", result.stderr)

    def test_html_report_is_exclusive_and_contains_only_summary_data(self):
        report = self.root / "report.html"
        result = self.run_cli("--html", str(report))
        self.assertEqual(result.returncode, 0)
        html = report.read_text(encoding="utf-8")
        self.assertIn("report-data", html)
        self.assertNotIn("PRIVATE_ROW_VALUE", html)
        self.assertNotIn("CREATE TABLE", html)
        original = report.read_bytes()
        self.assertEqual(self.run_cli("--html", str(report)).returncode, 2)
        self.assertEqual(report.read_bytes(), original)
        original = self.schema.read_bytes()
        self.assertEqual(self.run_cli("--html", str(self.schema)).returncode, 2)
        self.assertEqual(self.schema.read_bytes(), original)
        self.assertFalse(list(self.root.glob(".sqlite-pragma-lab-*")))

    def test_invalid_utf8_nul_and_oversize(self):
        for content in (b"\xff", b"SELECT '\x00';", b" " * (1024 * 1024 + 1)):
            with self.subTest(size=len(content)):
                self.migration.write_bytes(content)
                self.assertEqual(self.run_cli().returncode, 2)

    def test_bom_is_accepted(self):
        self.schema.write_bytes(b"\xef\xbb\xbfCREATE TABLE t(x);")
        self.assertEqual(self.run_cli().returncode, 0)

    def test_usage_and_version(self):
        for argument in ("--help", "--version"):
            result = subprocess.run([sys.executable, "-m", "sqlite_pragma_lab", argument], capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0)
        result = subprocess.run([sys.executable, "-m", "sqlite_pragma_lab", "--PRIVATE_UNKNOWN"], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(b"PRIVATE_UNKNOWN", result.stderr)

    def test_closed_stdout_returns_two_including_argparse(self):
        for unbuffered in (False, True):
            for arguments in (["--help"], ["--version"], self.args("--format", "json")[3:]):
                with self.subTest(unbuffered=unbuffered, argument=arguments[0]):
                    env = os.environ.copy()
                    env["PYTHONUNBUFFERED"] = "1" if unbuffered else "0"
                    child = subprocess.Popen([sys.executable, "-m", "sqlite_pragma_lab", *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                    child.stdout.close()
                    error = child.stderr.read()
                    child.stderr.close()
                    self.assertEqual(child.wait(timeout=20), 2, error)
                    self.assertNotIn(b"Exception ignored", error)

    def test_closed_stderr_returns_two(self):
        for arguments in (["--invalid"], self.args()[3:]):
            with self.subTest(argument=arguments[0]):
                self.schema.unlink(missing_ok=True)
                child = subprocess.Popen([sys.executable, "-m", "sqlite_pragma_lab", *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                child.stderr.close()
                output = child.stdout.read()
                child.stdout.close()
                self.assertEqual(child.wait(timeout=20), 2, output)


if __name__ == "__main__":
    unittest.main()
