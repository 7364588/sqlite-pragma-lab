"""Run bounded SQL fixtures under two explicit, isolated SQLite profiles.

SQL runs only in memory. This is a diagnostic harness, not a hostile-code sandbox
or an emulator for a particular ORM, libSQL, D1, or mobile database runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import json
import sqlite3
import time


class LabError(ValueError):
    """Invalid, unsupported, or excessive input; messages omit source contents."""


@dataclass(frozen=True)
class Limits:
    max_input_bytes: int = 1024 * 1024
    max_statement_bytes: int = 256 * 1024
    max_statements: int = 512
    max_tables: int = 64
    max_columns: int = 128
    max_pages: int = 4096
    max_value_bytes: int = 1024 * 1024
    max_vm_steps: int = 2_000_000
    max_report_bytes: int = 8 * 1024 * 1024
    timeout_seconds: float = 5.0

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "timeout_seconds":
                if type(value) not in {float, int} or not 0 < value <= 60:
                    raise LabError("Timeout must be positive and at most 60 seconds.")
            elif type(value) is not int or not 0 < value <= 2_147_483_647:
                raise LabError("Resource limits must be positive integers within SQLite's signed 32-bit range.")


@dataclass(frozen=True)
class _Statement:
    sql: str
    index: int
    line: int
    kind: str


def _kind(tokens: list[str]) -> str:
    if not tokens:
        return "OTHER"
    first = tokens[0].upper()
    if first in {"CREATE", "DROP", "ALTER"}:
        for token in tokens[1:4]:
            if token.upper() in {"TABLE", "INDEX", "VIEW", "TRIGGER"}:
                return first + " " + token.upper()
    return first if first in {"PRAGMA", "SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "EXPLAIN", "VACUUM", "ATTACH", "DETACH", "REINDEX", "ANALYZE"} else "OTHER"


def _split(sql: str, limits: Limits) -> list[_Statement]:
    if not isinstance(sql, str):
        raise LabError("SQL input must be text.")
    try:
        if len(sql.encode("utf-8")) > limits.max_input_bytes:
            raise LabError("SQL input exceeds the byte limit.")
    except UnicodeError as exc:
        raise LabError("SQL input is not valid Unicode.") from exc
    if "\0" in sql:
        raise LabError("SQL input contains a NUL character.")
    result = []
    start = 0
    line = 1
    first_line = None
    tokens: list[str] = []
    token = ""
    state = "normal"
    checks = 0
    i = 0

    def finish(end):
        nonlocal start, first_line, tokens, token
        text = sql[start:end]
        if len(text.encode("utf-8")) > limits.max_statement_bytes:
            raise LabError("A SQL statement exceeds the byte limit.")
        if first_line is not None:
            result.append(_Statement(text, len(result) + 1, first_line, _kind(tokens)))
            if len(result) > limits.max_statements:
                raise LabError("SQL exceeds the statement-count limit.")
        start, first_line, tokens, token = end, None, [], ""

    while i < len(sql):
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if state == "line_comment":
            if c == "\n":
                state = "normal"
        elif state == "block_comment":
            if c == "*" and nxt == "/":
                state = "normal"
                i += 1
        elif state in {"'", '"', "`"}:
            if c == state:
                if nxt == state:
                    i += 1
                else:
                    state = "normal"
        elif state == "]":
            if c == "]":
                state = "normal"
        else:
            if c.isascii() and (c.isalnum() or c == "_"):
                if first_line is None:
                    first_line = line
                if len(tokens) < 4 and len(token) < 32:
                    token += c
            else:
                if token:
                    tokens.append(token)
                    token = ""
                if c == "-" and nxt == "-":
                    state = "line_comment"
                    i += 1
                elif c == "/" and nxt == "*":
                    state = "block_comment"
                    i += 1
                elif c in {"'", '"', "`", "["}:
                    if first_line is None:
                        first_line = line
                    state = "]" if c == "[" else c
                elif c == ";":
                    checks += 1
                    if checks > 4096:
                        raise LabError("SQL exceeds the statement-boundary work limit.")
                    if sqlite3.complete_statement(sql[start:i + 1]):
                        finish(i + 1)
                elif not c.isspace() and first_line is None:
                    first_line = line
        if c == "\n":
            line += 1
        i += 1
    if token:
        tokens.append(token)
    if first_line is not None:
        if state not in {"normal", "line_comment"} or not sqlite3.complete_statement(sql[start:] + "\n;"):
            raise LabError("SQL ends in an incomplete statement.")
        finish(len(sql))
    return result


class _Budget:
    def __init__(self, limits: Limits):
        self.limit = limits.max_vm_steps
        self.deadline = time.monotonic() + limits.timeout_seconds
        self.steps = 0
        self.exhausted = False

    def progress(self):
        self.steps += 1000
        self.exhausted = self.steps > self.limit or time.monotonic() > self.deadline
        return int(self.exhausted)

    def check(self):
        if time.monotonic() > self.deadline or self.exhausted:
            self.exhausted = True
            raise LabError("SQL execution exceeded its resource budget.")


class _Guard:
    def __init__(self):
        self.internal = False
        self.pragma_requests: list[tuple[str, str | None]] = []
        self.denied = False

    def authorize(self, action, arg1, arg2, database, trigger):
        if self.internal:
            return sqlite3.SQLITE_OK
        denied_actions = {
            sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE,
            sqlite3.SQLITE_CREATE_TEMP_TABLE, sqlite3.SQLITE_CREATE_TEMP_INDEX,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
        }
        # Qualified CREATE TABLE temp.name uses CREATE_TABLE, not CREATE_TEMP_TABLE.
        # Do not reject every temp-schema callback: ordinary ALTER TABLE needs
        # SQLite's internal reads/updates of sqlite_temp_master during a rename.
        create_actions = {sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
                          sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW}
        denied = action in denied_actions or (database == "temp" and action in create_actions)
        if action == sqlite3.SQLITE_PRAGMA:
            pragma = (arg1 or "").lower()
            self.pragma_requests.append((pragma, arg2))
            denied = denied or pragma not in {"foreign_keys", "defer_foreign_keys"}
        if action == sqlite3.SQLITE_FUNCTION:
            denied = denied or (arg2 or arg1 or "").lower() in {"load_extension", "readfile", "writefile", "fts3_tokenizer"}
        if denied:
            self.denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


def _connection(limits: Limits):
    conn = sqlite3.connect(":memory:", isolation_level=None, cached_statements=0)
    try:
        return _configure(conn, limits)
    except Exception:
        conn.close()
        raise


def _configure(conn, limits):
    try:
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.NotSupportedError):
        pass  # Some builds omit extension loading entirely.
    for pragma in ("page_size=4096", f"max_page_count={limits.max_pages}", "temp_store=MEMORY", "journal_mode=MEMORY", "trusted_schema=OFF", "foreign_keys=ON"):
        conn.execute("PRAGMA " + pragma)
    if conn.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise LabError("This SQLite build does not support enabled foreign keys.")
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, limits.max_value_bytes)
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, limits.max_statement_bytes)
    conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, limits.max_columns)
    conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 64)
    conn.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 32)
    conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 128)
    conn.setlimit(sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH, 32)
    guard = _Guard()
    budget = _Budget(limits)
    conn.set_authorizer(guard.authorize)
    conn.set_progress_handler(budget.progress, 1000)
    return conn, guard, budget


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _snapshot(conn, guard, budget, limits):
    budget.check()
    guard.internal = True
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='table' ORDER BY name") if not row[0].lower().startswith("sqlite_")]
        if len(names) > limits.max_tables:
            raise LabError("Database exceeds the table-count limit.")
        tables = []
        edges = []
        for name in names:
            budget.check()
            tables.append({"name": name, "rows": conn.execute("SELECT COUNT(*) FROM main." + _quote(name)).fetchone()[0]})
            for edge in conn.execute("PRAGMA main.foreign_key_list(" + _quote(name) + ")"):
                if len(edges) >= 512:
                    raise LabError("Database exceeds the foreign-key metadata limit.")
                edges.append({"table": name, "id": edge[0], "seq": edge[1], "parent": edge[2], "from": edge[3], "to": edge[4], "on_update": edge[5], "on_delete": edge[6]})
        violations = {}
        for table, _rowid, parent, key in conn.execute("PRAGMA main.foreign_key_check"):
            budget.check()
            identifier = (table, parent, key)
            violations[identifier] = violations.get(identifier, 0) + 1
        integrity = list(conn.execute("PRAGMA main.integrity_check(10)"))
        return {
            "foreign_keys": bool(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
            "in_transaction": conn.in_transaction,
            "tables": tables,
            "foreign_key_edges": sorted(edges, key=lambda x: (x["table"], x["id"], x["seq"])),
            "foreign_key_violations": [{"table": k[0], "parent": k[1], "id": k[2], "count": v} for k, v in sorted(violations.items())],
            "integrity_ok": integrity == [("ok",)],
        }
    finally:
        guard.internal = False


def _measure(conn, guard, budget, limits):
    try:
        return _snapshot(conn, guard, budget, limits)
    except (sqlite3.Error, LabError):
        return None


def _changes(before, after):
    if before is None or after is None:
        return []
    old = {x["name"]: x["rows"] for x in before["tables"]}
    new = {x["name"]: x["rows"] for x in after["tables"]}
    changes = []
    for table in sorted(old.keys() | new.keys()):
        a, z = old.get(table), new.get(table)
        if a == z:
            continue
        kind = "added" if a is None else "removed" if z is None else "increased" if z > a else "decreased"
        changes.append({"table": table, "before": a, "after": z, "delta": None if a is None or z is None else z - a, "kind": kind})
    return changes


def _finding(code, message, severity="warning"):
    return {"code": code, "severity": severity, "message": message}


def _pragma_boolean(value):
    if value is None:
        return None
    # Use SQLite's own boolean conversion (including 00, 01 and quoted values),
    # without interpolating unquoted SQL or changing the profile connection.
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("PRAGMA foreign_keys='" + value.replace("'", "''") + "'")
        return bool(probe.execute("PRAGMA foreign_keys").fetchone()[0])
    finally:
        probe.close()


def _error_code(exc, guard, budget):
    if budget.exhausted:
        return "RESOURCE_LIMIT"
    if guard.denied:
        return "SQLITE_AUTH"
    if isinstance(exc, LabError):
        return "RESOURCE_LIMIT"
    return getattr(exc, "sqlite_errorname", "SQLITE_ERROR")


def _execute(conn, guard, budget, statement):
    budget.check()
    guard.pragma_requests.clear()
    guard.denied = False
    with _Cursor(conn.execute(statement.sql)) as cursor:
        for _row in cursor:
            budget.check()


class _Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, *args):
        self.cursor.close()


def _build_baseline(schema, seed, limits):
    conn, guard, budget = _connection(limits)
    try:
        for label, statements in (("schema", schema), ("seed", seed)):
            for statement in statements:
                try:
                    _execute(conn, guard, budget, statement)
                except (sqlite3.Error, LabError) as exc:
                    raise LabError(f"Baseline {label} failed at statement {statement.index} ({_error_code(exc, guard, budget)}).") from exc
            if conn.in_transaction:
                raise LabError(f"Baseline {label} leaves a transaction open.")
        conn.execute("PRAGMA foreign_keys=ON")
        baseline = _snapshot(conn, guard, budget, limits)
        return conn, baseline
    except Exception:
        conn.close()
        raise


def _profile(baseline_conn, baseline, migration, name, limits):
    conn, guard, budget = _connection(limits)
    try:
        baseline_conn.backup(conn)
        if name == "outer_transaction":
            conn.execute("BEGIN")
        initial = _snapshot(conn, guard, budget, limits)
        result = {"name": name, "status": "pass", "sql_completed": False, "completion": "incomplete", "initial": initial, "steps": [], "findings": []}
        failed = False
        previous = initial
        report_bytes = len(json.dumps(initial, ensure_ascii=True)) * 2
        for statement in migration:
            findings = []
            step = {"index": statement.index, "line": statement.line, "kind": statement.kind, "outcome": "ok", "error_code": None, "before": previous, "after": None, "changes": [], "findings": findings}
            try:
                _execute(conn, guard, budget, statement)
            except (sqlite3.Error, LabError) as exc:
                failed = True
                step["outcome"], step["error_code"] = "error", _error_code(exc, guard, budget)
                findings.append(_finding("STATEMENT_FAILED", "The statement failed; the error code is shown without SQL or row values.", "error"))
            after = _measure(conn, guard, budget, limits)
            if after is not None:
                report_bytes += len(json.dumps(after, ensure_ascii=True)) * 2
                if report_bytes > limits.max_report_bytes:
                    after = None
            step["after"], step["changes"] = after, _changes(previous, after)
            if after is None:
                failed = True
                findings.append(_finding("MEASUREMENT_UNAVAILABLE", "After-state measurement was unavailable or exceeded a resource limit.", "error"))
            else:
                for pragma, requested in guard.pragma_requests:
                    desired = _pragma_boolean(requested) if pragma == "foreign_keys" else None
                    if pragma == "foreign_keys" and desired is not None and previous["in_transaction"] and previous["foreign_keys"] != desired and after["foreign_keys"] == previous["foreign_keys"]:
                        findings.append(_finding("FOREIGN_KEYS_TOGGLE_IGNORED", "Foreign-key setting did not change inside an active transaction."))
                if any(change["kind"] == "decreased" for change in step["changes"]):
                    findings.append(_finding("ROWS_DECREASED", "A table present before and after this statement has fewer rows; later rollback or statements may restore them."))
                if name == "outer_transaction" and previous["in_transaction"] and not after["in_transaction"] and not failed:
                    findings.append(_finding("OUTER_TRANSACTION_ENDED", "The supplied SQL ended the runner transaction; later statements run in their observed transaction state."))
            result["steps"].append(step)
            previous = after
            if failed:
                break
        result["sql_completed"] = not failed
        finalization = {"action": "none", "outcome": "ok", "error_code": None}
        if not failed and conn.in_transaction and name == "autocommit":
            failed = True
            result["findings"].append(_finding("UNCLOSED_TRANSACTION", "The migration left a transaction open; the lab rolls it back instead of silently committing it.", "error"))
        if not failed and conn.in_transaction:
            finalization["action"] = "commit"
            try:
                budget.check()
                conn.execute("COMMIT")
                result["completion"] = "committed"
            except (sqlite3.Error, LabError) as exc:
                failed = True
                finalization.update(outcome="error", error_code=_error_code(exc, guard, budget))
                result["findings"].append(_finding("COMMIT_FAILED", "Runner commit failed; any open transaction is rolled back.", "error"))
        if failed and conn.in_transaction:
            conn.set_progress_handler(None, 0)
            conn.execute("ROLLBACK")
            if finalization["action"] == "none":
                finalization["action"] = "rollback"
            finalization["rollback"] = "completed"
            result["completion"] = "rolled_back"
        elif result["completion"] == "incomplete":
            result["completion"] = "stopped" if failed else "autocommit"
        # Reserve an independent bounded measurement after rollback/commit. An
        # interrupted statement's unknown after-state must never become zero.
        recovery = _Budget(limits)
        conn.set_progress_handler(recovery.progress, 1000)
        final = _measure(conn, guard, recovery, limits)
        result.update(finalization=finalization, final=final, final_changes=_changes(baseline, final))
        if final is None:
            failed = True
            result["findings"].append(_finding("FINAL_MEASUREMENT_UNAVAILABLE", "Final state could not be measured within limits.", "error"))
        else:
            if any(c["kind"] == "decreased" for c in result["final_changes"]):
                result["findings"].append(_finding("FINAL_ROW_DECREASE", "Final table row counts decreased from the baseline; intentional deletions also require review."))
            if any(c["kind"] == "removed" for c in result["final_changes"]):
                result["findings"].append(_finding("FINAL_TABLE_REMOVED", "A baseline table is absent from the final schema; intentional removals also require review."))
            if final["foreign_key_violations"]:
                result["findings"].append(_finding("FOREIGN_KEY_VIOLATIONS", "Final foreign-key violations were observed.", "error"))
            if not final["integrity_ok"]:
                result["findings"].append(_finding("INTEGRITY_CHECK_FAILED", "Final integrity check did not return ok.", "error"))
        if not any(table["rows"] > 0 for table in baseline["tables"]):
            result["findings"].append(_finding("EMPTY_BASELINE", "All baseline tables are empty or absent; populated-data behavior was not exercised.", "info"))
        needs_review = bool(result["findings"]) or any(step["findings"] for step in result["steps"])
        result["status"] = "failed" if failed else "review" if needs_review else "pass"
        return result
    finally:
        conn.close()


def run_lab(schema_sql: str, migration_sql: str, seed_sql: str | None = None, *, limits: Limits | None = None) -> dict:
    """Compare two real SQLite executions without opening a database file.

    SQL exceptions in migration profiles are evidence in the report. Invalid
    inputs or a baseline that cannot be built raise LabError. Raw SQL, input file
    paths, row values, and SQLite's potentially sensitive error messages are not
    included. A lower final row count is a review signal, not a security verdict.
    """
    limits = Limits() if limits is None else limits
    if not isinstance(limits, Limits):
        raise LabError("limits must be a Limits instance.")
    schema = _split(schema_sql, limits)
    seed = _split("" if seed_sql is None else seed_sql, limits)
    migration = _split(migration_sql, limits)
    if not schema or not migration:
        raise LabError("Schema and migration must each contain at least one statement.")
    if sum(map(len, (schema, seed, migration))) > limits.max_statements:
        raise LabError("Combined SQL exceeds the statement-count limit.")
    baseline_conn = None
    try:
        baseline_conn, baseline = _build_baseline(schema, seed, limits)
        profiles = [_profile(baseline_conn, baseline, migration, name, limits) for name in ("autocommit", "outer_transaction")]
    except sqlite3.Error as exc:
        raise LabError("Cannot build or measure the selected SQL fixture.") from exc
    finally:
        if baseline_conn is not None:
            baseline_conn.close()
    return {
        "schema": "sqlite-pragma-lab/report-v1",
        "status": "pass" if all(profile["status"] == "pass" for profile in profiles) else "review",
        "engine": {"name": "SQLite", "version": sqlite3.sqlite_version, "foreign_keys_supported": True},
        "inputs": {"schema_statements": len(schema), "seed_statements": len(seed), "migration_statements": len(migration)},
        "scope": ["Only explicitly supplied SQL runs in isolated memory databases.", "Counts describe rows, not content equality or runtime compatibility.", "No SQL bodies, row values, input paths, production connections, or ORM runtime emulation are included."],
        "profiles": profiles,
    }
