"""模拟综合回流，不连接模型或真实案例。"""

import copy
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agents import meta, synthesis
from agents.loop import StageFailure
from settings import CONFIG
from test_synthesis_design import trajectory_fixture
import workflow


def synthesis_case():
    budget = Mock()
    budget.can_synthesis_continue.return_value = True
    rt = SimpleNamespace(records=Mock(), budget=budget, tele=Mock(), names={"A", "B"})
    return SimpleNamespace(cfg=CONFIG, out=Path("."), rt=rt, chains={"C1": {}})


def sessions():
    lookup, final = Mock(id="synthesis:lookup"), Mock(id="synthesis:final")
    lookup.synthesis_size.return_value = final.synthesis_size.return_value = {"fits": True}
    return lookup, final


class SynthesisRecoveryTests(unittest.TestCase):
    def test_meta_receives_reason_on_each_decision(self):
        session = Mock(id="meta:arbitrate")
        session.seen = set()
        session.step.side_effect = [
            {"action": "followup", "target_type": "l1", "chain_id": "C1",
             "question": "查 RSS", "reason": "区分类型"},
            {"action": "conclude", "branch_assessments": [], "confidence_scores": [],
             "open_issues": [], "termination_reason": "调查结束"},
        ]
        feedback = {"meta_return_no": 1, "original_answer": {"root_causes": [],
            "propagation": [], "reason": "reservation 内存异常且 mem_stress 更可能"},
            "review_request": {"question": "查 RSS"}}
        case = SimpleNamespace(finalize_only=False, ordered=lambda: [], symptoms=[], rounds=[],
            followups=[], requests=[], investigation_stops=[], cfg=CONFIG, meta=session,
            chains={}, families={}, branch_frontiers={}, rt=SimpleNamespace(records=Mock()))

        def followup(*args, **kwargs):
            case.followups.append({"recovery_no": kwargs["recovery_no"]})

        case.followup = Mock(side_effect=followup)
        meta.arbitrate(case, synthesis_feedback=feedback)
        contexts = [call.args[0] for call in session.step.call_args_list]
        self.assertEqual(len(contexts), 2)
        for context in contexts:
            self.assertEqual(context["synthesis_feedback"]["original_answer"], feedback["original_answer"])
            self.assertEqual(context["synthesis_feedback"]["original_answer"]["reason"],
                             feedback["original_answer"]["reason"])

    def test_working_set_keeps_old_and_new_evidence_and_feedback(self):
        trajectory = trajectory_fixture()
        trajectory["实际证据"]["E-2"] = {"kind": "metric", "rows": [{"value": "新证据"}]}
        trajectory["综合回流历史"] = [{"synthesis_feedback": {"original_answer": {
            "root_causes": [], "propagation": [], "reason": "旧判断"}},
            "followups": [{"returned_revision": 3}]}]
        working_set, index = synthesis._working_set(trajectory)
        self.assertEqual(index["E-1"]["object"]["rows"][0]["value"], "原始数据")
        self.assertEqual(index["E-2"]["object"]["rows"][0]["value"], "新证据")
        self.assertEqual({v["revision"] for v in working_set["真实链版本和审核"]}, {1, 2})
        self.assertEqual(working_set["综合回流历史"][0]["synthesis_feedback"]
                         ["original_answer"]["reason"], "旧判断")

    def test_empty_reason_is_preserved_and_sent_with_meta_request(self):
        case = synthesis_case()
        lookup, final = sessions()
        empty = {"root_causes": [], "propagation": [],
                 "reason": "reservation 内存异常，mem_stress 更可能，但尚未确认。"}
        request = {"request_meta_review": {"question": "比较内存压力与负载",
                   "checks_already_done": "已比较 RSS", "expected_change": "区分候选类型",
                   "chain_id": "C1", "evidence_refs": ["E-1"]}, "reason": "需要进一步区分"}
        lookup.step.side_effect = [empty, request]
        with patch.object(synthesis, "collect_trajectory", return_value=trajectory_fixture()), \
             patch.object(synthesis, "Session", side_effect=[lookup, final]), \
             patch.object(synthesis.state, "report"), \
             patch.object(synthesis, "export_selected_answer") as export:
            answer, prediction, report = synthesis.synthesize(case, {}, meta_returns_left=3)
        self.assertIsNone(answer)
        self.assertIsNone(prediction)
        self.assertEqual(report["status"], "meta_review_requested")
        self.assertEqual(report["original_answer"], empty)
        self.assertIsNot(report["original_answer"], empty)
        self.assertEqual(report["review_request"]["question"], request["request_meta_review"]["question"])
        next_context = lookup.step.call_args_list[1].args[0]
        self.assertEqual(next_context["previous_answer"], empty)
        self.assertEqual(next_context["validation_feedback"][0]["code"], "EMPTY_ROOT_CAUSES")
        self.assertIn("request_meta_review", next_context["allowed_actions"])
        export.assert_not_called()

    def test_direct_meta_request_has_no_invented_prior_answer(self):
        case = synthesis_case()
        lookup, final = sessions()
        lookup.step.return_value = {"request_meta_review": {"question": "检查其他起点",
            "checks_already_done": "已检查 A", "expected_change": "定位未调查的候选",
            "chain_id": None, "evidence_refs": []}, "reason": "当前方向不足"}
        with patch.object(synthesis, "collect_trajectory", return_value=trajectory_fixture()), \
             patch.object(synthesis, "Session", side_effect=[lookup, final]):
            _, _, report = synthesis.synthesize(case, {}, meta_returns_left=3)
        self.assertEqual(report["status"], "meta_review_requested")
        self.assertIsNone(report["original_answer"])
        self.assertIsNone(report["source_draft_id"])

    def test_final_empty_root_has_one_submission_and_three_retries(self):
        case = synthesis_case()
        lookup, final = sessions()
        empty = {"root_causes": [], "propagation": [], "reason": "候选尚不明确"}
        lookup.step.return_value = empty
        final.step.return_value = empty
        with patch.object(synthesis, "collect_trajectory", return_value=trajectory_fixture()), \
             patch.object(synthesis, "Session", side_effect=[lookup, final]), \
             patch.object(synthesis.state, "report"), \
             patch.object(synthesis, "export_selected_answer") as export:
            with self.assertRaises(StageFailure):
                synthesis.synthesize(case, {}, final_mode=True)
        self.assertEqual(lookup.step.call_count + final.step.call_count, 4)
        self.assertEqual(final.step.call_count, 3)
        for call in final.step.call_args_list:
            self.assertEqual(call.args[0]["allowed_actions"], ["submit_answer"])
            self.assertEqual(call.args[0]["validation_feedback"][0]["path"], "root_causes")
        export.assert_not_called()

    def test_three_returns_preserve_reason_and_include_all_prior_history(self):
        case = workflow.Incident.__new__(workflow.Incident)
        case.cfg, case.out = CONFIG, Path(".")
        case.rt = SimpleNamespace(records=Mock(), budget=Mock(), handle="fixture")
        case.rt.budget.can_recovery.return_value = True
        case.rt.budget.snapshot.return_value = {}
        case.meta = SimpleNamespace(id="meta:arbitrate")
        case.investigate = Mock()
        case.ordered = Mock(return_value=[])
        case.counts = Mock(return_value={})
        case.followups, case.investigation_stops, case.requests = [], [], []
        case.rounds, case.recovery_history = [], []
        case.finalize_only, case.meta_selection_report = False, {}
        case.meta_conclusion, case.synthesis_report = {}, {}
        empty = {"root_causes": [], "propagation": [], "reason": "原始理由：A 更可能"}
        request = {"question": "核对候选", "checks_already_done": "已查 trace",
                   "expected_change": "区分类型", "chain_id": None, "evidence_refs": [], "reason": "补查"}
        rec = {"status": "meta_review_requested", "original_answer": empty,
               "source_draft_id": "synthesis-r01-d01", "review_request": request}
        answer = {"root_causes": [{"service": "A", "fault_kind": "network_delay"}], "propagation": []}
        captures, history_lengths = [], []

        def arbitrate(current, **kwargs):
            if kwargs:
                captures.append(copy.deepcopy(kwargs["synthesis_feedback"]))
                current.followups.append({"question": "核对候选"})
            return {"termination_reason": "结案"}

        returns = iter([(None, None, copy.deepcopy(rec)), (None, None, copy.deepcopy(rec)),
                        (None, None, copy.deepcopy(rec)), (answer, {}, {"status": "completed"})])

        def synthesize(*args, **kwargs):
            history_lengths.append(len(kwargs["recovery_history"]))
            return next(returns)

        with patch.object(workflow, "stage", side_effect=lambda *a, **k: nullcontext()), \
             patch.object(workflow.state, "report"), \
             patch.object(meta, "arbitrate", side_effect=arbitrate), \
             patch.object(synthesis, "synthesize", side_effect=synthesize) as synth:
            result = case.run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(captures), 3)
        for feedback in captures:
            self.assertEqual(feedback["original_answer"], empty)
            self.assertEqual(feedback["original_answer"]["reason"], empty["reason"])
        self.assertEqual([c.kwargs["round_no"] for c in synth.call_args_list], [1, 2, 3, 4])
        self.assertEqual([c.kwargs["final_mode"] for c in synth.call_args_list], [False, False, False, True])
        self.assertEqual(len(case.recovery_history), 3)
        self.assertEqual(history_lengths, [0, 1, 2, 3])


if __name__ == "__main__":
    unittest.main()
