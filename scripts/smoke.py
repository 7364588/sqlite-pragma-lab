"""Exercise an installed distribution, including its bundled HTML template."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from sqlite_pragma_lab import run_lab
from sqlite_pragma_lab.report import render_html


def main():
    checks = 0
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        schema = root / "schema.sql"
        seed = root / "seed.sql"
        migration = root / "migration.sql"
        schema.write_text("CREATE TABLE p(id PRIMARY KEY); CREATE TABLE c(id PRIMARY KEY,pid REFERENCES p(id) ON DELETE CASCADE);", encoding="utf-8")
        seed.write_text("INSERT INTO p VALUES(1); INSERT INTO c VALUES(1,1),(2,1);", encoding="utf-8")
        migration.write_text("PRAGMA foreign_keys=OFF; CREATE TABLE p2(id PRIMARY KEY); INSERT INTO p2 SELECT * FROM p; DROP TABLE p; ALTER TABLE p2 RENAME TO p; PRAGMA foreign_keys=ON;", encoding="utf-8")
        args = [sys.executable, "-m", "sqlite_pragma_lab", "run", "--schema", str(schema), "--seed", str(seed), "--migration", str(migration)]
        def run(*extra):
            return subprocess.run([*args, *extra], cwd=root, capture_output=True, text=True, timeout=20)
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (schema, seed, migration)]
        result = run("--format", "json", "--html", str(root / "report.html"))
        assert result.returncode == 1, result.stderr
        report = json.loads(result.stdout)
        assert report["profiles"][0]["status"] == "pass"
        assert report["profiles"][1]["status"] == "review"
        assert all(p["sql_completed"] for p in report["profiles"])
        assert [next(t["rows"] for t in p["final"]["tables"] if t["name"] == "c") for p in report["profiles"]] == [2, 0]
        checks += 1
        html = (root / "report.html").read_bytes()
        assert b"report-data" in html and b"connect-src 'none'" in html
        assert run("--html", str(root / "report.html")).returncode == 2
        assert (root / "report.html").read_bytes() == html
        checks += 1
        assert run("--html", str(schema)).returncode == 2
        assert hashes == [hashlib.sha256(p.read_bytes()).hexdigest() for p in (schema, seed, migration)]
        checks += 1
        migration.write_text("ALTER TABLE p ADD COLUMN label TEXT;", encoding="utf-8")
        assert run().returncode == 0
        checks += 1
        schema.unlink()
        result = run("--format", "json")
        assert result.returncode == 2 and json.loads(result.stdout)["status"] == "error"
        assert str(root) not in result.stdout + result.stderr
        checks += 1
        assert "report-data" in render_html(run_lab("CREATE TABLE t(x);", "SELECT 1;", "INSERT INTO t VALUES(1);"))
        checks += 1
    print(f"Installed distribution smoke: {checks} checks passed.")


if __name__ == "__main__":
    main()
