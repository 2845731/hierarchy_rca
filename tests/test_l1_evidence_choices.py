"""L1 短编号只改变模型选择；层间与正式产物仍使用完整证据编号。"""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from agents import l1, l2, meta, seed, synthesis
from agents.loop import EvidenceChoices, Invalid, Session, StageFailure
from settings import CONFIG, BASELINE, DATASET, REFERENCE, WORKSPACE
from test_sql_evidence import conclusion_reply, synthesis_reply, write_fixture
from workflow import Incident


class EvidenceChoiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="l1_choice_")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        write_fixture(root / "data")
        self.model = Mock()
        self.model.call.side_effect = AssertionError("测试只允许固定回复，不调用真实模型")
        self.case = Incident(root / "data", root / "out", self.model, CONFIG, "case_0001")
        self.addCleanup(self.case.rt.tele.close)
        self.tele = self.case.rt.tele
        self.ref = self.tele.call("query_telemetry", {"sql": "SELECT time FROM abnormal_traces"})["evidence_refs"][0]
        self.other = self.tele.call("query_telemetry", {"sql": "SELECT span_id FROM abnormal_traces"})["evidence_refs"][0]
        self.chain = l1.make_chain(self.case, {"seed_id": "S01", "service": "rate", "round": 1,
                                              "evidence_refs": [self.ref]}, 1)
        self.walker = self.case.walkers[self.chain["chain_id"]]
        self.walker.progress = Mock()

    def catalog(self):
        return self.walker.evidence_choices.available(self.walker.seen)

    def test_workspace_dependencies_resolve_for_this_version(self):
        self.assertEqual(WORKSPACE, Path(__file__).resolve().parents[4])
        self.assertTrue((BASELINE / "contracts/agent_contract.txt").is_file())
        self.assertTrue((REFERENCE / "scripts/paper_reference_scoring.py").is_file())
        self.assertTrue((DATASET / "cases").is_dir())

    def test_choices_stable_parallel_and_scoped_to_current_seen(self):
        initial = self.catalog()
        choice = next(iter(initial))
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: self.walker.evidence_choices.available({self.ref, self.other}), range(40)))
        self.assertTrue(all(v == values[0] for v in values))
        self.assertEqual(values[0][choice], self.ref)
        self.assertEqual(len(set(values[0])), 2)
        self.assertEqual(self.catalog(), initial)  # 其他分支有映射不代表本分支已收到证据。
        other_choice = next(k for k, v in values[0].items() if v == self.other)
        with self.assertRaises(Invalid):
            self.walker.resolve_l1_choices({"action": "review", "evidence_choice_ids": [other_choice]}, initial)

    def test_conflicts_unknown_and_legacy_are_not_guessed(self):
        offered = self.catalog()
        choice = next(iter(offered))
        legacy = {"action": "review", "root": {"service": "rate", "fault_kind": "network_delay",
                                                    "evidence_refs": [self.ref]}, "reason": "原协议"}
        self.assertEqual(self.walker.resolve_l1_choices(legacy, offered), (legacy, {}))
        with self.assertRaises(Invalid):
            self.walker.resolve_l1_choices({"action": "review", "evidence_choice_ids": [choice + "3"]}, offered)
        with self.assertRaises(Invalid):
            self.walker.resolve_l1_choices({"action": "review", "evidence_refs": [self.other],
                                           "evidence_choice_ids": [choice]}, offered)
        with self.assertRaises(Invalid):
            self.walker.resolve_l1_choices({"action": "review", "evidence_choice_ids": choice}, offered)
        same, _ = self.walker.resolve_l1_choices({"action": "review", "evidence_refs": [self.ref],
                                                  "evidence_choice_ids": [choice]}, offered)
        self.assertEqual(same["evidence_refs"], [self.ref])

    def test_original_typo_cannot_be_guessed_but_choice_resolves_exactly(self):
        actual = "E-32fc577746d54470b0b8"
        typo = "E-32fc577746d54470b0b38"
        self.walker.seen.add(actual)
        offered = self.catalog()
        choice = next(k for k, v in offered.items() if v == actual)
        action, _ = self.walker.resolve_l1_choices({"action": "review", "evidence_choice_ids": [choice]}, offered)
        self.assertEqual(action["evidence_refs"], [actual])
        with self.assertRaises(Invalid):
            self.case.rt.refs([typo], self.walker.seen)

    def test_step_rejects_unknown_choice_then_accepts_without_guessing(self):
        choice = next(iter(self.catalog()))
        bad = {"action": "review", "evidence_choice_ids": [choice + "3"], "reason": "审核"}
        good = {"action": "review", "evidence_choice_ids": [choice], "reason": "审核"}
        self.model.call.side_effect = [bad, good]
        result = self.walker.step({"chain": self.chain}, ["review"])
        self.assertEqual(result["evidence_refs"], [self.ref])
        self.assertEqual(self.model.call.call_count, 2)
        self.assertIn("未提供证据选择编号", self.model.call.call_args.args[1])
        self.assertEqual(self.walker.l1_review_failures, 1)  # 由 L1 完整检查成功后才清零。

    def test_l1_original_full_id_step_still_works_without_new_field(self):
        self.model.call.side_effect = None
        legacy = {"action": "review", "root": {"service": "rate", "fault_kind": "network_delay",
                  "evidence_refs": [self.ref]}, "reason": "原有完整编号"}
        self.model.call.return_value = legacy
        result = self.walker.step({"chain": self.chain}, ["review"])
        self.assertEqual(result, legacy)
        self.assertEqual(self.walker.history[-1]["data"], legacy)

    def test_review_retries_share_one_limit_across_choice_and_review_checks(self):
        bad_choice = {"action": "review", "evidence_choice_ids": ["不存在"], "reason": "审核"}
        bad_review = {"action": "review", "root": {"service": "other"}, "reason": "审核"}
        for retries in (CONFIG.max_l1_review_retries, 0):
            with self.subTest(retries=retries):
                self.case.rt.cfg = replace(CONFIG, max_l1_review_retries=retries)
                self.walker.accepted()
                self.model.call.reset_mock()
                self.model.call.side_effect = [bad_choice] + [bad_review] * retries
                with self.assertRaisesRegex(StageFailure, f"连续 {retries + 1} 次未通过校验"):
                    l1.walk_step(self.case, self.chain)
                self.assertEqual(self.model.call.call_count, retries + 1)
                self.assertEqual(self.walker.bad_actions, 0)
                self.assertEqual(self.chain["reviews"], [])

    def test_other_actions_still_stop_after_three_rejections(self):
        self.model.call.side_effect = None
        self.model.call.return_value = {"action": "unsupported", "reason": "未知动作"}
        with self.assertRaisesRegex(StageFailure, "连续 3 个无效动作"):
            self.walker.step({"chain": self.chain}, ["review"])
        self.assertEqual(self.model.call.call_count, 3)

    def test_note_and_replay_receive_original_refs_and_no_new_permission(self):
        choice = next(iter(self.catalog()))
        self.model.call.side_effect = [
            {"action": "tool", "tool": "record_progress", "args": {
                "finding": f"已查 {choice}", "next_step": "核对异常", "evidence_choice_ids": [choice]}},
            {"action": "tool", "tool": "replay_evidence", "args": {"evidence_choice_id": choice}},
            {"action": "submit", "reason": "结束"},
        ]
        self.walker.step({"chain": self.chain}, ["submit"])
        note = self.walker.investigation.notes[0]
        self.assertEqual(note["evidence_refs"], [self.ref])
        self.assertIn(self.ref, note["finding"])
        events = [e for e in self.walker.history if e["event"] == "tool_result"]
        self.assertEqual(events[-1]["data"]["args"]["evidence_ref"], self.ref)
        self.assertEqual(self.case.rt.budget.snapshot()["tool_calls"], 2)
        self.assertEqual(self.walker.seen, {self.ref})

    def test_short_and_full_replay_are_the_same_repeated_query(self):
        choice = next(iter(self.catalog()))
        self.model.call.side_effect = [
            {"action": "tool", "tool": "replay_evidence", "args": {"evidence_choice_id": choice}},
            {"action": "tool", "tool": "replay_evidence", "args": {"evidence_ref": self.ref}},
            {"action": "submit", "reason": "结束"},
        ]
        self.walker.step({"chain": self.chain}, ["submit"])
        self.assertEqual(self.walker.consecutive_repeats, 1)
        self.assertEqual(self.walker.last_repeated_query["args"]["evidence_ref"], self.ref)
        self.assertEqual(self.case.rt.budget.snapshot()["tool_calls"], 2)

    def test_empty_and_invalid_evidence_do_not_become_supported(self):
        empty = self.tele.call("query_telemetry", {"sql": "SELECT time FROM abnormal_traces WHERE 1=0"})["evidence_refs"][0]
        self.walker.seen.add(empty)
        choice = next(k for k, v in self.catalog().items() if v == empty)
        action, _ = self.walker.resolve_l1_choices({"action": "review", "root": {
            "service": "rate", "fault_kind": "network_delay", "evidence_choice_ids": [choice]}}, self.catalog())
        with self.assertRaises(Invalid):
            l1.root_claim(self.case, action["root"], self.walker, "rate")

    def test_adjacent_evidence_check_remains_active(self):
        self.walker.seen.add(self.other)
        self.walker.neighbors[("rate", "search")] = [{"caller": "search", "callee": "rate",
                                                      "window": "abnormal", "evidence_refs": [self.ref]}]
        choice = next(k for k, v in self.catalog().items() if v == self.other)
        action, _ = self.walker.resolve_l1_choices({"action": "extend", "edge": {
            "from": "rate", "to": "search", "evidence_choice_ids": [choice]}}, self.catalog())
        with self.assertRaises(Invalid):
            l1.checked_edge(self.case, action["edge"], ["rate"], self.walker)

    def test_root_edge_symptom_and_top_level_choices_convert_separately(self):
        self.walker.seen.add(self.other)
        self.walker.neighbors[("rate", "search")] = [{"caller": "search", "callee": "rate",
                                                      "window": "abnormal", "evidence_refs": [self.ref]}]
        offered = self.catalog()
        edge_choice = next(k for k, v in offered.items() if v == self.ref)
        root_choice = next(k for k, v in offered.items() if v == self.other)
        raw = {"action": "review", "root": {"service": "rate", "fault_kind": "network_delay",
            "evidence_choice_ids": [root_choice]}, "symptom": {"service": "rate", "description": "异常",
            "evidence_choice_ids": [root_choice]}, "evidence_choice_ids": [edge_choice], "reason": "审核"}
        converted, _ = self.walker.resolve_l1_choices(raw, offered)
        self.assertEqual(converted["root"]["evidence_refs"], [self.other])
        self.assertEqual(converted["symptom"]["evidence_refs"], [self.other])
        self.assertEqual(converted["evidence_refs"], [self.ref])
        self.assertIn("evidence_choice_ids", raw["root"])  # 原始模型动作没有被改写。
        edge, _ = self.walker.resolve_l1_choices({"action": "extend", "edge": {
            "from": "rate", "to": "search", "evidence_choice_ids": [edge_choice]}}, offered)
        self.assertEqual(l1.checked_edge(self.case, edge["edge"], ["rate"], self.walker)["evidence_refs"], [self.ref])

    def test_l2_new_evidence_return_and_failure_handoff_remain_full(self):
        self.case.rt.budget.reserve(self.chain["chain_id"])
        reviewer = self.case.reviewers[self.chain["chain_id"]]
        reviewer.progress = Mock()
        audit = {"action": "audit", "reason": "审核", "edge_verdicts": [],
            "component_verdict": {"service": "rate", "verdict": "support", "reason": "支持", "evidence_refs": [self.other]},
            "fault_kind_verdict": {"fault_kind": "network_delay", "verdict": "support", "reason": "支持", "evidence_refs": [self.other]},
            "supported_prefix": ["rate"], "score": 0.8, "doubts": [], "recommendation": "keep"}
        def query_then_audit(*args, **kwargs):
            reviewer.seen.add(self.other)  # 审核过程中查到的证据。
            return audit
        reviewer.step = Mock(side_effect=query_then_audit)
        action = {"action": "review", "root": {"service": "rate", "fault_kind": "network_delay",
                  "evidence_refs": [self.ref]}, "reason": "审核",
                  "fault_diagnosis": {"observed_phenomenon": "异常", "candidates": [],
                                      "missing_evidence": "待核查", "next_check": "对照"}}
        self.case.review_dispatch(self.chain, action, self.walker)
        self.assertIn(self.other, self.walker.seen)
        self.assertIn(self.other, self.chain["last_review"]["available_refs_at_review"])
        self.assertIn(self.other, self.catalog().values())
        self.assertEqual(self.chain["last_review"]["component_verdict"]["evidence_refs"], [self.other])

        sibling = l1.make_chain(self.case, {"seed_id": "S02", "service": "rate", "round": 1,
                                           "evidence_refs": [self.ref]}, 1)
        sr = self.case.reviewers[sibling["chain_id"]]
        sr.seen.add(self.other)
        sr.step = Mock(side_effect=StageFailure("固定审核失败"))
        self.case.rt.budget.reserve(sibling["chain_id"])
        sw = self.case.walkers[sibling["chain_id"]]
        self.case.review_dispatch(sibling, action, sw)
        self.assertEqual(sibling["last_review"]["status"], "technical_failure")
        self.assertIn(self.other, sw.seen)
        self.assertIn(self.other, sw.evidence_choices.available(sw.seen).values())

    def test_fault_diagnosis_resolves_only_available_candidate_choices(self):
        offered = self.catalog()
        choice = next(k for k, v in offered.items() if v == self.ref)
        raw = {"action": "review", "fault_diagnosis": {"observed_phenomenon": "异常",
            "candidates": [{"fault_kind": "network_delay", "status": "plausible", "reason": "线索",
                            "evidence_choice_ids": [choice]}],
            "missing_evidence": "待区分", "next_check": "对照"}}
        converted, _ = self.walker.resolve_l1_choices(raw, offered)
        candidate = converted["fault_diagnosis"]["candidates"][0]
        self.assertEqual(candidate["evidence_refs"], [self.ref])
        self.assertNotIn("evidence_choice_ids", candidate)
        raw["fault_diagnosis"]["candidates"][0]["evidence_choice_ids"] = ["不存在"]
        with self.assertRaises(Invalid):
            self.walker.resolve_l1_choices(raw, offered)

    def test_fork_inherits_seen_and_stable_case_mapping(self):
        self.walker.seen.add(self.other)
        before = self.catalog()
        child = l1.make_chain(self.case, self.chain["seed"], 2, parent=self.chain,
                              nodes=["rate", "search"], edges=[{"from": "rate", "to": "search",
                                                                  "evidence_refs": [self.ref]}])
        fork = self.case.walkers[child["chain_id"]]
        self.assertEqual(fork.seen, self.walker.seen)
        self.assertEqual(fork.evidence_choices.available(fork.seen), before)

    def test_seed_supplement_receives_original_ref(self):
        request = {"chain_id": self.chain["chain_id"], "reason": "补查", "evidence_refs": [self.ref]}
        self.case.queue_requests([request])
        self.model.call.side_effect = None
        self.model.call.return_value = {"action": "propose_seeds", "seeds": [{"service": "rate",
            "evidence_refs": [self.ref], "reason": "补查候选"}], "symptoms": [], "reason": "补充调查"}
        fresh = seed.seed_round(self.case, [request])
        self.assertIn(self.ref, self.case.seed_agent.seen)
        self.assertEqual(fresh[0]["evidence_refs"], [self.ref])
        self.assertEqual(request["response"]["status"], "new_candidates")
        prompt_text = self.model.call.call_args.args[1]
        self.assertIn(self.ref, prompt_text)
        self.assertNotIn("available_evidence_choices", prompt_text)

    def test_meta_followup_keeps_original_chain_evidence(self):
        self.case.rt.budget.reserve(self.chain["chain_id"])
        self.case.walk_family = Mock(side_effect=lambda c: c.update(status="submitted"))
        self.case.followup({"target_type": "l1", "chain_id": self.chain["chain_id"],
                            "question": "继续核查", "reason": "补查"}, meta_calls_to_keep=4)
        self.assertEqual(self.chain["seed"]["evidence_refs"], [self.ref])
        self.assertEqual(self.chain["feedback"]["question"], "继续核查")
        self.assertEqual(self.case.followups[0]["returned_status"], "submitted")

    def test_l1_short_selection_flows_through_l2_meta_and_final_answer(self):
        # seed 仍提交完整编号，L1 通过短编号送审，L2/Meta 和正式导出仍用完整编号。
        self.model.call.side_effect = None
        self.model.call.return_value = {"action": "propose_seeds", "seeds": [
            {"service": "rate", "evidence_refs": [self.ref], "reason": "候选"}],
            "symptoms": [], "reason": "候选已形成"}
        self.case.seed_agent.seen.add(self.ref)
        selected = seed.seed_round(self.case, [])
        self.assertEqual(selected[0]["evidence_refs"], [self.ref])

        self.case.rt.budget.reserve(self.chain["chain_id"])
        choice = next(k for k, v in self.catalog().items() if v == self.ref)
        self.model.call.side_effect = [
            {"action": "review", "root": {"service": "rate", "fault_kind": "network_delay",
                "evidence_choice_ids": [choice]}, "reason": f"审核 {choice}",
             "fault_diagnosis": {"observed_phenomenon": "异常", "candidates": [
                 {"fault_kind": "network_delay", "status": "plausible", "reason": "调用异常",
                  "evidence_choice_ids": [choice]}], "missing_evidence": "待核查", "next_check": "对照"}},
            {"action": "audit", "reason": "支持", "edge_verdicts": [],
             "component_verdict": {"service": "rate", "verdict": "support", "reason": "支持", "evidence_refs": [self.ref]},
             "fault_kind_verdict": {"fault_kind": "network_delay", "verdict": "support", "reason": "支持", "evidence_refs": [self.ref]},
             "supported_prefix": ["rate"], "score": 0.8, "doubts": [], "recommendation": "keep"},
            {"action": "submit", "reason": "提交"},
        ]
        l1.walk_step(self.case, self.chain)
        self.assertEqual(self.chain["root"]["evidence_refs"], [self.ref])
        self.assertIn(self.ref, self.model.call.call_args_list[1].args[1])  # L2 的审核上下文。
        l1.walk_step(self.case, self.chain)
        self.assertEqual(self.chain["status"], "submitted")
        options = meta.selection_options(self.case)
        self.assertIn(self.ref, options["root_causes"][("rate", "network_delay")][0]["evidence_refs"])
        self.model.call.side_effect = [conclusion_reply([self.chain]), synthesis_reply(self.chain)]
        conclusion = meta.arbitrate(self.case)
        self.assertNotIn("available_evidence_choices", self.model.call.call_args.args[1])
        self.assertNotIn("root_causes", conclusion)
        before = self.model.call.call_count
        answer, prediction, report = synthesis.synthesize(self.case, conclusion)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(self.model.call.call_count, before + 1)  # 独立综合生成一次，程序导出不另调模型。
        self.assertEqual(prediction["root_causes"][0]["evidence_ids"], [self.ref])
        self.assertEqual(answer["root_causes"][0]["evidence"][0]["sql"], self.tele.evidence(self.ref)["sql"])
        self.assertNotIn("claim", answer["root_causes"][0]["evidence"][0])
        events = [json.loads(line) for line in (self.case.out / "stages/events.jsonl").read_text("utf-8").splitlines()]
        raw = next(x for x in events if x["event"] == "model_action" and x["data"].get("action") == "review")
        self.assertEqual(raw["data"]["root"]["evidence_choice_ids"], [choice])
        resolved = next(x for x in events if x["event"] == "evidence_choice_resolved")
        self.assertEqual(resolved["data"]["choices"], [{"choice_id": choice, "evidence_ref": self.ref}])


if __name__ == "__main__":
    unittest.main()
