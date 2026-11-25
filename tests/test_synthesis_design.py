"""新仲裁与综合契约的固定输入检查；不调用外部模型。"""

import copy
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agents import meta, synthesis
from agents.context_transport import expand_context
from agents.loop import Invalid, Runtime, Session, StageFailure, prompt
from settings import CONFIG
from state import Budget, Limit, Records
from tools.telemetry import EvidenceValidationError


def trajectory_fixture():
    reviews = [
        {"status": "reviewed", "revision": 1, "review_id": "R1", "detail": "完整审核理由",
         "root_verdict": {"verdict": "contradict", "evidence_refs": ["E-1"], "reason": "根因反证"},
         "edge_verdicts": [{"verdict": "unknown", "from": "A", "to": "B",
                            "evidence_refs": ["E-1"], "reason": "边仍有疑问"}]},
        {"status": "technical_failure", "revision": 2, "review_id": "R2", "error": "审核失败"},
    ]
    chain = {"chain_id": "C1", "revision": 2, "reviews": reviews}
    categories = synthesis._review_categories(SimpleNamespace(ordered=lambda: [chain]))
    return {
        "真实链版本和审核": [
            {"chain": {"chain_id": "C1", "revision": revision, "root": {"service": "A"},
                       "seed": {"service": "A", "reason": "完整种子"},
                       "symptom": {"service": "B", "description": "完整症状"},
                       "target_symptom": {"service": "B", "detail": "目标症状原文"},
                       "edges": [{"from": "A", "to": "B", "evidence_refs": ["E-1"],
                                  "reason": "完整边理由"}], "detail": "完整版本" * revision},
             "reviews": [reviews[revision - 1]], "allowed_evidence_refs": ["E-1"],
             "current_revision": 2, "final_status": "submitted"}
            for revision in (1, 2)
        ],
        "审核按支持反对未确认和失败分类": categories,
        "事件按角色分类": {
            "L2AGENT": [{"seq": 2, "agent_id": "l2:C1", "event": "action_rejected",
                         "data": {"reason": "证据不足", "evidence_refs": ["E-1"]}}],
            "L1_AGENT": [{"seq": 1, "agent_id": "l1:C1", "event": "tool_result",
                         "data": {"tool": "query", "result": {"rows": [1], "truncated": False}}}],
        },
        "实际证据": {"E-1": {"kind": "trace", "rows": [{"value": "原始数据"}], "truncated": False}},
        "调查停止记录": [{"chain_id": "C1", "reason": "失败"}],
        "初始及补充候选": [{"round": 1, "mode": "initial", "detail": "完整轮次",
                     "seeds": [{"seed_id": "S1", "service": "A", "evidence_refs": ["E-1"],
                                "target_symptom": {"service": "B", "detail": "种子目标原文"},
                                "reason": "完整种子理由"}],
                     "symptoms": [{"service": "B", "description": "延迟", "evidence_refs": ["E-1"],
                                   "detail": "完整症状原文"}]}],
        "补查请求及实际返回": [{"request_id": "Q1", "request": "核对", "result": "未知"}],
        "META补查及实际返回": [{"chain_id": "C1", "request": "复核", "result": "反证",
                          "response": {"status": "completed", "detail": "完整回复"}}],
        "META结案": {"reason": "存在争议", "branch_assessments": [{"chain_id": "C1", "detail": "分支原文"}],
                   "confidence_scores": [{"chain_id": "C1", "conclusion_confidence": 0.5, "detail": "置信原文"}],
                   "open_issues": [{"chain_id": "C1", "detail": "未决原文"}]}, "轨迹说明": "顺序不是因果",
    }


