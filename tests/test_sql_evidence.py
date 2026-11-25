"""SQL 转换、证据复核与 L2 失败交接的本地测试，不调用真实模型。

使用临时目录中的十张小型遥测表；所有测试输出随目录清理，不写入 Result。
运行：python -B -m unittest discover -s tests -v
"""

import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pyarrow as pa
import pyarrow.parquet as pq
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents import l1, meta
import state
from agents.loop import abnormal_refs
from settings import CONFIG
from tools.telemetry import DataChanged, EvidenceValidationError, TABLES, Telemetry
from workflow import Incident


def write_fixture(directory):
    """客户端与服务端分别属于 search/rate，含正、零、负的纳秒时间差。"""
    directory.mkdir()
    for table in TABLES:
        base = 1_700_000_000_000_000_000 + (60_000_000_000 if table.startswith("abnormal") else 0)
        if table.endswith("_traces"):
            rows = []
            for i, gap in enumerate((137, 0, -250)):
                for name, span, parent, offset, kind in (
                    ("search", f"c{i}", "", 0, "Client"),
                    ("rate", f"s{i}", f"c{i}", gap, "Server"),
                ):
                    rows.append({"service_name": name, "trace_id": f"t{i}", "span_id": span,
                                 "parent_span_id": parent, "time": base + i * 1_000_000_000 + offset,
                                 "duration": 1_000_000, "span_name": "rate.Rate/GetRates",
                                 "attr.span_kind": kind})
        else:
            rows = []
            for i in range(8):
                row = {"service_name": "rate", "time": base + i * 1_000_000_000}
                if table.endswith("_logs"):
                    row.update(message=f"request failed {i}", level="error", trace_id=f"t{i}")
                else:
                    row.update(metric="requests_total", value=float(i + 1), count=i + 1, sum=2.0 * (i + 1))
                rows.append(row)
        data = pa.Table.from_pylist(rows)
        data = data.set_column(data.schema.get_field_index("time"), "time", data["time"].cast(pa.timestamp("ns")))
        pq.write_table(data, directory / f"{table}.parquet")


def conclusion_reply(chains, reason="固定输入已完成仲裁，保留未决问题供综合判断"):
    """V004 的 Meta 只结案，根因选择由后续独立 Synthesis 完成。"""
    return {"action": "conclude", "branch_assessments": [
        {"chain_id": c["chain_id"], "revision": c["revision"], "assessment": reason}
        for c in chains], "confidence_scores": [], "open_issues": [], "termination_reason": reason}


