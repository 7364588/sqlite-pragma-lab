# Changelog

## 0.1.0 — 2026-10-22

- Compare explicit SQL fixtures under autocommit and an outer transaction in isolated memory databases.
- Record effective foreign-key state, statement locations, row counts, foreign-key edges, and final commit or rollback results.
- Report ignored foreign-key toggles, row decreases, failed statements, incomplete measurements, and empty baseline coverage.
- Provide JSON, terminal summaries, and an offline interactive HTML report.
- Add synthetic examples, bounded execution, no-clobber report creation, and installed-distribution smoke checks.
