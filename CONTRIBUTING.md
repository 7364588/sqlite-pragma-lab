# Contributing

Use Python 3.11+ and a virtual environment. Install with `python -m pip install -e .`.
Run `python -m unittest discover -s tests -v`, `python scripts/smoke.py`, and
`python scripts/demo.py` before submitting changes. The test suite uses standard
library unittest and synthetic SQL only.

For behavior reports, include Python and SQLite versions, a minimal synthetic
schema/seed/migration, expected counts, and observed profile results. Do not post
production rows, credentials, confidential identifiers, or database files. A
smaller reproducible fixture is more useful than a complete application dump.

Changes to SQLite semantics should be checked against a direct in-memory SQLite
execution, not just a copy of the implementation. Preserve explicit transaction
control, distinguish unavailable measurements from zero, and test commit-time
failures and partial statement effects where relevant. Include a regression for
a demonstrated bug. Do not claim row-count preservation proves content equality.

Keep runtime dependencies at zero and HTML reports self-contained. UI changes
must use text DOM APIs for input-derived identifiers and retain the content
security policy. Discuss report-schema changes and expanded SQL permissions in
an issue before a large implementation.