def synthesis_reply(chain, *, revision=None, root=None):
    """为当前正式综合协议提供明确来源和完整证据的固定答案。"""
    claim = root or chain["root"]
    return {"root_causes": [{"service": claim["service"], "fault_kind": claim["fault_kind"],
             "evidence_refs": list(claim["evidence_refs"]), "source_chain_id": chain["chain_id"],
             "source_revision": chain["revision"] if revision is None else revision,
             "reason": "采用本版已核查根因，未证实的传播边不加入答案"}],
            "propagation": [], "reason": "固定综合结果保留证据来源及未完成调查"}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="rca_sql_test_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        write_fixture(self.root / "data")
        self.tele = Telemetry(self.root / "data", self.root / "out")
        self.addCleanup(self.tele.close)
        self.case = SimpleNamespace(rt=SimpleNamespace(tele=self.tele))

    def query(self, sql, limit=10):
        result = self.tele.call("query_telemetry", {"sql": sql, "limit": limit})
        self.assertEqual(result["status"], "ok", result)
        return result

    def bad_evidence(self):
        ref = self.query("SELECT time FROM abnormal_traces")["evidence_refs"][0]
        path = self.tele.evdir / f"{ref}.json"
        record = self.tele.evidence(ref)
        record["sql"] = "SELECT unsupported_function(time) FROM abnormal_traces"
        path.write_text(json.dumps(record), encoding="utf-8")
        return ref

    def test_nanoseconds_convert_save_and_replay(self):
        sql = """SELECT c.span_id, date_diff('nanosecond', c.time, s.time) AS gap
                 FROM abnormal_traces c JOIN abnormal_traces s
                 ON c.trace_id=s.trace_id AND c.span_id=s.parent_span_id ORDER BY c.span_id"""
        converted, tables = self.tele.validate(sql)
        self.assertIn("EPOCH_NS", converted)
        self.assertEqual(tables, {"abnormal_traces"})
        again, again_tables = self.tele.validate(converted)
        self.assertEqual(again_tables, tables)
        self.assertEqual(self.tele.fetch(again, 10), self.tele.fetch(converted, 10))
        result = self.query(sql)
        self.assertEqual([r["gap"] for r in result["rows"]], [137, 0, -250])
        ref = result["evidence_refs"][0]
        self.assertTrue(abnormal_refs(self.case, [ref]))
        replay = self.tele.verify(ref)
        self.assertEqual(replay["rows"], result["rows"])
        self.assertEqual(replay["sql"], self.tele.evidence(ref)["sql"])

    def test_generated_sql_is_checked_before_execution(self):
        # 模拟转换库输出未授权函数；必须在 fetch/保存前拒绝，而不是等 L2 再发现。
        with patch.object(exp.Select, "sql", return_value="SELECT unsupported_function(time) FROM abnormal_traces"), \
                patch.object(self.tele, "fetch") as fetch:
            result = self.tele.call("query_telemetry", {"sql": "SELECT time FROM abnormal_traces"})
        self.assertEqual(result["status"], "invalid_query")
        fetch.assert_not_called()
        self.assertEqual(list(self.tele.evdir.iterdir()), [])

    def test_readonly_restrictions_and_parameter_binding(self):
        for sql in (
            "DELETE FROM abnormal_traces", "SELECT * FROM abnormal_traces; SELECT * FROM normal_traces",
            "SELECT * FROM read_parquet('outside.parquet')", "SELECT * FROM information_schema.tables",
            "SELECT unsupported_function(time) FROM abnormal_traces",
            "SELECT * FROM abnormal_traces UNION ALL SELECT * FROM unrelated_table",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.tele.validate(sql)
        sql, tables = self.tele.validate(
            "SELECT ? AS outer_value FROM read_parquet('abnormal_traces.parquet') "
            "WHERE service_name IN (SELECT ? FROM normal_traces)", ["a'; DROP TABLE x;--", "rate"])
        self.assertEqual(tables, {"normal_traces", "abnormal_traces"})
        self.assertEqual(self.tele.fetch(sql, 10)[0][0]["outer_value"], "a'; DROP TABLE x;--")
        with self.assertRaisesRegex(ValueError, "参数数量"):
            self.tele.validate("SELECT ? FROM abnormal_traces")

    def test_record_checks_without_rewriting_or_executing(self):
        sql = "select time from abnormal_traces limit 1"
        with patch.object(self.tele, "fetch") as fetch:
            ref = self.tele.record("trace", sql, [{"time": "fixture"}], False, {})
        fetch.assert_not_called()
        self.assertEqual(self.tele.evidence(ref)["sql"], sql)
        before = list(self.tele.evdir.iterdir())
        with self.assertRaises(ValueError):
            self.tele.record("trace", "SELECT unsupported_function(time) FROM abnormal_traces", [], False, {})
        self.assertEqual(list(self.tele.evdir.iterdir()), before)

    def test_limit_and_truncation_are_preserved(self):
        result = self.query("SELECT span_id FROM abnormal_traces ORDER BY span_id", limit=1)
        ref = result["evidence_refs"][0]
        self.assertEqual(len(result["rows"]), 1)
        self.assertTrue(result["truncated"])
        self.assertTrue(self.tele.evidence(ref)["sql"].endswith("LIMIT 1"))
        replay = self.tele.verify(ref)
        self.assertEqual(replay["rows"], result["rows"])
        self.assertTrue(replay["truncated"])

    def test_normal_evidence_is_false_but_bad_evidence_raises(self):
        normal = self.query("SELECT 'abnormal_traces' AS label FROM normal_traces")["evidence_refs"][0]
        self.assertFalse(abnormal_refs(self.case, [normal]))
        ref = self.bad_evidence()
        with self.assertRaises(EvidenceValidationError) as caught:
            abnormal_refs(self.case, [ref])
        self.assertEqual(caught.exception.evidence_ref, ref)
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        with self.assertRaises(EvidenceValidationError):
            self.tele.verify(ref)
        replay = self.tele.call("replay_evidence", {"evidence_ref": ref})
        self.assertEqual(replay["status"], "technical_error")
        self.assertEqual(replay["evidence_ref"], ref)

    def test_parse_failure_is_classified_but_data_change_is_not_hidden(self):
        ref = self.query("SELECT time FROM abnormal_traces")["evidence_refs"][0]
        with patch.object(self.tele, "evidence", return_value={"sql": "SELECT ( FROM abnormal_traces"}):
            with self.assertRaises(EvidenceValidationError):
                self.tele.validated_evidence(ref)
        with patch.object(self.tele, "_fresh", side_effect=DataChanged("fixture data changed")):
            with self.assertRaises(DataChanged):
                self.tele.validated_evidence(ref)
        with patch.object(self.tele, "validate", side_effect=RuntimeError("unexpected defect")):
            with self.assertRaisesRegex(RuntimeError, "unexpected defect"):
                self.tele.validated_evidence(ref)

    def test_builtin_metric_log_and_trace_evidence_remains_replayable(self):
        calls = [
            ("inspect_telemetry", {"action": "rows", "table": "abnormal_traces"}),
            ("analyze_metrics", {"operation": "rank"}),
            ("analyze_metrics", {"operation": "compare", "source": "metrics_sum"}),
            ("analyze_metrics", {"operation": "series", "source": "metrics_histogram",
                                 "component": "rate", "metric": "requests_total"}),
            ("analyze_logs", {"operation": "search"}),
            ("analyze_logs", {"operation": "compare"}),
            ("query_graph", {"component": "rate", "window": "both"}),
            ("analyze_traces", {"operation": "operations"}),
            ("analyze_traces", {"operation": "pairs"}),
            ("analyze_traces", {"operation": "request", "trace_id": "t0"}),
        ]
        for name, args in calls:
            with self.subTest(tool=name, args=args):
                result = self.tele.call(name, args)
                self.assertEqual(result["status"], "ok", result)
                self.assertTrue(result["evidence_refs"])
                for ref in result["evidence_refs"]:
                    replay = self.tele.verify(ref)
                    self.assertEqual(replay["rows"], self.tele.evidence(ref)["rows"])

    def test_trace_both_windows_preserves_single_window_and_replay(self):
        for operation in ("operations", "pairs"):
            with self.subTest(operation=operation):
                result = self.tele.call("analyze_traces", {"operation": operation, "window": "both"})
                self.assertEqual(result["status"], "ok", result)
                self.assertEqual(set(result["windows"]), {"normal", "abnormal"})
                self.assertEqual({r["window"] for r in result["rows"]}, {"normal", "abnormal"})
                for window in ("normal", "abnormal"):
                    refs = result["windows"][window]["evidence_refs"]
                    self.assertTrue(refs)
                    for ref in refs:
                        self.assertTrue(self.tele.verify(ref)["rows"])
                self.assertEqual(len(result["evidence_refs"]), 2)
        bad = self.tele.call("analyze_traces", {"operation": "request", "window": "both", "trace_id": "t0"})
        self.assertNotEqual(bad["status"], "ok")


class HandoffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="rca_sql_handoff_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        write_fixture(self.root / "data")
        model = Mock()
        model.call.side_effect = AssertionError("测试未提供固定回复，禁止调用真实模型")
        self.case = Incident(self.root / "data", self.root / "out", model, CONFIG, "fixture")
        self.addCleanup(self.case.rt.tele.close)
        self.tele = self.case.rt.tele
        result = self.tele.call("query_telemetry", {"sql": "SELECT time FROM abnormal_traces"})
        self.ref = result["evidence_refs"][0]
        self.chain = self.make_chain("S01")

    def make_chain(self, seed_id):
        return l1.make_chain(self.case, {"seed_id": seed_id, "service": "rate", "round": 1,
                                        "evidence_refs": [self.ref]}, 1)

    def audit_action(self, ref):
        return {"action": "audit", "reason": "固定输入检查审核交接", "edge_verdicts": [],
                "component_verdict": {"service": "rate", "verdict": "support", "reason": "固定输入", "evidence_refs": [ref]},
                "fault_kind_verdict": {"fault_kind": "network_delay", "verdict": "support", "reason": "固定输入", "evidence_refs": [ref]},
                "supported_prefix": ["rate"], "score": 0.82, "doubts": [], "recommendation": "keep"}

    def send_review(self, c, ref):
        root = {"service": "rate", "fault_kind": "network_delay", "evidence_refs": [ref]}
        walker, reviewer = self.case.walkers[c["chain_id"]], self.case.reviewers[c["chain_id"]]
        walker.seen.add(ref)
        with patch.object(reviewer, "step", return_value=self.audit_action(ref)):
            self.case.review_dispatch(c, {"action": "review", "reason": "固定输入送审", "root": root,
                "fault_diagnosis": {"observed_phenomenon": "异常", "candidates": [],
                                    "missing_evidence": "待核查", "next_check": "对照"}}, walker)

    def corrupt_saved_sql(self):
        record = self.tele.evidence(self.ref)
        record["sql"] = "SELECT unsupported_function(time) FROM abnormal_traces"
        (self.tele.evdir / f"{self.ref}.json").write_text(json.dumps(record), encoding="utf-8")

    def test_l2_failure_returns_to_l1_and_other_chain_can_complete(self):
        sibling = self.make_chain("S02")
        before = copy.deepcopy(sibling)
        self.corrupt_saved_sql()
        self.send_review(self.chain, self.ref)
        failed = self.chain["last_review"]
        self.assertEqual(failed["status"], "technical_failure")
        self.assertIsNone(failed["score"])
        self.assertEqual(failed["evidence_ref"], self.ref)
        self.assertEqual(self.chain["status"], "exploring")
        self.assertEqual(sibling, before)
        self.assertNotIn("audit", self.case.rt.budget.holds[self.chain["chain_id"]])
        self.assertEqual(self.case.reviewers[self.chain["chain_id"]].bad_actions, 0)
        self.assertEqual(self.case.investigation_stops[0]["error_type"], "evidence_sql_validation_failed")

        walker = self.case.walkers[self.chain["chain_id"]]
        with patch.object(walker, "step", return_value={"action": "stop", "reason": "技术问题暂不能补查"}) as step:
            l1.walk_step(self.case, self.chain)
        self.assertEqual(step.call_args.args[0]["last_review"], failed)
        self.assertEqual(self.chain["status"], "technical_failure")

        good = self.tele.call("query_telemetry", {"sql": "SELECT span_id FROM abnormal_traces"})["evidence_refs"][0]
        self.send_review(sibling, good)
        with patch.object(self.case.walkers[sibling["chain_id"]], "step",
                          return_value={"action": "submit", "reason": "采用有效审核"}):
            l1.walk_step(self.case, sibling)
        self.assertEqual(sibling["status"], "submitted")
        self.case.investigate = Mock()
        self.case.rt.model.call.side_effect = [
            conclusion_reply(self.case.ordered(), "一支审核失败，综合只采用有效分支"), synthesis_reply(sibling)]
        report = self.case.run()
        self.assertEqual(report["status"], "partial")
        self.assertIsNotNone(report["answer"])
        self.assertTrue((self.case.out / "agent_answer.json").is_file())
        self.assertEqual(self.case.rt.model.call.call_count, 2)  # Meta 结案 + 独立综合。
        self.assertNotIn("claim", report["answer"]["root_causes"][0]["evidence"][0])

    def test_replacement_evidence_allows_new_review_and_resolves_stop(self):
        self.corrupt_saved_sql()
        self.send_review(self.chain, self.ref)
        good = self.tele.call("query_telemetry", {"sql": "SELECT span_id FROM abnormal_traces"})["evidence_refs"][0]
        self.send_review(self.chain, good)
        self.assertEqual(self.chain["last_review"]["status"], "reviewed")
        self.assertEqual(self.chain["last_review"]["score"], 0.82)
        self.assertEqual(len(self.chain["reviews"]), 2)
        self.assertTrue(self.case.investigation_stops[0]["resolved"])

    def test_meta_failure_is_preserved_and_synthesis_cannot_bypass_bad_evidence(self):
        self.send_review(self.chain, self.ref)
        self.chain["status"] = "submitted"
        self.corrupt_saved_sql()
        self.case.investigate = Mock()
        # 在 Meta 阶段触发真实的已存 SQL 校验错误，检查调度器的异常交接。
        # 普通 replay_evidence 工具失败是返回给模型的结果，不能假设它自动终止 Meta。
        self.case.rt.model.call.side_effect = [synthesis_reply(self.chain)]
        with patch.object(self.case.meta, "step", side_effect=lambda *args, **kwargs:
                          self.tele.validated_evidence(self.ref)):
            report = self.case.run()
        self.assertEqual(report["status"], "technical_failure")
        self.assertEqual(report["failure_stage"], "synthesis")
        self.assertEqual(report["meta_failure"]["error_type"], "evidence_sql_validation_failed")
        self.assertEqual(report["meta_failure"]["evidence_ref"], self.ref)
        self.assertEqual(report["evidence_ref"], self.ref)
        self.assertIsNone(report["answer"])
        saved = json.loads((self.case.out / "stages/case_summary.json").read_text("utf-8"))
        self.assertFalse(saved["has_answer"])
        self.assertEqual(saved["error_type"], "evidence_sql_validation_failed")
        self.assertEqual(self.case.rt.model.call.call_count, 1)  # 综合生成一次后立即拒绝坏证据。

    def test_synthesis_evidence_validation_failure_is_not_a_model_retry(self):
        self.send_review(self.chain, self.ref)
        self.chain["status"] = "submitted"
        self.corrupt_saved_sql()
        self.case.investigate = Mock()
        self.case.rt.model.call.side_effect = [synthesis_reply(self.chain)]
        with patch.object(meta, "arbitrate", return_value=conclusion_reply([self.chain])):
            report = self.case.run()
        self.assertEqual(report["failure_stage"], "synthesis")
        self.assertEqual(report["status"], "technical_failure")
        self.assertEqual(report["evidence_ref"], self.ref)
        self.assertEqual(self.case.rt.model.call.call_count, 1)  # 仅一次生成，没有把程序坏证据交回模型纠错。
        self.assertFalse((self.case.out / "agent_answer.json").exists())
        self.assertFalse((self.case.out / "prediction.json").exists())

    def test_exhausted_review_explicit_checkpoint_submission_uses_old_version(self):
        self.case.cfg = replace(CONFIG, reviews_per_chain=1)
        self.send_review(self.chain, self.ref)
        old_revision = self.chain["revision"]
        checkpoint = copy.deepcopy(self.chain["reviewed_checkpoint"])
        self.assertEqual(checkpoint["review"]["revision"], old_revision)
        state.change(self.case, self.chain, nodes=["rate", "search"], edges=[
            {"from": "rate", "to": "search", "evidence_refs": [self.ref]}])
        walker = self.case.walkers[self.chain["chain_id"]]
        action = {"action": "submit", "use_reviewed_version": True,
                  "confirm_no_contradiction": True, "reason": "未发现反证，保留已审核根因；新增边未审核"}
        with patch.object(walker, "step", return_value=action):
            l1.walk_step(self.case, self.chain)
        self.assertEqual(self.chain["revision"], old_revision + 1)
        self.assertEqual(self.chain["status"], "submitted")
        candidate = {"action": "select", "selected_chain_ids": [self.chain["chain_id"]],
                     "root_causes": [checkpoint["chain"]["root"]], "propagation": [], "reason": "已审核根因"}
        self.assertTrue(meta.check_selection(self.case, candidate)["valid"])
        bad = {**candidate, "propagation": [{"from": "rate", "to": "search", "evidence_refs": [self.ref]}]}
        self.assertFalse(meta.check_selection(self.case, bad)["valid"])
        self.case.investigate = Mock()
        self.case.rt.model.call.side_effect = [synthesis_reply(
            self.chain, revision=old_revision, root=checkpoint["chain"]["root"])]
        with patch.object(meta, "arbitrate", return_value=conclusion_reply([self.chain])):
            report = self.case.run()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["answer"]["root_causes"][0]["service"], "rate")
        self.assertEqual(report["answer"]["propagation"], [])
        self.assertEqual(report["prediction"]["selected_chains"][0]["revision"], old_revision)
        # 综合投影保存实际采用的旧版；未审核新版仍留在链状态和 Meta 交接记录。
        self.assertEqual(meta.meta_chains(self.case)[0]["unreviewed_revision"], old_revision + 1)
        self.assertEqual(report["prediction"]["selected_chains"][0]["edges"], [])

    def test_exhausted_review_blocks_change_but_allows_current_submission(self):
        self.case.cfg = replace(CONFIG, reviews_per_chain=1)
        self.send_review(self.chain, self.ref)
        walker = self.case.walkers[self.chain["chain_id"]]
        self.chain["target_symptom"] = {"service": "rate", "description": "测试症状", "evidence_refs": [self.ref]}
        walker.neighbors[tuple(sorted(("rate", "search")))] = [{"window": "abnormal", "evidence_refs": [self.ref]}]
        actions = [{"action": "extend", "edge": {"from": "rate", "to": "search", "evidence_refs": [self.ref]}, "reason": "尝试补边"},
                   {"action": "submit", "confirm_no_contradiction": True, "reason": "保留当前审核结果，新边未审核且未推翻根因"}]
        old_revision = self.chain["revision"]
        with patch.object(walker, "step", side_effect=actions):
            l1.walk_step(self.case, self.chain)
        self.assertEqual(self.chain["revision"], old_revision)
        self.assertEqual(self.chain["status"], "submitted")
        self.assertTrue(self.chain["review_limit_blocked"])
        self.assertEqual(self.chain["unreviewed_proposals"][0]["action"]["edge"]["to"], "search")
        self.assertEqual(self.chain["nodes"], ["rate"])

    def test_failed_review_cannot_use_previous_checkpoint(self):
        self.case.cfg = replace(CONFIG, reviews_per_chain=2)
        self.send_review(self.chain, self.ref)
        good = self.tele.call("query_telemetry", {"sql": "SELECT span_id FROM abnormal_traces"})["evidence_refs"][0]
        self.case.walkers[self.chain["chain_id"]].seen.add(good)
        self.corrupt_saved_sql()
        self.send_review(self.chain, self.ref)
        self.assertEqual(self.chain["last_review"]["status"], "technical_failure")
        walker = self.case.walkers[self.chain["chain_id"]]
        actions = [{"action": "submit", "use_reviewed_version": True,
                    "confirm_no_contradiction": True, "reason": "尝试提交旧版"},
                   {"action": "stop", "reason": "审核技术失败"}]
        with patch.object(walker, "step", side_effect=actions):
            l1.walk_step(self.case, self.chain)
        self.assertEqual(self.chain["status"], "technical_failure")
        self.assertIsNone(self.chain["submitted_checkpoint"])

    def test_checkpoint_requires_explicit_confirmation_and_can_stop(self):
        self.case.cfg = replace(CONFIG, reviews_per_chain=1)
        self.send_review(self.chain, self.ref)
        state.change(self.case, self.chain, nodes=["rate", "search"], edges=[
            {"from": "rate", "to": "search", "evidence_refs": [self.ref]}])
        walker = self.case.walkers[self.chain["chain_id"]]
        actions = [{"action": "submit", "use_reviewed_version": True,
                    "confirm_no_contradiction": False, "reason": "发现可能的反证"},
                   {"action": "stop", "reason": "新证据可能推翻旧判断"}]
        with patch.object(walker, "step", side_effect=actions):
            l1.walk_step(self.case, self.chain)
        self.assertEqual(self.chain["status"], "insufficient_evidence")
        self.assertIsNone(self.chain["submitted_checkpoint"])
        self.assertEqual(meta.selection_options(self.case), {"root_causes": {}, "propagation": {}})


if __name__ == "__main__":
    unittest.main()
