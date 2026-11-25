"""Meta 的规则与调度回归测试：仅本地固定输入，不调用模型接口。

运行：python -B -m unittest discover -s tests -v
真实案例记录另作只读回放；这里的小型数据用于覆盖错误路径，不作为诊断结果。
"""
import copy
import json
import sys
import tempfile
import threading
import unittest
import state
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents import meta, synthesis
from agents.loop import Runtime, Session, StageFailure, prompt
from output_contract import EvidenceSelectionError, export_answer, export_selected_answer
from local_answer_contract import LocalAgentRCAOutput
from progress_display import render_event
from settings import CONFIG
from state import Budget, Limit, Records
from tools.telemetry import Telemetry
from workflow import Incident


class FixtureEvidence(Telemetry):
    def __init__(self):
        self.items = {
            ref: {"kind": "trace", "sql": "SELECT * FROM abnormal_traces", "rows": [{"n": 1}]}
            for ref in ("root", "ab", "bc", "da", "ae", "ef", "other")
        }

    def evidence(self, ref):
        return self.items[ref]

    def verify(self, ref):
        return self.evidence(ref)

    def service_names(self):
        return {"A", "B", "C", "D", "E", "F"}


def chain(cid, nodes, refs, root=None):
    edges = [{"from": a, "to": b, "evidence_refs": [ref]}
             for a, b, ref in zip(nodes, nodes[1:], refs)]
    review = {
        "status": "reviewed", "revision": 1, "review_id": cid + "-R01",
        "component_verdict": {"service": nodes[0], "verdict": "support" if root else "unknown",
                              "evidence_refs": ["root"] if root else []},
        "fault_kind_verdict": {"fault_kind": root["fault_kind"] if root else None,
                               "verdict": "support" if root else "unknown",
                               "evidence_refs": ["root"] if root else []},
        "edge_verdicts": [{**e, "verdict": "support"} for e in edges],
    }
    return {
        "chain_id": cid, "revision": 1, "l1_id": cid, "l2_id": "l2:" + cid,
        "seed": {"service": nodes[0]}, "nodes": nodes, "edges": edges,
        "root": root, "symptom": None, "target_symptom": None,
        "parent_chain_id": None, "parent_revision": None, "status": "submitted",
        "last_review": review, "reviews": [review], "decisions": [], "reason": "fixture",
    }


class MetaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rca_meta_test_")
        self.addCleanup(self.temp.cleanup)
        c = Incident.__new__(Incident)
        c.cfg, c.out = CONFIG, Path(self.temp.name)
        c.seeds, c.symptoms, c.requests, c.rounds = [], [], [], []
        c.seed_attempts = 0  # 与 Incident.__init__ 一致，供 counts() 报告候选尝试。
        c.followups, c.investigation_stops = [], []
        c.recovery_history = []
        c.finalize_only, c.meta_selection_report = False, {}
        c.meta_conclusion, c.synthesis_report = {}, {}
        c.investigate = Mock()
        root = {"service": "A", "fault_kind": "network_delay", "evidence_refs": ["root"]}
        c.chains = {"C1": chain("C1", ["A", "B", "C"], ["ab", "bc"], root),
                    "C2": chain("C2", ["D", "A", "E", "F"], ["da", "ae", "ef"])}
        rt = Runtime.__new__(Runtime)
        rt.cfg, rt.handle, rt.budget = CONFIG, "fixture", Budget(CONFIG)
        rt.records, rt.tele = Records(c.out), FixtureEvidence()
        rt.compact_catalog, rt.provider_stop, rt.provider_failure = {}, threading.Event(), None
        rt.model = Mock()
        c.rt, c.meta = rt, Session(rt, "meta", "meta:arbitrate")
        self.case = c
        self.good = {
            "action": "select", "selected_chain_ids": ["C1"],
            "root_causes": [root], "propagation": copy.deepcopy(c.chains["C1"]["edges"]),
            "reason": "Choose supported items",
        }
        self.bad = copy.deepcopy(self.good)
        self.bad["selected_chain_ids"].append("C2")
        self.bad["propagation"].extend(copy.deepcopy(c.chains["C2"]["edges"][1:]))

    def test_valid_root_edges_empty_and_edges_only(self):
        for roots, edges, ids in [
            (self.good["root_causes"], self.good["propagation"], ["C1"]),
            ([], self.good["propagation"], ["C1"]),
            (self.good["root_causes"], [], ["C1"]), ([], [], []),
        ]:
            a = {**self.good, "root_causes": roots, "propagation": edges, "selected_chain_ids": ids}
            self.assertTrue(meta.check_selection(self.case, a)["valid"])

    def test_suffix_reports_each_edge_and_keeps_only_valid_items(self):
        result = meta.check_selection(self.case, self.bad)
        self.assertEqual([i["code"] for i in result["issues"]], ["missing_prefix", "missing_prefix"])
        self.assertEqual(result["issues"][0]["sources"][0]["missing_prefix"], [{"from": "D", "to": "A"}])
        kept, removed = meta.keep_valid_selection(self.case, self.bad)
        self.assertEqual(kept["propagation"], self.good["propagation"])
        self.assertEqual(kept["selected_chain_ids"], ["C1"])
        self.assertTrue(meta.check_selection(self.case, kept)["valid"])
        self.assertTrue(any(i["code"] == "unused_chain" for i in removed))

    def test_evidence_scope_and_prefix_reported_together(self):
        a = copy.deepcopy(self.bad)
        a["propagation"][0]["evidence_refs"] = ["other"]
        result = meta.check_selection(self.case, a)
        codes = [i["code"] for i in result["issues"]]
        self.assertIn("evidence_scope", codes)
        self.assertIn("missing_prefix", codes)
        self.assertEqual(result["issues"][0]["allowed_refs"], ["ab"])
        kept, _ = meta.keep_valid_selection(self.case, a)
        self.assertEqual(kept["propagation"], [])  # 不能让证据非法的 A→B 为后面的 B→C 搭桥。

    def test_multiple_sources_need_only_one_complete_prefix(self):
        self.case.chains["C3"] = chain("C3", ["B", "C"], ["bc"])
        a = {**self.good, "root_causes": [], "propagation": [self.good["propagation"][1]],
             "selected_chain_ids": ["C1", "C3"]}
        self.assertTrue(meta.check_selection(self.case, a)["valid"])

    def test_direction_support_and_revision_are_exact(self):
        a = copy.deepcopy(self.good)
        a["propagation"][0]["from"], a["propagation"][0]["to"] = "B", "A"
        self.assertIn("unsupported_item", [i["code"] for i in meta.check_selection(self.case, a)["issues"]])
        self.case.chains["C1"]["last_review"]["edge_verdicts"][0]["verdict"] = "unknown"
        self.assertFalse(meta.check_selection(self.case, self.good)["valid"])
        self.case.chains["C1"]["revision"] = 2
        self.assertIn("unreviewed_chain", [i["code"] for i in meta.check_selection(self.case, self.good)["issues"]])

    def test_normal_only_and_log_only_edges_are_rejected(self):
        for kind, sql in [("trace", "SELECT * FROM normal_traces"), ("log", "SELECT * FROM abnormal_logs")]:
            self.case.rt.tele.items["ab"].update(kind=kind, sql=sql)
            result = meta.check_selection(self.case, self.good)
            self.assertIn("invalid_evidence", [i["code"] for i in result["issues"]])

    def test_malformed_selection_is_not_guessed(self):
        for changes in [{"reason": ""}, {"propagation": None}, {"selected_chain_ids": [None]}]:
            with self.assertRaises(StageFailure):
                meta.keep_valid_selection(self.case, {**self.good, **changes})

    def test_wrong_item_field_types_receive_feedback_not_a_python_exception(self):
        a = copy.deepcopy(self.good)
        a["propagation"][0]["from"] = ["A"]
        self.case.rt.model.call.side_effect = [a, self.good]
        result = meta.arbitrate_legacy(self.case)
        self.assertEqual(result["propagation"], self.good["propagation"])
        self.assertEqual(self.case.meta_selection_report["selection_failures"], 1)

    def test_unknown_validator_exception_propagates(self):
        self.case.rt.tele.validate = Mock(side_effect=RuntimeError("unexpected validation defect"))
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            meta.check_selection(self.case, self.good)

    def test_meta_repairs_after_three_rejections_and_receives_all_feedback(self):
        self.case.rt.model.call.side_effect = [copy.deepcopy(self.bad) for _ in range(3)] + [self.good]
        result = meta.arbitrate_legacy(self.case)
        self.assertEqual(result["propagation"], self.good["propagation"])
        self.assertEqual(self.case.rt.model.call.call_count, 4)
        self.assertEqual(self.case.meta.bad_actions, 0)
        payload = self.case.rt.model.call.call_args_list[1].args[1]
        self.assertIn("selection_feedback", payload)
        self.assertIn("missing_prefix", payload)
        self.assertFalse(self.case.meta_selection_report["used_partial_selection"])
        self.assertEqual(len(self.case.meta_selection_report["resolved_issues"]), 2)

    def test_nine_failed_selections_retain_partial_then_export(self):
        self.case.rt.model.call.return_value = self.bad
        result = meta.arbitrate_legacy(self.case)
        self.assertEqual(self.case.rt.model.call.call_count, 9)
        self.assertEqual(self.case.meta_selection_report["selection_failures"], 9)
        self.assertTrue(self.case.meta_selection_report["used_partial_selection"])
        self.assertEqual(len(result["propagation"]), 2)
        self.assertNotEqual(result["reason"], self.bad["reason"])
        answer = {
            group: [
                {**{k: v for k, v in item.items() if k != "evidence_refs"},
                 "evidence": [{"kind": "trace", "sql": "SELECT * FROM abnormal_traces", "claim": "Fixture evidence"}]}
                for item in result[group]
            ] for group in ("root_causes", "propagation")
        }
        export_answer(self.case.out, answer, result, self.case.rt.tele, list(self.case.chains.values()))
        self.assertTrue((self.case.out / "agent_answer.json").is_file())

    def test_program_assembly_preserves_selection_and_reuses_shared_ref(self):
        selection = copy.deepcopy(self.good)
        selection["propagation"][0]["evidence_refs"] = ["root", "ab"]
        self.case.rt.tele.verify = Mock(side_effect=self.case.rt.tele.evidence)
        answer, projection = export_selected_answer(
            self.case.out, selection, self.case.rt.tele, list(self.case.chains.values()))
        self.assertEqual(self.case.rt.model.call.call_count, 0)
        self.assertEqual(self.case.rt.tele.verify.call_count, 3)
        self.assertEqual(answer["root_causes"][0]["service"], "A")
        self.assertEqual(answer["root_causes"][0]["fault_kind"], "network_delay")
        self.assertEqual(answer["propagation"][0]["from"], "A")
        self.assertEqual(answer["propagation"][0]["to"], "B")
        self.assertEqual(len(answer["propagation"][0]["evidence"]), 2)
        self.assertTrue(all(set(e) == {"kind", "sql"} for group in answer.values()
                            for item in group for e in item["evidence"]))
        self.assertEqual(projection["root_causes"][0]["evidence_ids"], ["root"])
        self.assertEqual(json.loads((self.case.out / "agent_answer.json").read_text(encoding="utf-8")), answer)
        self.assertEqual(set(json.loads((self.case.out / "phase4_output/evidence_replay.json").read_text(encoding="utf-8"))),
                         {"root", "ab", "bc"})
        LocalAgentRCAOutput.model_validate(answer)

    def test_program_assembly_never_saves_unsupported_or_empty_evidence(self):
        for case_name in ("all_empty", "log_only", "unknown_service"):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory(prefix="rca_export_test_") as temp:
                selection = copy.deepcopy(self.good)
                if case_name == "all_empty":
                    self.case.rt.tele.items["root"]["rows"] = []
                elif case_name == "log_only":
                    self.case.rt.tele.items["ab"]["kind"] = "log"
                else:
                    selection["root_causes"][0]["service"] = "outside"
                with self.assertRaises(ValueError):
                    export_selected_answer(Path(temp), selection, self.case.rt.tele, list(self.case.chains.values()))
                self.assertFalse((Path(temp) / "agent_answer.json").exists())
                if case_name == "all_empty":
                    replay = json.loads((Path(temp) / "phase4_output/evidence_replay.json").read_text(encoding="utf-8"))
                    self.assertFalse(replay["root"]["has_rows"])
                    self.case.rt.tele.items["root"]["rows"] = [{"n": 1}]
                if case_name == "log_only":
                    self.case.rt.tele.items["ab"]["kind"] = "trace"

    def test_program_assembly_records_empty_ref_but_keeps_nonempty(self):
        selection = copy.deepcopy(self.good)
        selection["root_causes"][0]["evidence_refs"] = ["root", "other"]
        self.case.rt.tele.items["other"]["rows"] = []
        answer, projection = export_selected_answer(
            self.case.out, selection, self.case.rt.tele, list(self.case.chains.values()))
        self.assertEqual(len(answer["root_causes"][0]["evidence"]), 1)
        self.assertEqual(projection["root_causes"][0]["evidence_ids"], ["root", "other"])
        replay = json.loads((self.case.out / "phase4_output/evidence_replay.json").read_text(encoding="utf-8"))
        self.assertFalse(replay["other"]["has_rows"])

    def test_synthesized_export_rejects_empty_root_even_with_edges(self):
        selection = {**self.good, "root_causes": []}
        with self.assertRaises(EvidenceSelectionError):
            export_selected_answer(self.case.out, selection, self.case.rt.tele,
                                   list(self.case.chains.values()), synthesized=True)
        self.assertFalse((self.case.out / "agent_answer.json").exists())

    def test_budget_limit_preserves_synthesis_calls(self):
        self.case.rt.budget.models = CONFIG.max_model_calls - CONFIG.meta_synthesis_reserve - 1
        self.case.rt.model.call.return_value = self.bad
        result = meta.arbitrate_legacy(self.case)
        self.assertEqual(len(result["propagation"]), 2)
        self.assertEqual(self.case.rt.budget.models, CONFIG.max_model_calls - CONFIG.meta_synthesis_reserve)
        for _ in range(CONFIG.synthesis_lookup_max):
            self.case.rt.budget.model("synthesis_lookup")
        with self.assertRaises(Limit):
            self.case.rt.budget.model("synthesis_lookup")
        self.case.rt.budget.enter_synthesis_final()
        for _ in range(1 + CONFIG.synthesis_final_max_retries):
            self.case.rt.budget.model("synthesis")
        self.assertEqual(self.case.rt.budget.models,
                         CONFIG.max_model_calls - CONFIG.meta_synthesis_reserve
                         + CONFIG.synthesis_lookup_max + 1 + CONFIG.synthesis_final_max_retries)

    def test_all_invalid_does_not_become_successful_empty_answer(self):
        a = {**self.good, "root_causes": [], "propagation": [{"from": "X", "to": "Y", "evidence_refs": ["ab"]}]}
        self.case.rt.model.call.return_value = a
        with self.assertRaises(StageFailure):
            meta.arbitrate_legacy(self.case)
        self.assertFalse((self.case.out / "stages/meta/selection.json").exists())

    def test_last_proposal_not_older_candidate_is_used(self):
        empty_invalid = {**self.good, "reason": "", "root_causes": [], "propagation": []}
        self.case.rt.model.call.side_effect = [self.bad] * 8 + [empty_invalid]
        with self.assertRaises(StageFailure):
            meta.arbitrate_legacy(self.case)

    def test_json_and_provider_failures_do_not_trigger_partial_selection(self):
        for error in [ValueError("invalid JSON"), RuntimeError("provider failed")]:
            with self.subTest(error=type(error).__name__):
                # 每个子测试使用新的会话，避免接口停止标志串到下一次。
                self.case.meta = Session(self.case.rt, "meta", "meta:arbitrate")
                self.case.rt.provider_stop.clear()
                self.case.rt.model.call.side_effect = [self.bad] + [error] * 9
                with self.assertRaises((StageFailure, RuntimeError)):
                    meta.arbitrate_legacy(self.case)
                self.assertFalse(self.case.meta_selection_report["used_partial_selection"])

    def test_followup_does_not_reset_content_failures_or_release_reserve(self):
        self.case.followup = Mock()
        followup = {"action": "followup", "reason": "Investigate", "question": "Specific gap", "target_type": "l1"}
        self.case.rt.model.call.side_effect = [self.bad, followup, self.good]
        meta.arbitrate_legacy(self.case)
        self.assertEqual(self.case.meta_selection_report["selection_failures"], 1)
        self.assertEqual(self.case.followup.call_args.kwargs["meta_calls_to_keep"], 8 + CONFIG.meta_synthesis_reserve)
        self.assertGreaterEqual(self.case.rt.budget.meta_left, CONFIG.meta_synthesis_reserve)
        b = Budget(CONFIG)
        self.assertTrue(b.allow_followup(11))
        self.assertGreaterEqual(b.meta_left, 11)
        b.models = CONFIG.max_model_calls - 4
        self.assertFalse(b.allow_followup(11))

    def test_budget_limit_after_followup_does_not_reuse_old_proposal(self):
        def finish_followup(*args, **kwargs):
            # 补查已经改变链版本；即使之后只剩综合额度，也不能拿旧提案生成答案。
            self.case.chains["C1"]["revision"] += 1
            self.case.rt.budget.models = CONFIG.max_model_calls - CONFIG.meta_synthesis_reserve

        self.case.followup = Mock(side_effect=finish_followup)
        self.case.rt.model.call.side_effect = [
            self.bad, {"action": "followup", "reason": "Check current revision"},
        ]
        with self.assertRaises(Limit):
            meta.arbitrate_legacy(self.case)
        self.assertFalse((self.case.out / "stages/meta/selection.json").exists())
        self.assertFalse(self.case.meta_selection_report["used_partial_selection"])

    def test_workflow_reports_partial_after_program_assembly(self):
        self.case.investigation_stops = [{"resolved": False, "status": "technical_failure"}]
        answer, prediction = export_selected_answer(
            self.case.out, self.good, self.case.rt.tele, list(self.case.chains.values()), synthesized=True)
        with unittest.mock.patch.object(meta, "arbitrate", return_value={}), \
             unittest.mock.patch.object(synthesis, "synthesize", return_value=(answer, prediction, {"status": "completed"})):
            report = self.case.run()
        self.assertEqual(report["status"], "partial")
        self.assertIsNotNone(report["answer"])
        self.assertTrue(all("claim" not in e for group in report["answer"].values()
                            for item in group for e in item["evidence"]))
        self.assertTrue((self.case.out / "prediction.json").exists())
        saved = json.loads((self.case.out / "stages/case_summary.json").read_text("utf-8"))
        self.assertEqual(saved["synthesis_report"]["status"], "completed")

    def test_progress_and_prompt_contracts(self):
        issue = meta.check_selection(self.case, self.bad)["issues"][0]
        event = {"kind": "meta_selection_issue", "stage": "meta", "issue": issue,
                 "reason": issue["message"], "case_handle": "fixture"}
        line = render_event(event, color=False, icons=False)
        self.assertIn("C2", line)
        self.assertIn("D → A", line)
        for role in ("seed", "l1", "l2", "meta"):
            system, user = prompt(role)
            self.assertIn("{{context}}", user)
            self.assertNotIn("{{fault_kind_options}}", system)

    def test_synthesis_lookup_uses_synthesis_prompt(self):
        self.assertEqual(prompt("synthesis_lookup"), prompt("synthesis"))

    def test_checkpoint_preserves_supported_edges_not_unreviewed_extension(self):
        root = {"service": "A", "fault_kind": "network_delay", "evidence_refs": ["root"]}
        old = chain("C1", ["A", "B", "C", "D", "E"], ["ab", "bc", "da", "ae"], root)
        current = copy.deepcopy(old)
        current.update(revision=2, nodes=["A", "B", "C", "D", "E", "F"], last_review=None)
        current["edges"].append({"from": "E", "to": "F", "evidence_refs": ["ef"]})
        current["submitted_checkpoint"] = {"chain": state.view(old), "review": old["last_review"]}
        self.case.chains = {"C1": current}
        selection = {"action": "select", "selected_chain_ids": ["C1"], "root_causes": [root],
                     "propagation": copy.deepcopy(old["edges"]), "reason": "只采用已审核的四条边"}
        self.assertTrue(meta.check_selection(self.case, selection)["valid"])
        added = {**selection, "propagation": [*selection["propagation"], current["edges"][-1]]}
        self.assertFalse(meta.check_selection(self.case, added)["valid"])
        self.case.investigate = Mock()
        chosen = meta.select(self.case, selection)
        answer, prediction = export_selected_answer(self.case.out, chosen, self.case.rt.tele,
                                                     [meta.submitted_view(current)], synthesized=True)
        with unittest.mock.patch.object(meta, "arbitrate", return_value={}), \
             unittest.mock.patch.object(synthesis, "synthesize", return_value=(answer, prediction, {"status": "completed"})):
            report = self.case.run()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["answer"]["root_causes"][0]["service"], "A")
        self.assertEqual(len(report["answer"]["propagation"]), 4)
        self.assertEqual(report["prediction"]["selected_chains"][0]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
