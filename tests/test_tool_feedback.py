"""查询失败的元数据反馈测试；只使用临时遥测，不调用模型或网络。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_sql_evidence import write_fixture
from tools.telemetry import DataChanged, EvidenceValidationError, Telemetry


class ToolFeedbackTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="rca_feedback_")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        write_fixture(root / "data")
        self.tele = Telemetry(root / "data", root / "out")
        self.addCleanup(self.tele.close)

    def test_missing_column_preserves_error_and_uses_only_referenced_schema(self):
        args = {"sql": "SELECT body FROM abnormal_logs"}
        with patch.object(self.tele, "fetch", wraps=self.tele.fetch) as fetch:
            result = self.tele.call("query_telemetry", args)
        self.assertEqual(fetch.call_count, 1)  # 诊断不执行第二个查询，也不自动重试。
        self.assertEqual(result["status"], "technical_error")
        self.assertIn("body", result["error"])
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(list(self.tele.evdir.iterdir()), [])
        feedback = result["query_feedback"]
        self.assertTrue(feedback["metadata_only"])
        self.assertEqual(feedback["source_detection"], "validated_tables")
        self.assertEqual(list(feedback["schemas"]), ["abnormal_logs"])
        self.assertEqual(feedback["schemas"]["abnormal_logs"],
                         self.tele.catalog["tables"]["abnormal_logs"]["columns"])
        self.assertIn("message", feedback["schemas"]["abnormal_logs"])
        self.assertNotIn("body", feedback["schemas"]["abnormal_logs"])
        self.assertEqual(args, {"sql": "SELECT body FROM abnormal_logs"})

    def test_reserved_alias_failure_gets_quoting_hint_without_execution(self):
        with patch.object(self.tele, "fetch", wraps=self.tele.fetch) as fetch:
            result = self.tele.call("query_telemetry", {
                "sql": "SELECT message AS window FROM abnormal_logs GROUP BY window"})
        self.assertEqual(result["status"], "invalid_query")
        self.assertEqual(result["evidence_refs"], [])
        fetch.assert_not_called()
        feedback = result["query_feedback"]
        self.assertIn('AS "window"', " ".join(feedback["hints"]))
        self.assertEqual(feedback["source_detection"], "names_in_invalid_sql_not_validated")
        self.assertEqual(list(feedback["schemas"]), ["abnormal_logs"])

    def test_timestamp_numeric_operation_gets_unit_aware_hint(self):
        result = self.tele.call("query_telemetry", {
            "sql": "SELECT time / 1000 AS t FROM abnormal_logs"})
        self.assertEqual(result["status"], "technical_error")
        feedback = result["query_feedback"]
        self.assertEqual(feedback["schemas"]["abnormal_logs"]["time"], "timestamp[ns]")
        self.assertIn("epoch_ns/epoch_ms", " ".join(feedback["hints"]))
        self.assertEqual(result["evidence_refs"], [])

    def test_schema_count_is_bounded_and_omissions_explicit(self):
        result = self.tele.call("query_telemetry", {
            "sql": "SELECT missing FROM abnormal_logs UNION ALL "
                   "SELECT missing FROM normal_logs UNION ALL "
                   "SELECT missing FROM abnormal_traces",
            "kind": "log",
        })
        self.assertEqual(result["status"], "technical_error")
        feedback = result["query_feedback"]
        self.assertEqual(len(feedback["schemas"]), 2)
        self.assertEqual(feedback["schema_table_count"], 3)
        self.assertEqual(feedback["omitted_schema_tables"], ["normal_logs"])

    def test_bad_inspect_fields_get_schema_without_query_or_evidence(self):
        with patch.object(self.tele, "fetch") as fetch:
            result = self.tele.call("inspect_telemetry", {
                "action": "rows", "table": "abnormal_logs", "fields": ["body"]})
        fetch.assert_not_called()
        self.assertEqual(result["status"], "invalid_query")
        self.assertEqual(result["error"], "fields 必须是原始表字段的列表")
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(list(result["query_feedback"]["schemas"]), ["abnormal_logs"])

    def test_invalid_action_container_cannot_break_error_reporting(self):
        result = self.tele.call("inspect_telemetry", {
            "action": ["rows"], "table": "abnormal_logs"})
        self.assertEqual(result["status"], "invalid_query")
        self.assertIn("action", result["error"])
        self.assertIn("query_feedback", result)

    def test_success_and_no_observations_keep_existing_contract(self):
        for condition, expected in (("TRUE", "ok"), ("FALSE", "no_observations")):
            with self.subTest(condition=condition):
                result = self.tele.call("query_telemetry", {
                    "sql": f"SELECT message FROM abnormal_logs WHERE {condition}"})
                self.assertEqual(result["status"], expected)
                self.assertNotIn("query_feedback", result)
                self.assertEqual(len(result["evidence_refs"]), 1)

    def test_stale_data_and_replay_failures_do_not_get_schema_feedback(self):
        with patch.object(self.tele, "_fresh", side_effect=DataChanged("changed")):
            result = self.tele.call("query_telemetry", {"sql": "SELECT body FROM abnormal_logs"})
        self.assertEqual(result, {"status": "stale_data", "error": "changed", "evidence_refs": []})
        with patch.object(self.tele, "verify", side_effect=EvidenceValidationError("E-test", "bad SQL")):
            result = self.tele.call("replay_evidence", {"evidence_ref": "E-test"})
        self.assertEqual(result["status"], "technical_error")
        self.assertEqual(result["error_type"], "evidence_sql_validation_failed")
        self.assertNotIn("query_feedback", result)

    def test_diagnostic_names_ignore_comments_literals_and_external_paths(self):
        sql = "SELECT 'FROM normal_logs' AS x FROM abnormal_logs -- JOIN normal_traces\n"
        self.assertEqual(self.tele._diagnostic_tables(sql), ["abnormal_logs"])
        self.assertEqual(self.tele._diagnostic_tables(
            "SELECT bad FROM read_parquet('abnormal_logs.parquet')"), ["abnormal_logs"])
        self.assertEqual(self.tele._diagnostic_tables(
            "SELECT bad FROM 'abnormal_logs.parquet'"), [])
        self.assertEqual(self.tele._diagnostic_tables(
            "SELECT bad FROM read_parquet('outside/abnormal_logs.parquet')"), [])
        self.assertEqual(self.tele._diagnostic_tables(
            'SELECT bad FROM "abnormal_logs.parquet"'), ["abnormal_logs"])

    def test_other_validation_failures_remain_unchanged(self):
        result = self.tele.call("query_telemetry", {"sql": "DELETE FROM abnormal_logs"})
        self.assertEqual(result["status"], "invalid_query")
        self.assertEqual(result["evidence_refs"], [])
        self.assertNotIn("query_feedback", result)
        result = self.tele.call("query_telemetry", {"sql": "SELECT * FROM unknown_table"})
        self.assertNotIn("query_feedback", result)


if __name__ == "__main__":
    unittest.main()
