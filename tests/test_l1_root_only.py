"""固定回复检查单节点根因与传播结束的交接，不调用真实模型。"""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import state
from agents import l1, meta
from agents.loop import Invalid
from settings import CONFIG
from state import Limit
from test_sql_evidence import conclusion_reply, synthesis_reply, write_fixture
from workflow import Incident


class RootOnlyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="l1_root_only_")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        write_fixture(self.directory / "data")
        model = Mock()
        model.call.side_effect = AssertionError("只允许固定测试回复，禁止真实模型调用")
        self.case = Incident(self.directory / "data", self.directory / "out", model, CONFIG, "fixture")
        self.addCleanup(self.case.rt.tele.close)
        result = self.case.rt.tele.call("query_telemetry", {"sql": "SELECT time FROM abnormal_traces"})
        self.ref = result["evidence_refs"][0]
        self.chain = l1.make_chain(self.case, {"seed_id": "S01", "service": "rate", "round": 1,
                                              "evidence_refs": [self.ref]}, 1)
        self.walker = self.case.walkers[self.chain["chain_id"]]
        self.case.rt.budget.reserve(self.chain["chain_id"])

    def review(self, verdict="support"):
        reply = {"action": "audit", "reason": "固定审核", "edge_verdicts": [],
                 "component_verdict": {"service": "rate", "verdict": verdict, "reason": "固定判断",
                                       "evidence_refs": [self.ref] if verdict != "unknown" else []},
                 "fault_kind_verdict": {"fault_kind": "mem_stress", "verdict": verdict, "reason": "固定判断",
                                        "evidence_refs": [self.ref] if verdict != "unknown" else []},
                 "supported_prefix": ["rate"], "score": 0.8, "doubts": [], "recommendation": "keep"}
        root = {"service": "rate", "fault_kind": "mem_stress", "evidence_refs": [self.ref]}
        reviewer = self.case.reviewers[self.chain["chain_id"]]
        with patch.object(reviewer, "step", return_value=reply):
            self.case.review_dispatch(self.chain, {"action": "review", "root": root, "reason": "审核根因",
                "fault_diagnosis": {"observed_phenomenon": "异常", "candidates": [],
                                    "missing_evidence": "尚需核查", "next_check": "对照指标"}}, self.walker)

    @staticmethod
    def stop(reason="没有找到可确认的传播边"):
        return {"action": "stop", "disposition": "insufficient_evidence", "reason": reason}

    def events(self):
        return [json.loads(line) for line in (self.case.out / "stages/events.jsonl").read_text("utf-8").splitlines()]

    def test_unknown_fault_keeps_candidate_component_through_l2(self):
        root = {"service": "rate", "fault_kind": None, "evidence_refs": [self.ref]}
        diagnosis = {"observed_phenomenon": "调用异常", "candidates": [
            {"fault_kind": "network_delay", "status": "needs_check", "reason": "待区分", "evidence_refs": []}],
            "missing_evidence": "缺少网络观测", "next_check": "检查可用字段"}
        reply = {"action": "audit", "reason": "仍需调查", "edge_verdicts": [],
                 "component_verdict": {"service": "rate", "verdict": "support", "reason": "组件线索",
                                       "evidence_refs": [self.ref]},
                 "fault_kind_verdict": {"fault_kind": None, "verdict": "unknown", "reason": "类型未提交",
                                        "evidence_refs": []},
                 "supported_prefix": [], "score": 0.3, "doubts": ["类型未知"], "recommendation": "query"}
        reviewer = self.case.reviewers[self.chain["chain_id"]]
        with patch.object(reviewer, "step", return_value=reply) as step:
            self.case.review_dispatch(self.chain, {"action": "review", "root": root,
                                                   "fault_diagnosis": diagnosis, "reason": "审核"}, self.walker)
        self.assertEqual(self.chain["root"]["service"], "rate")
        self.assertIsNone(self.chain["root"]["fault_kind"])
        self.assertEqual(step.call_args.args[0]["chain"]["fault_diagnosis"], diagnosis)
        self.assertEqual(self.chain["last_review"]["component_verdict"]["verdict"], "support")
        self.assertEqual(self.chain["last_review"]["fault_kind_verdict"]["verdict"], "unknown")
        self.assertNotIn("root_verdict", self.chain["last_review"])
        self.chain["status"] = "submitted"
        self.assertEqual(meta.selection_options(self.case)["root_causes"], {})

    def test_candidate_evidence_validation_and_null_kind(self):
        root = l1.root_claim(self.case, {"service": "rate", "fault_kind": None,
                                       "evidence_refs": [self.ref]}, self.walker, "rate")
        self.assertIsNone(root["fault_kind"])
        invalid = {"observed_phenomenon": "异常", "candidates": [
            {"fault_kind": "network_delay", "status": "plausible", "reason": "声称支持", "evidence_refs": []}],
            "missing_evidence": "待查", "next_check": "对照"}
        with self.assertRaises(Invalid):
            l1.checked_fault_diagnosis(self.case, invalid, self.walker)

    def test_reminder_then_submit_reaches_meta_and_program_assembly(self):
        self.review()
        sibling = l1.make_chain(self.case, {"seed_id": "S02", "service": "rate", "round": 1,
                                           "evidence_refs": [self.ref]}, 1)
        sibling_before = copy.deepcopy(sibling)
        self.case.rt.model.call.side_effect = [self.stop(),
            {"action": "submit", "reason": "根因仍有支持，未发现可确认的传播边"}]
        l1.walk_step(self.case, self.chain)
        self.assertEqual(self.chain["status"], "submitted")
        self.assertEqual(self.chain["edges"], [])
        self.assertEqual(self.chain["root"]["evidence_refs"], [self.ref])
        self.assertEqual(sibling, sibling_before)
        self.assertEqual(self.case.rt.model.call.call_count, 2)
        self.assertIn("root_stop_confirmation", self.case.rt.model.call.call_args.args[1])
        self.assertEqual(len([e for e in self.events() if e["event"] == "root_stop_confirmation_requested"]), 1)

        options = meta.selection_options(self.case)
        self.assertIn(("rate", "mem_stress"), options["root_causes"])
        self.assertEqual(options["propagation"], {})
        self.case.investigate = Mock()
        self.case.rt.model.call.side_effect = [conclusion_reply(self.case.ordered()), synthesis_reply(self.chain)]
        report = self.case.run()
        self.assertEqual(report["answer"]["root_causes"][0]["service"], "rate")
        self.assertEqual(report["answer"]["root_causes"][0]["fault_kind"], "mem_stress")
        self.assertEqual(report["answer"]["propagation"], [])
        self.assertEqual(report["prediction"]["root_causes"][0]["evidence_ids"], [self.ref])
        self.assertTrue((self.case.out / "agent_answer.json").is_file())

    def test_explicit_second_stop_is_respected_once(self):
        self.review()
        self.case.rt.model.call.side_effect = [self.stop(), self.stop("新增证据使根因无法保留")]
        l1.walk_step(self.case, self.chain)
        self.assertEqual(self.chain["status"], "insufficient_evidence")
        self.assertIn("无法保留", self.chain["reason"])
        self.assertEqual(self.case.rt.model.call.call_count, 2)
        self.assertEqual(meta.selection_options(self.case), {"root_causes": {}, "propagation": {}})

    def test_reminder_can_request_additional_candidates(self):
        self.review()
        with patch.object(self.walker, "step", side_effect=[self.stop(),
                {"action": "reseed", "suggested_service": "rate",
                 "evidence_refs": [self.ref], "reason": "继续寻找其他根因候选"}]) as step:
            l1.walk_step(self.case, self.chain)
        self.assertEqual(step.call_count, 2)
        self.assertEqual(self.chain["status"], "reseed_requested")
        self.assertEqual(self.chain["reseed_request"]["evidence_refs"], [self.ref])

    def test_missing_root_does_not_trigger(self):
        self.review()
        self.chain["root"] = None
        with patch.object(self.walker, "step", return_value=self.stop()) as step:
            l1.walk_step(self.case, self.chain)
        self.assertEqual(step.call_count, 1)
        self.assertEqual(self.chain["status"], "insufficient_evidence")

    def test_new_evidence_is_identified_without_automatic_root_submission(self):
        self.review()
        new_ref = self.case.rt.tele.call("query_telemetry", {"sql": "SELECT span_id FROM abnormal_traces"})["evidence_refs"][0]
        self.walker.seen.add(new_ref)
        captured = []
        def reply(ctx, actions, **kwargs):
            captured.append(copy.deepcopy(ctx))
            return self.stop() if len(captured) == 1 else self.stop("新证据仍需核实，暂不保留根因")
        with patch.object(self.walker, "step", side_effect=reply):
            l1.walk_step(self.case, self.chain)
        self.assertEqual(captured[1]["root_stop_confirmation"]["new_evidence_refs"], [new_ref])
        self.assertEqual(self.chain["status"], "insufficient_evidence")
        self.assertEqual(self.chain["last_review"]["component_verdict"]["verdict"], "support")

    def test_unsupported_contradicted_and_review_failure_are_not_reminded(self):
        for index, verdict in enumerate(("unknown", "contradict")):
            with self.subTest(verdict=verdict):
                if index:
                    new_ref = self.case.rt.tele.call("query_telemetry", {"sql": "SELECT span_id FROM abnormal_traces"})["evidence_refs"][0]
                    self.walker.seen.add(new_ref)
                    self.assertTrue(self.case.rt.budget.reserve(self.chain["chain_id"]))
                self.review(verdict)
                with patch.object(self.walker, "step", return_value=self.stop()) as step:
                    l1.walk_step(self.case, self.chain)
                self.assertEqual(step.call_count, 1)
                self.assertEqual(self.chain["status"], "insufficient_evidence")
                self.chain["status"] = "exploring"
        self.chain["last_review"] = {"status": "technical_failure", "revision": self.chain["revision"], "review_id": "R"}
        with patch.object(self.walker, "step", return_value=self.stop()) as step:
            l1.walk_step(self.case, self.chain)
        self.assertEqual(step.call_count, 1)
        self.assertEqual(self.chain["status"], "technical_failure")

    def test_stale_review_and_existing_edge_do_not_trigger(self):
        self.review()
        old = copy.deepcopy(self.chain["last_review"])
        state.change(self.case, self.chain, nodes=["rate", "search"], edges=[
            {"from": "rate", "to": "search", "evidence_refs": [self.ref]}])
        self.chain["last_review"] = old
        with patch.object(self.walker, "step", return_value=self.stop()) as step:
            l1.walk_step(self.case, self.chain)
        self.assertEqual(step.call_count, 1)
        self.assertEqual(self.chain["status"], "insufficient_evidence")
        self.assertFalse(any(e["event"] == "root_stop_confirmation_requested" for e in self.events()))

    def test_stale_review_alone_does_not_trigger(self):
        self.review()
        self.chain["revision"] += 1
        with patch.object(self.walker, "step", return_value=self.stop()) as step:
            l1.walk_step(self.case, self.chain)
        self.assertEqual(step.call_count, 1)
        self.assertEqual(self.chain["status"], "insufficient_evidence")

    def test_current_review_with_edge_does_not_trigger(self):
        self.review()
        self.chain["edges"] = [{"from": "rate", "to": "search", "evidence_refs": [self.ref]}]
        with patch.object(self.walker, "step", return_value=self.stop()) as step:
            l1.walk_step(self.case, self.chain)
        self.assertEqual(step.call_count, 1)
        self.assertEqual(self.chain["status"], "insufficient_evidence")

    def test_deferred_change_still_requires_confirmation(self):
        self.review()
        self.chain["review_limit_blocked"] = True
        with patch.object(self.walker, "step", side_effect=[self.stop(),
                {"action": "submit", "reason": "尝试提交"}, self.stop("不确认暂存修改")]) as step:
            l1.walk_step(self.case, self.chain)
        self.assertEqual(step.call_count, 3)
        self.assertEqual(self.chain["status"], "insufficient_evidence")

    def test_other_budget_failure_is_not_misreported_as_model_budget(self):
        self.review()
        with patch.object(self.walker, "step", side_effect=[self.stop(), Limit("tool_budget_exhausted")]):
            with self.assertRaisesRegex(Limit, "tool_budget_exhausted"):
                l1.walk_step(self.case, self.chain)

    def test_rejected_or_withdrawn_is_not_converted(self):
        self.review()
        for disposition in ("rejected", "withdrawn"):
            with self.subTest(disposition=disposition):
                action = {"action": "stop", "disposition": disposition, "reason": "不保留"}
                if disposition == "rejected":
                    action["evidence_refs"] = [self.ref]
                with patch.object(self.walker, "step", return_value=action) as step:
                    l1.walk_step(self.case, self.chain)
                self.assertEqual(step.call_count, 1)
                self.assertEqual(self.chain["status"], disposition)
                self.chain["status"] = "exploring"

    def test_last_reserved_model_turn_does_not_request_extra_call(self):
        self.review()
        budget = self.case.rt.budget
        budget.models = CONFIG.max_model_calls - budget.meta_left - sum(len(x) for x in budget.holds.values())
        self.case.rt.model.call.side_effect = None
        self.case.rt.model.call.return_value = self.stop()
        l1.walk_step(self.case, self.chain)
        self.assertTrue(self.walker.last_final_action_only)
        self.assertEqual(self.case.rt.model.call.call_count, 1)
        self.assertEqual(self.chain["status"], "insufficient_evidence")
        self.assertFalse(any(e["event"] == "root_stop_confirmation_requested" for e in self.events()))

    def test_shared_budget_consumed_before_confirmation_preserves_stop(self):
        self.review()
        with patch.object(self.walker, "step", side_effect=[self.stop(), Limit("model_budget_exhausted")]) as step:
            l1.walk_step(self.case, self.chain)
        self.assertEqual(step.call_count, 2)
        self.assertEqual(self.chain["status"], "insufficient_evidence")
        self.assertIn("未能完成根因与传播关系的再次确认", self.chain["reason"])
        self.assertEqual(meta.selection_options(self.case), {"root_causes": {}, "propagation": {}})
        self.assertTrue(any(e["event"] == "root_stop_confirmation_unfinished" for e in self.events()))

    def test_program_assembly_does_not_bypass_failed_evidence_replay(self):
        self.review()
        self.case.rt.model.call.side_effect = [self.stop(), {"action": "submit", "reason": "保留根因"}]
        l1.walk_step(self.case, self.chain)
        self.case.investigate = Mock()
        # 最终一次正式综合也必须处理程序回放的空结果，不能直接组装已有根因。
        self.case.finalize_only = True
        self.case.cfg = replace(CONFIG, synthesis_final_max_retries=0)
        self.case.rt.model.call.side_effect = [conclusion_reply([self.chain]), synthesis_reply(self.chain)]
        with patch.object(self.case.rt.tele, "verify", return_value={"kind": "trace", "sql": "SELECT 1", "rows": []}):
            report = self.case.run()
        self.assertEqual(report["status"], "technical_failure")
        self.assertEqual(report["failure_stage"], "synthesis")
        self.assertIn("回放均未返回数据", report["completion_reason"])
        self.assertIsNone(report["answer"])
        self.assertFalse((self.case.out / "agent_answer.json").exists())


if __name__ == "__main__":
    unittest.main()