class SynthesisDesignTests(unittest.TestCase):
    def test_meta_conclusion_and_followup_go_to_existing_owner(self):
        conclusion = {"action": "conclude", "branch_assessments": [], "confidence_scores": [],
                      "open_issues": [], "termination_reason": "没有可继续的补查"}
        with tempfile.TemporaryDirectory() as folder:
            session = Mock()
            session.seen = set()
            session.id = "meta:arbitrate"
            session.step.side_effect = [
                {"action": "followup", "target_type": "l1", "chain_id": "C1",
                 "question": "请核对新证据并交原审核者", "reason": "审核冲突"}, conclusion]
            case = SimpleNamespace(finalize_only=False, ordered=lambda: [], symptoms=[], rounds=[],
                                   followups=[], requests=[], investigation_stops=[], cfg=CONFIG,
                                   meta=session, chains={}, families={}, branch_frontiers={},
                                   out=Path(folder), followup=Mock(),
                                   rt=SimpleNamespace(records=Records(folder)))
            result = meta.arbitrate(case)
            self.assertEqual(result["termination_reason"], conclusion["termination_reason"])
            self.assertEqual(case.followup.call_args.args[0]["target_type"], "l1")
            self.assertEqual(case.followup.call_args.kwargs["meta_calls_to_keep"], 1 + CONFIG.meta_synthesis_reserve)
            self.assertNotIn("root_causes", result)

    def test_unmatched_review_is_not_assigned_to_another_version(self):
        trajectory = trajectory_fixture()
        item = {"chain_id": "C1", "revision": 9, "review_id": "R9",
                "object": "根因", "verdict": {"verdict": "contradict"}}
        trajectory["审核按支持反对未确认和失败分类"]["反对"].append(item)
        working_set, index = synthesis._working_set(trajectory)
        unmatched = working_set["未匹配版本的审核"]
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0]["revision"], 9)
        self.assertEqual(index[unmatched[0]["object_id"]]["object"], item)
        self.assertNotIn(9, [r["revision"] for v in working_set["真实链版本和审核"]
                             for r in v["reviews"]])

    def test_synthesis_rejects_missing_or_incomplete_source_and_allows_empty(self):
        case = SimpleNamespace(rt=SimpleNamespace(names={"A"}))
        empty = {"root_causes": [], "propagation": [], "reason": "证据不足"}
        _, errors, _ = synthesis.check_draft(case, empty, [], case_evidence_refs=[])
        self.assertEqual(errors, [])
        snapshot = trajectory_fixture()["真实链版本和审核"][0]
        for chain_id, revision in (("missing", 1), ("C1", 9), ("C1", None), (None, 1)):
            with self.subTest(chain_id=chain_id, revision=revision):
                draft = {**empty, "root_causes": [{
                    "service": "A", "fault_kind": "network_delay",
                    "source_chain_id": chain_id, "source_revision": revision,
                    "evidence_refs": ["E-1"], "reason": "来源待核对",
                }]}
                _, errors, _ = synthesis.check_draft(case, draft, [snapshot], case_evidence_refs=["E-1"])
                self.assertEqual([error["path"] for error in errors], ["root_causes[0]"])
                self.assertIn("来源链或版本不存在", errors[0]["message"])

    def test_synthesis_accepts_traceable_unknown_root_without_propagation(self):
        snapshot = trajectory_fixture()["真实链版本和审核"][0]
        snapshot["chain"]["root"]["fault_kind"] = "network_delay"
        snapshot["reviews"][0]["root_verdict"]["verdict"] = "unknown"
        rt = SimpleNamespace(names={"A", "B"}, refs=Mock(return_value=["E-1"]), tele=Mock())
        rt.tele.validated_evidence.return_value = ({}, "", {"abnormal_metrics"})
        root = {"service": "A", "fault_kind": "network_delay", "source_chain_id": "C1",
                "source_revision": 1, "evidence_refs": ["E-1"],
                "reason": "A 的延迟最吻合症状，但尚不能排除上游负载变化"}
        draft = {"root_causes": [root], "propagation": [], "reason": "A 是当前最有依据的候选"}

        selection, errors, _ = synthesis.check_draft(
            SimpleNamespace(rt=rt), draft, [snapshot], case_evidence_refs=["E-1"],
        )

        self.assertEqual(errors, [])
        self.assertEqual(selection["root_causes"], [{"service": "A", "fault_kind": "network_delay",
                                                     "evidence_refs": ["E-1"]}])
        self.assertEqual(selection["propagation"], [])
        self.assertFalse(selection["root_provenance"][0]["independent_inference"])

    def test_synthesis_uses_case_evidence_for_independent_root(self):
        snapshots = trajectory_fixture()["真实链版本和审核"]
        snapshots[0]["chain"]["root"] = {"service": "A", "fault_kind": "network_loss"}
        snapshots[1]["allowed_evidence_refs"] = ["E-2"]
        rt = Runtime.__new__(Runtime)
        rt.names = {"A", "B"}
        rt.tele = Mock()
        rt.tele.evidence.return_value = {"sql": "SELECT 1", "rows": [{"value": 1}]}
        rt.tele.validated_evidence.return_value = ({}, "", {"abnormal_metrics"})
        case = SimpleNamespace(rt=rt)
        root = {
            "service": "A", "fault_kind": "network_delay", "evidence_refs": ["E-1", "E-2"],
            "reason": "结合两处观测，延迟更可能，仍未排除负载变化",
        }
        for chain_id, revision in (("C1", 1), (None, None)):
            with self.subTest(chain_id=chain_id):
                draft = {
                    "root_causes": [{**root, "source_chain_id": chain_id, "source_revision": revision}],
                    "propagation": [], "reason": "整合两个版本的证据",
                }
                selection, errors, chains = synthesis.check_draft(
                    case, draft, snapshots, case_evidence_refs=["E-1", "E-2"],
                )
                self.assertEqual(errors, [])
                self.assertEqual(selection["root_causes"][0]["evidence_refs"], ["E-1", "E-2"])
                self.assertTrue(selection["root_provenance"][0]["independent_inference"])
                self.assertEqual(selection["root_provenance"][0]["source_chain_id"], chain_id)
                self.assertEqual([chain["chain_id"] for chain in chains], ["C1"] if chain_id else [])

                _, errors, _ = synthesis.check_draft(case, draft, snapshots, case_evidence_refs=["E-1"])
                self.assertEqual([error["path"] for error in errors], ["root_causes[0].evidence_refs"])
                self.assertIn("E-2", errors[0]["message"])

    def test_synthesis_feedback_names_wrong_direction_and_missing_prefix(self):
        snapshot = trajectory_fixture()["真实链版本和审核"][0]
        snapshot["chain"]["edges"].append({"from": "B", "to": "C", "evidence_refs": ["E-1"]})
        rt = SimpleNamespace(names={"A", "B", "C"}, refs=Mock(return_value=["E-1"]), tele=Mock())
        rt.tele.validated_evidence.return_value = ({}, "", {"abnormal_traces"})
        rt.tele.evidence.return_value = {"kind": "trace"}
        source = {"source_chain_id": "C1", "source_revision": 1, "evidence_refs": ["E-1"], "reason": "调用异常"}
        draft = {"root_causes": [], "propagation": [
            {"from": "B", "to": "A", **source}, {"from": "B", "to": "C", **source},
        ], "reason": "核对传播方向"}

        _, errors, _ = synthesis.check_draft(
            SimpleNamespace(rt=rt), draft, [snapshot], case_evidence_refs=["E-1"],
        )

        self.assertEqual([error["path"] for error in errors], ["propagation[0]", "propagation[1]"])
        self.assertIn("C1 第 1 版没有 B → A", errors[0]["message"])
        self.assertIn("缺少连续前段 A → B", errors[1]["message"])

    def test_synthesis_exports_independent_root_after_correcting_empty_replay(self):
        trajectory = trajectory_fixture()
        snapshot = trajectory["真实链版本和审核"][0]
        snapshot["chain"]["root"] = None
        snapshot["allowed_evidence_refs"].append("E-2")
        trajectory["实际证据"]["E-2"] = {"kind": "metric", "rows": [{"value": 2}]}
        rt = SimpleNamespace(names={"A", "B"}, records=Mock(), tele=Mock(), budget=Mock(),
                             refs=Mock(side_effect=lambda refs, _: refs))
        rt.budget.can_synthesis_continue.return_value = True
        rt.tele.service_names.return_value = rt.names
        rt.tele.validated_evidence.return_value = ({}, "", {"abnormal_metrics"})
        rt.tele.verify.side_effect = [
            {"kind": "metric", "sql": "SELECT 1", "rows": []},
            {"kind": "metric", "sql": "SELECT 2", "rows": [{"value": 2}]},
        ]
        case = SimpleNamespace(cfg=CONFIG, out=Path("."), rt=rt)
        lookup, final = Mock(id="synthesis:lookup"), Mock(id="synthesis:final")
        lookup.synthesis_size.return_value = final.synthesis_size.return_value = {"fits": True}
        root = {"service": "A", "fault_kind": "network_delay", "source_chain_id": "C1",
                "source_revision": 1, "evidence_refs": ["E-1"], "reason": "A 的延迟吻合症状"}
        draft = {"root_causes": [root], "propagation": [], "reason": "A 是当前最有依据的候选"}
        corrected = {**draft, "root_causes": [{**root, "evidence_refs": ["E-2"]}]}
        lookup.step.side_effect = [draft, corrected]

        with patch.object(synthesis, "collect_trajectory", return_value=trajectory), \
             patch.object(synthesis, "Session", side_effect=[lookup, final]), \
             patch("output_contract._save_replay"), patch("output_contract._save_outputs", return_value={}) as save:
            answer, _, report = synthesis.synthesize(case, {})

        feedback = lookup.step.call_args_list[1].args[0]["validation_feedback"]
        self.assertEqual(feedback[0]["path"], "root_causes[0].evidence_refs")
        self.assertIn("E-1", feedback[0]["message"])
        self.assertEqual(lookup.step.call_args_list[1].args[0]["previous_answer"], draft)
        self.assertEqual(report["formal_attempts"], 2)
        self.assertEqual(answer["root_causes"][0]["fault_kind"], "network_delay")
        self.assertEqual(answer["root_causes"][0]["evidence"][0]["sql"], "SELECT 2")
        rt.records.save.assert_any_call(
            "synthesis/round_01/validation_02.json",
            {"valid": True, "errors": [], "root_provenance": [{
                "service": "A", "fault_kind": "network_delay", "evidence_refs": ["E-2"],
                "source_chain_id": "C1", "source_revision": 1, "independent_inference": True,
            }]},
        )
        save.assert_called_once()

    def test_synthesis_does_not_retry_invalid_saved_sql(self):
        trajectory = trajectory_fixture()
        trajectory["真实链版本和审核"][0]["chain"]["root"]["fault_kind"] = "network_delay"
        rt = SimpleNamespace(names={"A", "B"}, records=Mock(), tele=Mock(), budget=Mock(),
                             refs=Mock(return_value=["E-1"]))
        rt.budget.can_synthesis_continue.return_value = True
        rt.tele.service_names.return_value = rt.names
        rt.tele.validated_evidence.return_value = ({}, "", {"abnormal_metrics"})
        rt.tele.verify.side_effect = EvidenceValidationError("E-1", "保存的 SQL 已损坏")
        case = SimpleNamespace(cfg=CONFIG, out=Path("."), rt=rt)
        lookup, final = Mock(), Mock()
        lookup.synthesis_size.return_value = {"fits": True}
        lookup.step.return_value = {
            "root_causes": [{"service": "A", "fault_kind": "network_delay", "source_chain_id": "C1",
                             "source_revision": 1, "evidence_refs": ["E-1"], "reason": "A 的延迟吻合症状"}],
            "propagation": [], "reason": "A 是当前最有依据的候选",
        }
        with patch.object(synthesis, "collect_trajectory", return_value=trajectory), \
             patch.object(synthesis, "Session", side_effect=[lookup, final]), \
             patch("output_contract._save_outputs") as save:
            with self.assertRaises(EvidenceValidationError):
                synthesis.synthesize(case, {})
        final.step.assert_not_called()
        save.assert_not_called()

    def test_lookup_and_final_calls_are_reserved_inside_total_budget(self):
        # 测试最紧的合法预留边界；回查/正式次数沿用当前设置，不假定旧的 8+4。
        cfg = replace(CONFIG, meta_synthesis_reserve=CONFIG.synthesis_lookup_max + CONFIG.synthesis_max_attempts)
        self.assertGreaterEqual(cfg.meta_reserve, cfg.meta_synthesis_reserve + 1)
        budget = Budget(cfg)
        budget.models = cfg.max_model_calls - cfg.meta_synthesis_reserve - 1
        self.assertTrue(budget.model("meta"))
        for _ in range(cfg.synthesis_lookup_max):
            budget.model("synthesis_lookup")
        with self.assertRaises(Limit):
            budget.model("synthesis_lookup")
        self.assertEqual(budget.synthesis_lookups, cfg.synthesis_lookup_max)
        budget.enter_synthesis_final()
        for _ in range(1 + cfg.synthesis_final_max_retries):
            budget.model("synthesis")
        self.assertEqual(budget.models, cfg.max_model_calls - cfg.synthesis_lookup_max +
                         1 + cfg.synthesis_final_max_retries)

    def test_lookup_preserves_final_calls_and_chain_holds(self):
        budget = Budget(CONFIG)
        budget.holds["C1"] = {"audit", "response"}
        budget.models = CONFIG.max_model_calls - (1 + CONFIG.synthesis_final_max_retries) - 3
        budget.model("synthesis_lookup")
        with self.assertRaises(Limit):
            budget.model("synthesis_lookup")
        budget.enter_synthesis_final()
        for _ in range(1 + CONFIG.synthesis_final_max_retries):
            budget.model("synthesis")
        with self.assertRaises(Limit):
            budget.model("synthesis")
        self.assertEqual(budget.holds["C1"], {"audit", "response"})

    def test_generation_attempts_do_not_nest_json_retries(self):
        case = SimpleNamespace(cfg=CONFIG,
                               rt=SimpleNamespace(handle="fixture", records=Mock(), budget=Mock()))
        lookup, final = Mock(), Mock()
        lookup.synthesis_size.return_value = final.synthesis_size.return_value = {"fits": True}
        lookup.step.return_value = {"root_causes": [], "propagation": [], "reason": ""}
        final.step.side_effect = Invalid("JSON 格式错误")
        with patch.object(synthesis, "collect_trajectory", return_value=trajectory_fixture()), \
             patch.object(synthesis, "Session", side_effect=[lookup, final]):
            with self.assertRaisesRegex(StageFailure, f"{1 + CONFIG.synthesis_final_max_retries} 次"):
                synthesis.synthesize(case, {}, final_mode=True)
        self.assertEqual(lookup.step.call_count, 1)
        self.assertEqual(final.step.call_count, CONFIG.synthesis_final_max_retries)

    def test_oversized_request_is_rejected_before_consuming_model_budget(self):
        from dataclasses import replace
        cfg = replace(CONFIG, synthesis_context_limit=1000, synthesis_output_reserve=100,
                      synthesis_context_margin=100)
        runtime = SimpleNamespace(cfg=cfg, budget=Budget(cfg), handle="test", model=Mock(),
                                  provider_stop=Mock(is_set=Mock(return_value=False)), records=Mock())
        session = Session(runtime, "synthesis", "synthesis:final")
        with self.assertRaises(StageFailure):
            session.step({"trajectory": "大" * 2000}, [], synthesis=True)
        self.assertEqual(runtime.budget.models, 0)
        runtime.model.call.assert_not_called()

    def test_working_set_preserves_versions_reviews_and_original_objects(self):
        trajectory = trajectory_fixture()
        original = copy.deepcopy(trajectory)
        working_set, index = synthesis._working_set(trajectory)
        versions = working_set["真实链版本和审核"]
        self.assertEqual([v["revision"] for v in versions], [1, 2])
        for i, version in enumerate(versions):
            source = trajectory["真实链版本和审核"][i]
            prefix = f"V-C1-{i + 1}"
            self.assertEqual(version["object_id"], prefix)
            self.assertNotIn("chain", version)
            self.assertNotIn("detail", version)
            self.assertEqual(index[prefix]["object"], source["chain"])
            for key in ("allowed_evidence_refs", "current_revision", "final_status"):
                self.assertEqual(version[key], source[key])
            for key in ("root", "seed", "symptom", "target_symptom"):
                oid = f"{prefix}-{key.upper()}"
                self.assertEqual(version[f"{key}_object_id"], oid)
                self.assertEqual(index[oid]["object"], source["chain"][key])
            edge = version["edges"][0]
            self.assertEqual(edge, {"object_id": f"{prefix}-EDGE001", "from": "A", "to": "B",
                                    "evidence_refs": ["E-1"]})
            self.assertEqual(index[edge["object_id"]]["object"], source["chain"]["edges"][0])
            review = version["reviews"][0]
            self.assertEqual(review["object_id"], f"{prefix}-R001")
            self.assertEqual(review["revision"], i + 1)
            self.assertEqual(index[review["object_id"]]["object"], source["reviews"][0])
            self.assertNotIn("detail", review)
            self.assertNotIn("error", review)
        self.assertEqual(versions[0]["reviews"][0]["legacy_root_verdict"],
                         {"verdict": "contradict", "evidence_refs": ["E-1"], "reason": "根因反证"})
        self.assertEqual(versions[0]["reviews"][0]["edge_verdicts"],
                         [{"from": "A", "to": "B", "verdict": "unknown", "evidence_refs": ["E-1"],
                           "reason": "边仍有疑问", "direction_status": "legacy_unchecked"}])
        self.assertEqual(versions[1]["reviews"][0]["status"], "technical_failure")

        round_entry = working_set["初始及补充候选"][0]
        seed = round_entry["seeds"][0]
        symptom = working_set["seed症状"][0]
        self.assertEqual(round_entry["object_id"], "ROUND-000001")
        self.assertNotIn("detail", round_entry)
        self.assertEqual(seed["object_id"], "ROUND-000001-SEED001")
        self.assertEqual(seed["target_symptom_object_id"], "ROUND-000001-SEED001-TARGET")
        self.assertNotIn("reason", seed)
        self.assertEqual(round_entry["symptom_object_ids"], ["ROUND-000001-SYM001"])
        self.assertEqual(symptom["object_id"], round_entry["symptom_object_ids"][0])
        self.assertEqual(symptom["round_object_id"], round_entry["object_id"])
        self.assertEqual(symptom["description"], "延迟")
        self.assertNotIn("detail", symptom)
        self.assertEqual(index[seed["object_id"]]["object"], trajectory["初始及补充候选"][0]["seeds"][0])
        self.assertEqual(index[seed["target_symptom_object_id"]]["object"],
                         trajectory["初始及补充候选"][0]["seeds"][0]["target_symptom"])
        self.assertEqual(index[symptom["object_id"]]["object"], trajectory["初始及补充候选"][0]["symptoms"][0])
        self.assertEqual([e["seq"] for e in working_set["事件索引"]], [1, 2])
        self.assertEqual(working_set["事件索引"][0]["row_count"], 1)
        self.assertEqual(working_set["证据索引"], [{"object_id": "E-1", "kind": "trace",
                                                 "row_count": 1, "truncated": False}])
        for name, oid in (("调查停止记录", "STOP-000001"), ("初始及补充候选", "ROUND-000001"),
                          ("补查请求及实际返回", "REQ-000001"), ("META补查及实际返回", "FOLLOWUP-000001")):
            entry = working_set[name][0]
            self.assertEqual(entry["object_id"], oid)
            self.assertEqual(index[oid]["object"], trajectory[name][0])
        self.assertEqual(working_set["META补查及实际返回"][0]["response"], {"status": "completed"})
        meta = working_set["META结案"]
        self.assertEqual(meta["object_id"], "META-CONCLUSION-R001")
        for key in ("branch_assessments", "confidence_scores", "open_issues"):
            item = meta[key][0]
            self.assertEqual(item["object_id"], f"META-CONCLUSION-R001-{key.upper()}-000001")
            self.assertNotIn("detail", item)
            self.assertEqual(index[item["object_id"]]["object"], trajectory["META结案"][key][0])
        self.assertEqual(index["META-CONCLUSION-R001"]["object"], trajectory["META结案"])
        expected_ids = {"EV-00000001", "EV-00000002", "E-1", "ROUND-000001",
                        "ROUND-000001-SEED001", "ROUND-000001-SEED001-TARGET", "ROUND-000001-SYM001",
                        "STOP-000001", "REQ-000001", "FOLLOWUP-000001", "META-CONCLUSION-R001",
                        "META-CONCLUSION-R001-BRANCH_ASSESSMENTS-000001",
                        "META-CONCLUSION-R001-CONFIDENCE_SCORES-000001",
                        "META-CONCLUSION-R001-OPEN_ISSUES-000001"}
        for prefix in ("V-C1-1", "V-C1-2"):
            expected_ids.update({prefix, f"{prefix}-R001", f"{prefix}-EDGE001",
                                 *(f"{prefix}-{key}" for key in ("ROOT", "SEED", "SYMPTOM", "TARGET_SYMPTOM"))})
        self.assertEqual(set(index), expected_ids)
        for entry in index.values():
            value = trajectory
            for part in entry["source"].split("/")[1:]:
                value = value[int(part)] if isinstance(value, list) else value[part]
            self.assertEqual(entry["object"], value)
        self.assertEqual(index["EV-00000002"]["object"], trajectory["事件按角色分类"]["L2AGENT"][0])
        self.assertEqual(index["E-1"]["object"], trajectory["实际证据"]["E-1"])
        self.assertEqual(trajectory, original)

    def test_trajectory_summary_counts_applied_actions_and_distinct_reviews(self):
        trajectory = trajectory_fixture()
        versions = trajectory["真实链版本和审核"]
        versions[0]["chain"]["nodes"] = ["A", "B"]
        versions[1]["chain"]["nodes"] = ["A"]
        first_review = versions[0]["reviews"][0]
        first_review.update(chain_id="C1", recommendation="backtrack", reason="存在根因反证，请回退补查。")
        failed_review = versions[1]["reviews"][0]
        failed_review.update(chain_id="C1", reason="审核接口失败")
        events = [
            {"seq": 1, "event": "model_action", "data": {"action": "fork", "reason": "尝试分支"}},
            {"seq": 2, "event": "action_rejected", "data": "分支校验失败"},
            {"seq": 3, "event": "decision_applied", "data": {
                "chain_id": "C1", "action": "extend", "input_revision": 0, "output_revision": 1,
                "reason": "调查影响到 B"}},
            {"seq": 4, "event": "review_requested", "data": {"chain_id": "C1", "revision": 1}},
            {"seq": 5, "event": "review_received", "data": {
                "chain_id": "C1", "revision": 1, "review": first_review}},
            {"seq": 6, "event": "decision_applied", "data": {
                "chain_id": "C1", "action": "review", "input_revision": 1, "output_revision": 1}},
            {"seq": 7, "event": "decision_applied", "data": {
                "chain_id": "C1", "action": "backtrack", "input_revision": 1, "output_revision": 2,
                "reason": "根据反证回退至 A", "review_received": "R1"}},
            {"seq": 8, "event": "review_requested", "data": {"chain_id": "C1", "revision": 2}},
            {"seq": 9, "event": "review_received", "data": failed_review},
            {"seq": 10, "event": "decision_applied", "data": {
                "chain_id": "C1", "action": "review", "input_revision": 2, "output_revision": 2,
                "review_received": "R1", "reason": "按 R1 的建议回退后重新送审"}},
            {"seq": 11, "event": "review_requested", "data": {"chain_id": "C1", "revision": 2}},
        ]
        trajectory["事件按角色分类"] = {"L1_AGENT": [
            {"agent_id": "l1:S1:C1", **event} for event in events
        ]}
        original = copy.deepcopy(trajectory)
        working_set, index = synthesis._working_set(trajectory)
        summaries = working_set["trajectory_summary"]["chains"]
        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary["action_counts"]["L1"], {
            "choose_symptom": 0, "extend": 1, "fork": 0, "backtrack": 1,
            "reseed": 0, "review": 3, "submit": 0, "stop": 0,
        })
        self.assertEqual(summary["action_counts"]["L2"], {
            "requested": 3, "completed": 1, "failed": 1, "unfinished": 1,
        })
        steps = summary["key_steps"]
        self.assertEqual([step["seq"] for step in steps], [3, 5, 7, 8, 9])
        self.assertEqual(steps[1]["legacy_root_verdict"], "contradict")
        self.assertEqual(steps[1]["recommendation"], "backtrack")
        self.assertEqual(steps[1]["revision"], 1)
        self.assertEqual(steps[2]["review_received"], "R1")
        self.assertEqual(steps[2]["nodes_after"], ["A"])
        self.assertEqual(steps[3]["action"], "review")
        self.assertEqual(steps[3]["review_received"], "R1")
        self.assertEqual(steps[3]["source_object_ids"], ["EV-00000010", "EV-00000008"])
        self.assertEqual(steps[4]["status"], "technical_failure")
        self.assertIsNone(steps[4]["component_verdict"])
        for step in steps:
            for object_id in step["source_object_ids"]:
                self.assertIn(object_id, index)
        self.assertEqual(trajectory, original)

    def test_trajectory_summary_links_fork_and_reseed_without_inheriting_counts(self):
        trajectory = trajectory_fixture()
        versions = trajectory["真实链版本和审核"]
        for version in versions:
            version["chain"]["seed"]["seed_id"] = "S1"
        fork = copy.deepcopy(versions[0])
        fork.update(reviews=[], current_revision=0, final_status="submitted")
        fork["chain"].update(chain_id="C2", revision=0, parent_chain_id="C1", parent_revision=1,
                             nodes=["A", "C"])
        reseed = copy.deepcopy(fork)
        reseed["chain"].update(chain_id="C3", parent_chain_id=None, parent_revision=None,
                               seed={"seed_id": "S3", "service": "D"}, nodes=["D"])
        versions.extend([fork, reseed])
        trajectory["补查请求及实际返回"] = [{
            "chain_id": "C1", "request_id": "Q1", "reason": "应从 D 调查",
            "response": {"status": "new_candidates", "new_seed_ids": ["S3"], "reason": "发现 D 的新线索"},
        }]
        trajectory["事件按角色分类"] = {"L1_AGENT": [
            {"seq": 1, "agent_id": "l1:S1:C1", "event": "decision_applied", "data": {
                "chain_id": "C1", "action": "fork", "input_revision": 1, "output_revision": 1,
                "child_chain_id": "C2", "reason": "核查另一条传播边"}},
            {"seq": 2, "agent_id": "l1:S1:C1", "event": "decision_applied", "data": {
                "chain_id": "C1", "action": "reseed", "input_revision": 2, "output_revision": 2,
                "reason": "应从 D 调查"}},
            {"seq": 3, "agent_id": "C1", "event": "seed_response", "data": {
                "request_id": "Q1", "status": "new_candidates", "new_seed_ids": ["S3"]}},
            {"seq": 4, "agent_id": "l1:S3:C3", "event": "decision_applied", "data": {
                "chain_id": "C3", "action": "stop", "input_revision": 0, "output_revision": 0,
                "reason": "新候选仍缺证据"}},
        ]}
        working_set, index = synthesis._working_set(trajectory)
        chains = {chain["chain_id"]: chain for chain in working_set["trajectory_summary"]["chains"]}
        self.assertEqual(chains["C1"]["action_counts"]["L1"]["fork"], 1)
        self.assertEqual(chains["C1"]["action_counts"]["L1"]["reseed"], 1)
        self.assertEqual(chains["C1"]["origin"]["type"], "initial_seed")
        self.assertEqual(chains["C2"]["origin"]["type"], "fork")
        self.assertEqual(chains["C2"]["origin"]["parent_chain_id"], "C1")
        self.assertEqual(chains["C2"]["origin"]["parent_revision"], 1)
        self.assertEqual(chains["C3"]["action_counts"]["L1"]["reseed"], 0)
        origin = chains["C3"]["origin"]
        self.assertEqual(origin["type"], "reseed")
        self.assertEqual(origin["requests"][0]["source_chain_id"], "C1")
        self.assertEqual(origin["requests"][0]["request_id"], "Q1")
        self.assertEqual(index[origin["requests"][0]["source_object_ids"][0]]["object"],
                         trajectory["补查请求及实际返回"][0])
        response = next(step for step in chains["C1"]["key_steps"] if step["agent"] == "Seed")
        self.assertEqual((response["seq"], response["status"], response["new_seed_ids"]),
                         (3, "new_candidates", ["S3"]))

    def test_trajectory_summary_meta_counts_dispatches_and_records_returns(self):
        for concluded in (False, True):
            with self.subTest(concluded=concluded):
                trajectory = trajectory_fixture()
                tickets = [
                    {"target_type": "l1", "chain_id": "C1", "question": "核对反证", "reason": "审核分歧",
                     "returned_status": "submitted", "returned_revision": 2},
                    {"target_type": "seed", "chain_id": None, "question": "寻找新起点", "reason": "现有链不足",
                     "response": {"status": "new_candidates", "new_seed_ids": ["S3"], "reason": "新增 D"}},
                    {"target_type": "l1", "chain_id": "C1", "question": "进一步核对", "reason": "尚有疑点"},
                ]
                trajectory["META补查及实际返回"] = tickets
                events = []
                for seq, ticket in ((1, tickets[0]), (3, tickets[1]), (5, tickets[2])):
                    events.append({"seq": seq, "event": "followup_dispatched", "data": {
                        key: ticket[key] for key in ("target_type", "chain_id", "question", "reason")
                    }})
                    if seq < 5:
                        events.append({"seq": seq + 1, "event": "followup_returned", "data": ticket})
                events.extend([
                    {"seq": 6, "event": "model_action", "data": {
                        "action": "followup", "target_type": "seed", "question": "被拒绝的提议"}},
                    {"seq": 7, "event": "action_rejected", "data": "预算不足"},
                ])
                conclusion = {"termination_reason": "已有依据足够", "branch_assessments": [],
                              "confidence_scores": [], "open_issues": []}
                if not concluded:
                    conclusion = {"status": "未完成", "failure": {"reason": "接口失败"},
                                  "termination_reason": "接口失败"}
                trajectory["META结案"] = conclusion
                events.append({"seq": 8, "event": "concluded" if concluded else "arbitration_failed",
                               "data": conclusion})
                trajectory["事件按角色分类"] = {"META_AGENT": [
                    {"agent_id": "meta:arbitrate", **event} for event in events
                ]}
                working_set, index = synthesis._working_set(trajectory)
                meta_summary = working_set["trajectory_summary"]["meta"]
                self.assertEqual(meta_summary["action_counts"], {
                    "followup_l1": 2, "followup_seed": 1, "conclude": int(concluded),
                })
                steps = meta_summary["key_steps"]
                self.assertEqual([step["seq"] for step in steps], [1, 3, 5, 8])
                self.assertEqual((steps[0]["returned_status"], steps[0]["returned_revision"]), ("submitted", 2))
                self.assertEqual(steps[1]["new_seed_ids"], ["S3"])
                self.assertEqual(steps[1]["response"]["summary"], "新增 D")
                self.assertEqual(steps[2]["returned_status"], "unfinished")
                self.assertEqual(steps[3]["action"], "conclude" if concluded else "arbitration_failed")
                for step in steps:
                    for object_id in step["source_object_ids"]:
                        self.assertIn(object_id, index)

    def test_trajectory_summary_limits_excerpts_and_preserves_legacy_sources(self):
        trajectory = trajectory_fixture()
        full_reason = "存在反证，需要继续核对。" * 40
        trajectory["真实链版本和审核"][0]["reviews"][0]["reason"] = full_reason
        original = copy.deepcopy(trajectory)
        working_set, index = synthesis._working_set(trajectory)
        chain = working_set["trajectory_summary"]["chains"][0]
        self.assertIsNone(chain["action_counts"]["L1"]["fork"])
        self.assertEqual(chain["action_counts"]["L2"], {
            "requested": 2, "completed": 1, "failed": 1, "unfinished": 0,
        })
        review = next(step for step in chain["key_steps"] if step.get("review_id") == "R1")
        self.assertIsNone(review["seq"])
        self.assertTrue(review["summary_truncated"])
        self.assertLessEqual(len(review["summary"]), 160)
        self.assertEqual(index[review["source_object_ids"][0]]["object"]["reason"], full_reason)
        self.assertEqual(trajectory, original)
        for text in ("", "短说明", "字" * 160, "字" * 161):
            excerpt = synthesis._brief_reason(text)
            self.assertLessEqual(len(excerpt["summary"]), 160)
            self.assertEqual(excerpt["summary_truncated"], len(text) > 160)
        empty = {**trajectory, "真实链版本和审核": [], "事件按角色分类": {},
                 "补查请求及实际返回": [], "META补查及实际返回": [], "META结案": {}}
        empty_working_set, _ = synthesis._working_set(empty)
        self.assertEqual(empty_working_set["trajectory_summary"], {
            "chains": [], "meta": {"action_counts": {"followup_l1": 0, "followup_seed": 0, "conclude": 0},
                                    "key_steps": []},
        })

    def test_trajectory_summary_submission_identifies_the_reviewed_version(self):
        trajectory = trajectory_fixture()
        trajectory["真实链版本和审核"][0]["chain"]["nodes"] = ["A", "B"]
        trajectory["真实链版本和审核"][1]["chain"]["nodes"] = ["A", "B", "C"]
        trajectory["事件按角色分类"] = {"L1_AGENT": [{
            "seq": 1, "agent_id": "l1:S1:C1", "event": "decision_applied", "data": {
                "chain_id": "C1", "action": "submit", "input_revision": 2, "output_revision": 2,
                "submitted_revision": 1, "unreviewed_revision": 2, "review_received": "R1",
                "reason": "保留已审核版本并说明新增边未完成审核",
            },
        }]}
        working_set, _ = synthesis._working_set(trajectory)
        chain = working_set["trajectory_summary"]["chains"][0]
        submit = next(step for step in chain["key_steps"] if step["action"] == "submit")
        self.assertEqual(chain["current_revision"], 2)
        self.assertEqual(submit["submitted_revision"], 1)
        self.assertEqual(submit["unreviewed_revision"], 2)
        self.assertEqual(submit["nodes_after"], ["A", "B"])

    def test_lookup_returns_exact_originals_once_and_reports_unknown_ids(self):
        trajectory = trajectory_fixture()
        case = SimpleNamespace(cfg=CONFIG, out=Path("."), chains={},
                               rt=SimpleNamespace(records=Mock(), tele=Mock(), budget=Mock()))
        lookup, final = Mock(id="synthesis:lookup"), Mock(id="synthesis:final")
        lookup.synthesis_size.return_value = final.synthesis_size.return_value = {"fits": True}
        request = lambda *ids: {"request_original_objects": list(ids), "reason": "核对原文"}
        lookup.step.side_effect = [request("V-C1-1", "E-1", "V-C1-1"),
                                   request("EV-00000002"), request("missing"),
                                   {"request_meta_review": {"question": "检查候选", "checks_already_done": "已查原文",
                                    "expected_change": "区分原因", "chain_id": None, "evidence_refs": []}, "reason": "需补查"}]
        with patch.object(synthesis, "collect_trajectory", return_value=trajectory), \
             patch.object(synthesis, "Session", side_effect=[lookup, final]), \
             patch.object(synthesis, "export_selected_answer", return_value=({}, {})):
            _, _, report = synthesis.synthesize(case, {}, meta_returns_left=3)
        contexts = [call.args[0] for call in lookup.step.call_args_list]
        self.assertIn("trajectory_summary", contexts[0]["working_set"])
        self.assertEqual([obj["object_id"] for obj in contexts[1]["original_objects"]], ["V-C1-1", "E-1"])
        self.assertEqual(contexts[1]["original_objects"][0],
                         {"object_id": "V-C1-1", "source": "/真实链版本和审核/0/chain",
                          "object": trajectory["真实链版本和审核"][0]["chain"]})
        self.assertEqual(contexts[1]["original_objects"][1]["object"], trajectory["实际证据"]["E-1"])
        self.assertEqual([obj["object_id"] for obj in contexts[2]["original_objects"]], ["EV-00000002"])
        self.assertEqual(contexts[3]["original_objects"], [])
        self.assertIn("未知对象 ID", contexts[3]["validation_feedback"][0]["message"])
        self.assertEqual((report["status"], report["lookup_calls"], report["formal_attempts"]),
                         ("meta_review_requested", 4, 0))
        final.step.assert_not_called()

    def test_lookup_rejects_malformed_requests_with_feedback(self):
        case = SimpleNamespace(cfg=CONFIG, out=Path("."), chains={},
                               rt=SimpleNamespace(records=Mock(), tele=Mock(), budget=Mock()))
        lookup, final = Mock(id="synthesis:lookup"), Mock(id="synthesis:final")
        lookup.synthesis_size.return_value = final.synthesis_size.return_value = {"fits": True}
        lookup.step.side_effect = [
            {"request_original_objects": [], "reason": "核对"},
            {"request_original_objects": ["E-1"], "reason": ""},
            {"request_original_objects": ["E-1", 1], "reason": "核对"},
            {"request_original_objects": ["E-1"], "reason": "核对", "extra": True},
            {"request_meta_review": {"question": "检查候选", "checks_already_done": "已查原文",
             "expected_change": "区分原因", "chain_id": None, "evidence_refs": []}, "reason": "需补查"},
        ]
        with patch.object(synthesis, "collect_trajectory", return_value=trajectory_fixture()), \
             patch.object(synthesis, "Session", side_effect=[lookup, final]), \
             patch.object(synthesis, "export_selected_answer", return_value=({}, {})):
            synthesis.synthesize(case, {}, meta_returns_left=3)
        contexts = [call.args[0] for call in lookup.step.call_args_list]
        for context in contexts[1:]:
            self.assertEqual(context["original_objects"], [])
            self.assertIn("非空对象编号列表", context["validation_feedback"][0]["message"])

    def test_lookup_rejects_single_and_combined_oversize_without_truncation(self):
        trajectory = trajectory_fixture()
        case = SimpleNamespace(cfg=CONFIG, out=Path("."), chains={},
                               rt=SimpleNamespace(records=Mock(), tele=Mock(), budget=Mock()))
        lookup, final = Mock(id="synthesis:lookup"), Mock(id="synthesis:final")
        def size(context):
            ids = [obj["object_id"] for obj in context["original_objects"]]
            return {"fits": "V-C1-2" not in ids and len(ids) < 2}
        lookup.synthesis_size.side_effect = final.synthesis_size.side_effect = size
        lookup.step.side_effect = [
            {"request_original_objects": ["V-C1-2"], "reason": "核对"},
            {"request_original_objects": ["V-C1-1", "E-1"], "reason": "核对"},
            {"request_original_objects": ["E-1"], "reason": "核对"},
            {"request_meta_review": {"question": "检查候选", "checks_already_done": "已查原文",
             "expected_change": "区分原因", "chain_id": None, "evidence_refs": []}, "reason": "需补查"},
        ]
        with patch.object(synthesis, "collect_trajectory", return_value=trajectory), \
             patch.object(synthesis, "Session", side_effect=[lookup, final]), \
             patch.object(synthesis, "export_selected_answer", return_value=({}, {})):
            synthesis.synthesize(case, {}, meta_returns_left=3)
        contexts = [call.args[0] for call in lookup.step.call_args_list]
        self.assertIn("超限", contexts[1]["validation_feedback"][0]["message"])
        self.assertIn("超限", contexts[2]["validation_feedback"][0]["message"])
        self.assertEqual(contexts[1]["original_objects"], [])
        self.assertEqual(contexts[2]["original_objects"], [])
        self.assertEqual(contexts[3]["original_objects"][0]["object"], trajectory["实际证据"]["E-1"])

    def test_lookup_limit_switches_to_final_and_rejects_further_lookup(self):
        case = SimpleNamespace(cfg=CONFIG, out=Path("."), chains={},
                               rt=SimpleNamespace(records=Mock(), tele=Mock(), budget=Mock()))
        lookup, final = Mock(id="synthesis:lookup"), Mock(id="synthesis:final")
        lookup.synthesis_size.return_value = final.synthesis_size.return_value = {"fits": True}
        lookup.step.return_value = {"request_original_objects": ["E-1"], "reason": "核对"}
        final.step.side_effect = [
            {"request_original_objects": ["V-C1-1"], "reason": "再次核对"},
            {"request_meta_review": {"question": "检查候选", "checks_already_done": "已查原文",
             "expected_change": "区分原因", "chain_id": None, "evidence_refs": []}, "reason": "需补查"},
        ]
        with patch.object(synthesis, "collect_trajectory", return_value=trajectory_fixture()), \
             patch.object(synthesis, "Session", side_effect=[lookup, final]), \
             patch.object(synthesis, "export_selected_answer", return_value=({}, {})):
            _, _, report = synthesis.synthesize(case, {}, meta_returns_left=3)
        self.assertEqual(lookup.step.call_count, 8)
        self.assertEqual(final.step.call_count, 2)
        first, second = [call.args[0] for call in final.step.call_args_list]
        self.assertEqual(first["phase"], "final")
        self.assertEqual(first["original_objects"][0]["object_id"], "E-1")
        self.assertEqual(second["original_objects"], [])
        self.assertIn("不能回查", second["validation_feedback"][0]["message"])
        self.assertEqual((report["status"], report["lookup_calls"], report["formal_attempts"]),
                         ("meta_review_requested", 8, 1))

    def test_real_session_json_feedback_does_not_linger(self):
        trajectory = trajectory_fixture()
        runtime = SimpleNamespace(cfg=CONFIG, budget=Budget(CONFIG), handle="test", model=Mock(),
                                  provider_stop=threading.Event(), provider_failure=None,
                                  records=Mock(), tele=Mock(), names={"A", "B"})
        case = SimpleNamespace(cfg=CONFIG, out=Path("."), rt=runtime, chains={})
        runtime.model.call.side_effect = [
            ValueError("invalid JSON"),
            {"request_original_objects": ["E-1"], "reason": "核对原文"},
            {"request_meta_review": {"question": "检查候选", "checks_already_done": "已查原文",
             "expected_change": "区分原因", "chain_id": None, "evidence_refs": []}, "reason": "需补查"},
        ]
        with patch.object(synthesis, "collect_trajectory", return_value=trajectory), \
             patch.object(synthesis, "export_selected_answer", return_value=({}, {})):
            _, _, report = synthesis.synthesize(case, {}, meta_returns_left=3)
        template = prompt("synthesis_lookup")[1]
        prefix, suffix = template.split("{{context}}")
        payloads = []
        for call in runtime.model.call.call_args_list:
            user = call.args[1]
            self.assertTrue(user.startswith(prefix))
            self.assertTrue(user.endswith(suffix))
            payloads.append(expand_context(
                json.loads(user[len(prefix):len(user) - len(suffix) if suffix else None])))
        self.assertEqual(len(payloads), 3)
        self.assertEqual(payloads[0]["validation_feedback"], [])
        self.assertIn("输出不是有效 JSON", payloads[1]["validation_feedback"][0]["message"])
        for payload in payloads:
            self.assertEqual(payload["format_feedback"], [])
            self.assertNotIn("本轮纠错指令", payload)
        self.assertEqual([payload["lookup_calls_used"] for payload in payloads], [0, 1, 2])
        self.assertEqual([payload["formal_attempts_used"] for payload in payloads], [0, 0, 0])
        self.assertEqual(payloads[2]["validation_feedback"], [])
        self.assertEqual(payloads[2]["original_objects"][0]["object"], trajectory["实际证据"]["E-1"])
        self.assertEqual((report["status"], report["lookup_calls"], report["formal_attempts"]),
                         ("meta_review_requested", 3, 0))
        self.assertEqual(runtime.budget.models, 3)
        self.assertEqual(runtime.budget.synthesis_lookups, 3)

    def test_oversized_working_set_stops_before_model_call(self):
        case = SimpleNamespace(cfg=CONFIG, rt=SimpleNamespace(records=Mock(), budget=Mock()))
        lookup, final = Mock(), Mock()
        lookup.synthesis_size.return_value = {"fits": False}
        with patch.object(synthesis, "collect_trajectory", return_value=trajectory_fixture()), \
             patch.object(synthesis, "Session", side_effect=[lookup, final]):
            with self.assertRaisesRegex(StageFailure, "未发送模型"):
                synthesis.synthesize(case, {})
        lookup.step.assert_not_called()
        final.step.assert_not_called()


if __name__ == "__main__":
    unittest.main()
