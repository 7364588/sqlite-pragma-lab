"""Explicit SQL input CLI, with private errors and exclusive report creation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from . import __version__
from .core import LabError, Limits, run_lab


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise LabError("Invalid command-line arguments; use --help for usage.")

    def _print_message(self, message, file=None):
        if message:
            stream = file if file is not None else sys.stdout
            stream.write(message)
            stream.flush()


def _parser():
    parser = _Parser(description="Compare SQLite foreign-key behavior under two transaction wrappers.")
    parser.add_argument("--version", action="version", version="sqlite-pragma-lab " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run explicitly selected SQL files in isolated memory databases")
    run.add_argument("--schema", type=Path, required=True)
    run.add_argument("--seed", type=Path)
    run.add_argument("--migration", type=Path, required=True)
    run.add_argument("--format", choices=("text", "json"), default="text")
    run.add_argument("--html", type=Path, metavar="NEW_REPORT")
    return parser


def _read(path):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise LabError("SQL inputs must be regular files, not symbolic links.")
    with path.open("rb") as stream:
        content = stream.read(Limits().max_input_bytes + 1)
    if len(content) > Limits().max_input_bytes:
        raise LabError("A SQL input exceeds the byte limit.")
    try:
        return content.decode("utf-8-sig")
    except UnicodeError as exc:
        raise LabError("SQL inputs must be UTF-8 text.") from exc


def _write_new(path, content):
    temporary = None
    try:
        if os.path.lexists(path):
            raise LabError("Report destination already exists; choose a new file.")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".sqlite-pragma-lab-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _summary(report):
    lines = ["SQLite Pragma Lab: " + report["status"].upper(), "SQLite " + report["engine"]["version"] + " | isolated memory databases"]
    for profile in report["profiles"]:
        lines.append(f"{profile['name']}: {profile['status']} ({profile['completion']})")
        for step in profile["steps"]:
            if step["findings"]:
                codes = ", ".join(item["code"] for item in step["findings"])
                lines.append(f"  Statement {step['index']}, line {step['line']}, {step['kind']}: {codes}")
        for change in profile["final_changes"]:
            # JSON escaping keeps hostile identifiers from becoming terminal controls.
            name = json.dumps(change["table"], ensure_ascii=True)
            lines.append(f"  {name}: {change['before']} -> {change['after']} ({change['kind']})")
        for finding in profile["findings"]:
            lines.append("  " + finding["code"] + ": " + finding["message"])
    lines.append("Counts are not content equality. No production database was opened or changed.")
    return "\n".join(lines)


def _emit_error(message, json_mode):
    try:
        if json_mode:
            sys.stdout.write(json.dumps({"schema": "sqlite-pragma-lab/report-v1", "status": "error", "error": message}) + "\n")
            sys.stdout.flush()
        else:
            sys.stderr.write("sqlite-pragma-lab: " + message + "\n")
            sys.stderr.flush()
    except (OSError, ValueError, UnicodeError):
        pass


def _quiet_failed_streams():
    # Avoid a second failing flush at interpreter shutdown changing exit 2 to 120.
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        try:
            stream.flush()
        except (OSError, ValueError, UnicodeError):
            try:
                descriptor = stream.fileno()
                sink = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(sink, descriptor)
                finally:
                    os.close(sink)
                stream.flush()
            except (OSError, ValueError, UnicodeError, AttributeError):
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def main(argv=None):
    args = None
    try:
        try:
            args = _parser().parse_args(argv)
        except SystemExit as exit_signal:
            # argparse help/version can buffer their output; flush inside the
            # same failure boundary as ordinary report output.
            sys.stdout.flush()
            return int(exit_signal.code)
        if args.html and (os.path.lexists(args.html) or not args.html.parent.is_dir()):
            raise LabError("Report destination must be new and its parent directory must exist.")
        report = run_lab(_read(args.schema), _read(args.migration), _read(args.seed) if args.seed else None)
        if args.html:
            from .report import render_html
            _write_new(args.html, render_html(report))
        sys.stdout.write((json.dumps(report, ensure_ascii=True, indent=2) if args.format == "json" else _summary(report)) + "\n")
        sys.stdout.flush()
        return 0 if report["status"] == "pass" else 1
    except (LabError, OSError, ValueError, UnicodeError) as error:
        message = str(error) if isinstance(error, LabError) else "Cannot read input or write output; check file paths, permissions, and text encoding."
        _quiet_failed_streams()
        _emit_error(message, args is not None and args.format == "json")
        _quiet_failed_streams()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
