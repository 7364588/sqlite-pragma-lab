import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sqlite_pragma_lab import LabError, Limits, run_lab


SCHEMA = "CREATE TABLE folders(id INTEGER PRIMARY KEY); CREATE TABLE files(id INTEGER PRIMARY KEY,folder_id REFERENCES folders(id) ON DELETE CASCADE);"
SEED = "INSERT INTO folders VALUES(1); INSERT INTO files VALUES(1,1),(2,1);"
REBUILD = "PRAGMA foreign_keys=OFF; CREATE TABLE new_folders(id INTEGER PRIMARY KEY); INSERT INTO new_folders SELECT * FROM folders; DROP TABLE folders; ALTER TABLE new_folders RENAME TO folders; PRAGMA foreign_keys=ON;"


def rows(snapshot):
    return {t["name"]: t["rows"] for t in snapshot["tables"]}


class CoreTests(unittest.TestCase):
    def test_cascade_fixture_and_final_integrity(self):
        report = run_lab(SCHEMA, REBUILD, SEED)
        auto, outer = report["profiles"]
        self.assertEqual(auto["status"], "pass")
        self.assertEqual(rows(auto["final"])["files"], 2)
        self.assertEqual(rows(outer["final"])["files"], 0)
        self.assertTrue(outer["sql_completed"])
        self.assertEqual(outer["completion"], "committed")
        self.assertTrue(outer["final"]["integrity_ok"])
        self.assertEqual(outer["final"]["foreign_key_violations"], [])
        self.assertEqual(outer["steps"][3]["kind"], "DROP TABLE")
        self.assertTrue(any(c["table"] == "files" and c["delta"] == -2 for c in outer["steps"][3]["changes"]))
        self.assertEqual(outer["initial"]["foreign_key_edges"][0]["on_delete"], "CASCADE")

    def test_clean_additive_migration(self):
        report = run_lab(SCHEMA, "ALTER TABLE folders ADD COLUMN label TEXT;", SEED)
        self.assertEqual(report["status"], "pass")
        self.assertTrue(all(p["final_changes"] == [] for p in report["profiles"]))

    def test_numeric_boolean_pragma_spellings(self):
        for value in ("00", "0", "OFF", "FALSE", "NO", "'00'"):
            with self.subTest(value=value):
                auto, outer = run_lab("CREATE TABLE t(x);", f"PRAGMA foreign_keys={value};", "INSERT INTO t VALUES(1);")["profiles"]
                self.assertFalse(auto["final"]["foreign_keys"])
                self.assertTrue(outer["final"]["foreign_keys"])
                self.assertIn("FOREIGN_KEYS_TOGGLE_IGNORED", [x["code"] for x in outer["steps"][0]["findings"]])

    def test_explain_pragma_observes_real_engine_behavior(self):
        report = run_lab("CREATE TABLE t(x);", "EXPLAIN PRAGMA foreign_keys=OFF;", "INSERT INTO t VALUES(1);")
        for profile in report["profiles"]:
            oracle = sqlite3.connect(":memory:", isolation_level=None)
            try:
                oracle.execute("PRAGMA foreign_keys=ON")
                if profile["name"] == "outer_transaction":
                    oracle.execute("BEGIN")
                list(oracle.execute("EXPLAIN PRAGMA foreign_keys=OFF"))
                self.assertEqual(profile["steps"][0]["after"]["foreign_keys"], bool(oracle.execute("PRAGMA foreign_keys").fetchone()[0]))
            finally:
                oracle.close()

    def test_failure_rolls_back_transient_outer_loss(self):
        migration = "DELETE FROM files; INSERT INTO missing VALUES(1);"
        auto, outer = run_lab(SCHEMA, migration, SEED)["profiles"]
        self.assertEqual(rows(auto["final"])["files"], 0)
        self.assertEqual(rows(outer["steps"][0]["after"])["files"], 0)
        self.assertEqual(rows(outer["final"])["files"], 2)
        self.assertEqual(outer["final_changes"], [])
        self.assertEqual(outer["completion"], "rolled_back")

    def test_fail_trigger_keeps_partial_statement_effect_before_rollback(self):
        schema = "CREATE TABLE t(x INTEGER); CREATE TRIGGER stop BEFORE DELETE ON t WHEN OLD.x=2 BEGIN SELECT RAISE(FAIL,'PRIVATE_ERROR'); END;"
        auto, outer = run_lab(schema, "DELETE FROM t;", "INSERT INTO t VALUES(1),(2),(3);")["profiles"]
        self.assertEqual(rows(auto["steps"][0]["after"])["t"], 2)
        self.assertEqual(rows(auto["final"])["t"], 2)
        self.assertEqual(rows(outer["steps"][0]["after"])["t"], 2)
        self.assertEqual(rows(outer["final"])["t"], 3)
        self.assertNotIn("PRIVATE_ERROR", json.dumps([auto, outer]))

    def test_deferred_foreign_key_fails_at_runner_commit(self):
        schema = "CREATE TABLE p(id PRIMARY KEY); CREATE TABLE c(x REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED);"
        _, outer = run_lab(schema, "INSERT INTO c VALUES(4);", "INSERT INTO p VALUES(1);")["profiles"]
        self.assertTrue(outer["sql_completed"])
        self.assertEqual(outer["finalization"]["action"], "commit")
        self.assertEqual(outer["finalization"]["outcome"], "error")
        self.assertEqual(outer["finalization"]["rollback"], "completed")
        self.assertEqual(rows(outer["final"])["c"], 0)

    def test_explicit_begin_is_not_removed(self):
        auto, outer = run_lab("CREATE TABLE t(x);", "BEGIN; INSERT INTO t VALUES(2); COMMIT;", "INSERT INTO t VALUES(1);")["profiles"]
        self.assertTrue(auto["sql_completed"])
        self.assertFalse(outer["sql_completed"])
        self.assertEqual(outer["steps"][0]["error_code"], "SQLITE_ERROR")
        self.assertEqual(rows(outer["final"])["t"], 1)

    def test_unclosed_autocommit_transaction_rolls_back(self):
        auto, _ = run_lab("CREATE TABLE t(x);", "BEGIN; DELETE FROM t;", "INSERT INTO t VALUES(1);")["profiles"]
        self.assertEqual(auto["completion"], "rolled_back")
        self.assertEqual(rows(auto["final"])["t"], 1)
        self.assertIn("UNCLOSED_TRANSACTION", [f["code"] for f in auto["findings"]])

    def test_explicit_commit_ends_wrapper_visibly(self):
        _, outer = run_lab("CREATE TABLE t(x);", "COMMIT; DELETE FROM t;", "INSERT INTO t VALUES(1);")["profiles"]
        self.assertIn("OUTER_TRANSACTION_ENDED", [f["code"] for f in outer["steps"][0]["findings"]])
        self.assertFalse(outer["steps"][1]["before"]["in_transaction"])

    def test_savepoint_rollback_preserves_counts(self):
        report = run_lab("CREATE TABLE t(x);", "SAVEPOINT s; DELETE FROM t; ROLLBACK TO s; RELEASE s;", "INSERT INTO t VALUES(1);")
        self.assertTrue(all(rows(p["final"])["t"] == 1 for p in report["profiles"]))
        self.assertTrue(all(p["steps"][1]["changes"][0]["delta"] == -1 for p in report["profiles"]))

    def test_trigger_strings_comments_and_line_numbers(self):
        schema = "CREATE TABLE t(x); CREATE TABLE log(x);"
        migration = "-- ignored ;\n/* block ; */\nCREATE TRIGGER tr AFTER INSERT ON t BEGIN\n INSERT INTO log VALUES('a;b');\n INSERT INTO log VALUES('c');\nEND;\nINSERT INTO t VALUES('private-value');"
        for p in run_lab(schema, migration, "INSERT INTO t VALUES(1);")["profiles"]:
            self.assertEqual(len(p["steps"]), 2)
            self.assertEqual(p["steps"][0]["line"], 3)
            self.assertEqual(p["steps"][0]["kind"], "CREATE TRIGGER")
            self.assertEqual(rows(p["final"])["log"], 2)
            self.assertNotIn("private-value", json.dumps(p))

    def test_rename_table_has_absent_count_not_zero(self):
        report = run_lab("CREATE TABLE old(x);", "ALTER TABLE old RENAME TO new;", "INSERT INTO old VALUES(1);")
        changes = report["profiles"][0]["final_changes"]
        self.assertEqual(changes, [{"table": "new", "before": None, "after": 1, "delta": None, "kind": "added"}, {"table": "old", "before": 1, "after": None, "delta": None, "kind": "removed"}])

    def test_quoted_identifiers_and_no_value_echo(self):
        report = run_lab('CREATE TABLE "odd""name"(x);', 'DELETE FROM "odd""name";', 'INSERT INTO "odd""name" VALUES(\'DO_NOT_ECHO\');')
        self.assertEqual(report["profiles"][0]["final_changes"][0]["table"], 'odd"name')
        self.assertNotIn("DO_NOT_ECHO", json.dumps(report))

    def test_rejects_external_writes_temp_virtual_and_unsafe_pragmas(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "must-not-exist.db"
            quoted = str(target).replace("'", "''")
            statements = [f"ATTACH DATABASE '{quoted}' AS extra;", f"VACUUM INTO '{quoted}';", "CREATE TEMP TABLE secret(x);", "CREATE TABLE temp.secret(x);", "CREATE VIRTUAL TABLE v USING fts5(x);", "PRAGMA writable_schema=ON;", "PRAGMA journal_mode=WAL;", "PRAGMA temp_store=FILE;", "SELECT load_extension('PRIVATE_PATH');"]
            for sql in statements:
                with self.subTest(sql=sql):
                    report = run_lab("CREATE TABLE t(x);", sql, "INSERT INTO t VALUES(1);")
                    self.assertTrue(all(p["status"] == "failed" for p in report["profiles"]))
                    self.assertFalse(target.exists())

    def test_vm_budget_reports_unknown_measurement_not_zero(self):
        migration = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT x FROM n;"
        report = run_lab("CREATE TABLE t(x);", migration, "INSERT INTO t VALUES(1);", limits=Limits(max_vm_steps=5000))
        for profile in report["profiles"]:
            self.assertEqual(profile["status"], "failed")
            self.assertEqual(profile["steps"][0]["error_code"], "RESOURCE_LIMIT")
            self.assertIsNone(profile["steps"][0]["after"])
            self.assertEqual(rows(profile["final"])["t"], 1)

    def test_page_limit_and_value_limit(self):
        report = run_lab("CREATE TABLE t(x);", "INSERT INTO t VALUES(zeroblob(40000));", "INSERT INTO t VALUES('a');", limits=Limits(max_pages=4))
        self.assertTrue(all(p["status"] == "failed" for p in report["profiles"]))
        report = run_lab("CREATE TABLE t(x);", "SELECT randomblob(2048);", "INSERT INTO t VALUES('a');", limits=Limits(max_value_bytes=1024))
        self.assertTrue(all(p["status"] == "failed" for p in report["profiles"]))

    def test_report_limit_is_controlled(self):
        report = run_lab("CREATE TABLE t(x);", "SELECT 1; SELECT 2;", "INSERT INTO t VALUES(1);", limits=Limits(max_report_bytes=10))
        self.assertTrue(all(p["status"] == "failed" for p in report["profiles"]))
        self.assertIsNone(report["profiles"][0]["steps"][0]["after"])

    def test_table_limit_and_baseline_failure(self):
        with self.assertRaises(LabError):
            run_lab("CREATE TABLE a(x); CREATE TABLE b(x);", "SELECT 1;", limits=Limits(max_tables=1))
        with self.assertRaises(LabError) as error:
            run_lab("SELECT missing_private_value;", "SELECT 1;")
        self.assertNotIn("missing_private_value", str(error.exception))

    def test_limits_reject_overflow_and_invalid_types(self):
        for field in ("max_value_bytes", "max_columns", "max_statement_bytes", "max_vm_steps"):
            with self.subTest(field=field), self.assertRaises(LabError):
                Limits(**{field: 2**100})
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(LabError):
                Limits(timeout_seconds=value)

    def test_input_limits_incomplete_sql_and_wrong_types(self):
        for schema, migration, seed in (("", "SELECT 1;", None), ("CREATE TABLE t(x);", "", None), ("CREATE TABLE t(x);", "SELECT '\x00';", None), ("CREATE TABLE t(x);", "SELECT 'unfinished", None), ("CREATE TABLE t(x);", "SELECT 1;", 2)):
            with self.subTest(migration=migration), self.assertRaises(LabError):
                run_lab(schema, migration, seed)
        with self.assertRaises(LabError):
            run_lab("CREATE TABLE t(x);", "SELECT 1;", limits=Limits(max_input_bytes=4))

    def test_empty_baseline_is_not_claimed_as_data_coverage(self):
        report = run_lab("CREATE TABLE t(x);", "SELECT 1;")
        self.assertEqual(report["status"], "review")
        self.assertIn("EMPTY_BASELINE", [f["code"] for f in report["profiles"][0]["findings"]])

    def test_no_trailing_semicolon_and_only_comment_tail(self):
        report = run_lab("CREATE TABLE t(x)", "SELECT 1 -- trailing comment", "INSERT INTO t VALUES('a;''b')")
        self.assertEqual(report["status"], "pass")


if __name__ == "__main__":
    unittest.main()
