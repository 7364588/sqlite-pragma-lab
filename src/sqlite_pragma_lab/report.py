"""Standalone, local report rendering without external assets."""

import json
from importlib.resources import files


def render_html(report: dict) -> str:
    payload = json.dumps(report, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return files("sqlite_pragma_lab").joinpath("report.html").read_text(encoding="utf-8").replace("__REPORT_DATA__", payload)
