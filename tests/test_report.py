import json
import re
import unittest

from sqlite_pragma_lab.report import render_html


class ReportTests(unittest.TestCase):
    def test_untrusted_names_remain_inert_data(self):
        report = {"schema": "sqlite-pragma-lab/report-v1", "status": "review",
                  "profiles": [{"name": '</script><img src=x onerror="alert(1)">', "final": None}]}
        html = render_html(report)
        body = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)[1]
        self.assertNotIn("<", body)
        self.assertEqual(json.loads(body), report)
        self.assertIn("connect-src 'none'", html)
        self.assertNotIn("innerHTML", html)

    def test_renderer_has_no_external_assets_and_is_deterministic(self):
        report = {"schema": "sqlite-pragma-lab/report-v1", "status": "pass", "profiles": []}
        self.assertEqual(render_html(report), render_html(report))
        self.assertNotRegex(render_html(report), r'<script[^>]+src=')


if __name__ == "__main__":
    unittest.main()
