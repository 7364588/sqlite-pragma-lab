# Maintenance

For routine maintenance, inspect real issues, pull requests, current-main CI, and
available security alerts. Run existing tests when the environment or dependency
set changes. Do not manufacture commits or releases for unchanged audits.

For Python or SQLite updates, prioritize foreign-key toggles inside transactions,
table renames, cascades, deferred constraints, trigger failures, authorizer
callbacks, resource interruption, and final rollback measurements. The report
must continue to record the actual runtime version. Different versions can
legitimately produce different results; do not normalize away semantic changes.

Before a release:

1. Run the complete unittest suite and regenerate the synthetic demo.
2. Build wheel and source distributions with `python -m build`.
3. Install each distribution into its own fresh virtual environment and run
   `python scripts/smoke.py` with that environment's interpreter, outside any
   source-path override. Verify the HTML template is included.
4. Review package contents for credentials, local paths, caches, and unintended
   files. Verify CI against the exact release commit before publishing artifacts.

Actions are pinned to immutable official commits. Review update provenance and
diffs before merging; do not bypass failing checks. Runtime dependencies remain
stdlib-only; build tooling is a separate dependency boundary.
