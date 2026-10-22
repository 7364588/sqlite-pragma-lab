"""Regenerate the public report from the repository's synthetic SQL fixture."""
import json
from pathlib import Path

from sqlite_pragma_lab import run_lab
from sqlite_pragma_lab.report import render_html


def main():
    root = Path(__file__).resolve().parents[1]
    example = root / "examples" / "folder-rebuild"
    read = lambda name: (example / name).read_text(encoding="utf-8")
    report = run_lab(read("schema.sql"), read("migration.sql"), read("seed.sql"))
    report["demo"] = True
    auto, outer = report["profiles"]
    assert auto["status"] == "pass" and outer["status"] == "review"
    assert all(p["sql_completed"] for p in report["profiles"])
    assert {t["name"]: t["rows"] for t in outer["final"]["tables"]}["files"] == 0
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "demo.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (docs / "index.html").write_text(render_html(report), encoding="utf-8", newline="\n")
    print("Synthetic demo generated: autocommit preserves 2 files; outer transaction retains 0.")


if __name__ == "__main__":
    main()
