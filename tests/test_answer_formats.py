"""本地无 claim 答案与历史论文格式的兼容检查，不访问真实模型或数据。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import evaluate
from contracts.agent_output import AgentRCAOutput
from local_answer_contract import LEGACY_FORMAT, LOCAL_FORMAT, LocalAgentRCAOutput


class AnswerFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rca_answer_format_")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.output = self.base / "out"
        self.output.mkdir()
        self.case_data = self.base / "data"
        self.case_data.mkdir()
        (self.output / "run_manifest.json").write_text(
            json.dumps({"status": "completed", "has_answer": True}), encoding="utf-8")
        self.batch_row = {"status": "completed", "has_answer": True}
        self.local = {
            "root_causes": [{"service": "A", "fault_kind": "network_delay", "evidence": [
                {"kind": "trace", "sql": "SELECT 1"}]}],
            "propagation": [{"from": "A", "to": "B", "evidence": [
                {"kind": "trace", "sql": "SELECT 2"}]}],
        }
        self.legacy = json.loads(json.dumps(self.local))
        for group in self.legacy.values():
            for item in group:
                item["evidence"][0]["claim"] = "Recorded evidence"
        self.truth = {
            "system": "fixture", "applicable": True,
            "faults": [{"role": "required", "service": "a", "services": {"a"}, "kind": "network_delay"}],
            "edges": {("a", "b")}, "nodes": {"a", "b"}, "alarms": {"b"},
        }

    def grade(self, answer, fmt=None):
        (self.output / "agent_answer.json").write_text(json.dumps(answer), encoding="utf-8")
        def replay(actual, store, zero_evidence_score):
            items = [{"status": "OK", **e} for group in actual.values()
                     for item in group for e in item["evidence"]]
            return {"items": items, "sql_exec": 1.0}
        with patch.object(evaluate, "label_context", return_value=self.truth), \
                patch.object(evaluate, "ParquetStore"), patch.object(evaluate, "replay_evidence", side_effect=replay):
            args = ("case", self.output, self.case_data, self.batch_row, {})
            return evaluate.grade_case(*args) if fmt is None else evaluate.grade_case(*args, fmt)

    def test_legacy_default_and_new_format_are_both_scored(self):
        legacy = self.grade(self.legacy)
        new = self.grade(self.local, LOCAL_FORMAT)
        self.assertTrue(legacy["valid_answer"])
        self.assertTrue(new["valid_answer"])
        self.assertEqual(legacy["metrics"], new["metrics"])
        self.assertEqual(legacy["root_counts"], new["root_counts"])
        self.assertEqual(legacy["sql_query_counts"], {"OK": 2})
        self.assertEqual(new["sql_query_counts"], {"OK": 2})

    def test_old_and_new_contracts_are_strict_and_not_guessed(self):
        AgentRCAOutput.model_validate(self.legacy)
        LocalAgentRCAOutput.model_validate(self.local)
        self.assertFalse(self.grade(self.local, LEGACY_FORMAT)["valid_answer"])
        self.assertFalse(self.grade(self.legacy, LOCAL_FORMAT)["valid_answer"])
        mixed = json.loads(json.dumps(self.local))
        mixed["root_causes"][0]["evidence"][0]["claim"] = "Extra field"
        self.assertFalse(self.grade(mixed, LOCAL_FORMAT)["valid_answer"])

    def test_unknown_batch_format_fails_before_reading_cases(self):
        run = self.base / "run"
        run.mkdir()
        (run / "run_metadata.json").write_text(json.dumps({"pipeline": "hierarchical_v1", "answer_format": "unknown"}), encoding="utf-8")
        (run / "summary.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未知答案格式"):
            evaluate.evaluate(run)

    def test_batch_metadata_selects_local_and_unmarked_legacy_formats(self):
        for fmt, answer in ((LOCAL_FORMAT, self.local), (None, self.legacy)):
            with self.subTest(fmt=fmt):
                run = self.base / (fmt or "legacy")
                case = run / "case"
                case.mkdir(parents=True)
                meta = {"pipeline": "hierarchical_v1", "planned_cases": ["case"],
                        "dataset_root": str(self.base)}
                if fmt:
                    meta["answer_format"] = fmt
                (run / "run_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
                (run / "summary.json").write_text(json.dumps({"status": "completed", "cases": [
                    {"case": "case", **self.batch_row}]}), encoding="utf-8")
                (case / "run_manifest.json").write_text(json.dumps(self.batch_row), encoding="utf-8")
                (case / "agent_answer.json").write_text(json.dumps(answer), encoding="utf-8")
                with patch.object(evaluate, "label_context", return_value=self.truth), \
                        patch.object(evaluate, "ParquetStore"), \
                        patch.object(evaluate, "replay_evidence", return_value={"items": [], "sql_exec": 1.0}):
                    report = evaluate.evaluate(run)
                self.assertEqual(report["answer_format"], fmt or LEGACY_FORMAT)
                self.assertEqual(report["valid_predictions"], 1)


if __name__ == "__main__":
    unittest.main()
