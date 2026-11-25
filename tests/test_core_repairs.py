"""Core repair contracts. Fixed objects only; saved for later execution, not run during editing."""

import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agents import l2, meta, synthesis
from agents.loop import Runtime
from workflow import Incident
from test_synthesis_design import trajectory_fixture


class CoreRepairTests(unittest.TestCase):
    def setUp(self):
        self.case = Incident.__new__(Incident)
        self.case.symptoms = []
        self.case.chains = {}
        runtime = Runtime.__new__(Runtime)
        runtime.names = {"A", "B"}
        runtime.records = Mock()
        runtime.records.lock = threading.RLock()
        runtime.budget = Mock()
        runtime.tele = Mock()
        runtime.tele.evidence.side_effect = lambda ref: {
            "kind": "trace", "sql": "SELECT * FROM abnormal_traces",
            "rows": [{"value": 1}], "data_version": "fixture", "details": {"window": "abnormal"}}
        runtime.tele.validated_evidence.return_value = (None, None, {"abnormal_traces"})
        self.case.rt = runtime
        self.session = SimpleNamespace(id="l1:S01:C01", seen={"E1", "E2"})

    def symptom(self, **extra):
        return {"service": "B", "operation": "GET /item", "anomaly_kind": "error",
                "description": "Observed errors", "evidence_refs": ["E1"], **extra}

    def test_registry_deduplicates_description_changes_but_keeps_interfaces(self):
        self.case.register_symptoms([self.symptom()], self.session)
        self.case.register_symptoms([self.symptom(description="Same observation restated",
                                                  evidence_refs=["E1", "E2"])], self.session)
        self.case.register_symptoms([self.symptom(operation="POST /order")], self.session)
        self.assertEqual(len(self.case.symptoms), 2)
        self.assertEqual(self.case.symptoms[0]["evidence_refs"], ["E1", "E2"])
        self.assertEqual([x["symptom_index"] for x in self.case.symptoms], [0, 1])

    def test_bad_annotation_keeps_good_annotation_and_does_not_grant_evidence(self):
        result = self.case.register_symptoms([
            self.symptom(), self.symptom(evidence_refs=["unseen"]), None], self.session)
        self.assertEqual(result["symptom_indices"], [0])
        self.assertEqual(len(result["annotation_errors"]), 2)
        filtered = self.case.symptom_snapshot(set())[0]
        self.assertEqual(filtered["evidence_refs"], [])
        self.assertFalse(filtered["evidence_available"])
        self.assertNotIn("evidence_scopes", filtered)

    def audit_direction(self, direction):
        chain = {"chain_id": "C1", "l1_id": "l1:C1", "revision": 1, "nodes": ["A", "B"], "root": None,
                 "edges": [{"from": "A", "to": "B", "evidence_refs": ["E1"]}], "reviews": []}
        session = Mock()
        session.id, session.seen = "l2:C1", set()
        session.step.return_value = {
            "action": "audit", "reason": "Fixed review",
            "component_verdict": {"service": "A", "verdict": "unknown", "reason": "Unknown", "evidence_refs": []},
            "fault_kind_verdict": {"fault_kind": None, "verdict": "unknown", "reason": "Unknown", "evidence_refs": []},
            "edge_verdicts": [{"edge_id": "E01", "assessed_direction": direction, "verdict": "support",
                               "mechanism": "response_wait", "scope": "GET /item",
                               "reason": "Independent direction", "evidence_refs": ["E1"]}],
            "score": 0.7, "doubts": [], "recommendation": "backtrack"}
        self.case.reviewers = {"C1": session}
        self.case.walkers = {"C1": SimpleNamespace(seen={"E1"})}
        l2.audit(self.case, chain, {"evidence_refs": ["E1"]})
        self.assertEqual(session.step.call_count, 1)
        return chain["last_review"]

    def test_reverse_support_does_not_support_or_flip_original_edge(self):
        result = self.audit_direction(["B", "A"])
        edge = result["edge_verdicts"][0]
        self.assertEqual((edge["from"], edge["to"], edge["verdict"]), ("A", "B", "unknown"))
        self.assertTrue(edge["direction_mismatch"])
        self.assertEqual(edge["assessed_verdict"], "support")
        self.assertEqual(result["supported_prefix"], ["A"])

    def test_forward_support_builds_continuous_prefix(self):
        result = self.audit_direction(["A", "B"])
        self.assertEqual(result["supported_prefix"], ["A", "B"])
        self.assertFalse(result["edge_verdicts"][0]["direction_mismatch"])

    def test_meta_optional_scores_do_not_abort_conclusion(self):
        action = {"action": "conclude", "branch_assessments": [], "open_issues": [],
                  "termination_reason": "No further check", "confidence_scores": [
                      {"chain_id": "C1", "revision": 1, "conclusion_ref": "edge:A|B",
                       "conclusion_confidence": 0.873, "reason": "Specific evidence"},
                      {"chain_id": "C1", "revision": 1, "conclusion_ref": "invented"}]}
        with patch.object(meta, "_version", return_value={"edges": [{"from": "A", "to": "B"}]}):
            result = meta.check_conclusion(self.case, action)
        self.assertEqual(len(result["confidence_scores"]), 1)
        self.assertEqual(len(result["validation_issues"]), 1)

    def test_root_only_cannot_explain_another_service(self):
        chain = {"chain_id": "C1", "revision": 1, "nodes": ["A"], "edges": [],
                 "target_symptom": self.symptom(), "reviews": [{"revision": 1, "status": "reviewed",
                     "component_verdict": {"verdict": "support", "evidence_refs": ["E1"]}, "edge_verdicts": []}]}
        self.case.chains = {"C1": chain}
        self.assertIsNone(meta._explains_symptom(self.case, chain, self.symptom()))
        self.assertEqual(meta._explains_symptom(self.case, chain, self.symptom(service="A")), "root_local")

    def test_compaction_preserves_versions_permissions_and_original_objects(self):
        trajectory = trajectory_fixture()
        working, index = synthesis._working_set(trajectory)
        before = copy.deepcopy(working)
        compact = synthesis._compact_working_set(working, index)
        self.assertEqual(working, before)
        self.assertEqual(len(compact["真实链版本和审核"]), len(working["真实链版本和审核"]))
        for full, short in zip(working["真实链版本和审核"], compact["真实链版本和审核"]):
            self.assertEqual(short["object_id"], full["object_id"])
            permission_id = short["allowed_evidence_refs"]["object_id"]
            self.assertEqual(compact["compact_objects"][permission_id], full["allowed_evidence_refs"])
            self.assertEqual(index[permission_id]["object"], full["allowed_evidence_refs"])

    def test_historical_counterevidence_requires_explicit_resolution(self):
        snapshot = {"chain": {"chain_id": "C1", "revision": 1, "nodes": ["A", "B"],
                    "edges": [{"from": "A", "to": "B"}]}, "allowed_evidence_refs": ["E1", "E2"],
                    "reviews": [{"review_id": "C1-R1", "status": "reviewed", "revision": 1,
                        "edge_verdicts": [{"from": "A", "to": "B", "verdict": "contradict",
                                           "evidence_refs": ["E1"], "reason": "Counterevidence"}]}]}
        edge = {"from": "A", "to": "B", "source_chain_id": "C1", "source_revision": 1,
                "evidence_refs": ["E2"], "reason": "New discriminating observation"}
        draft = {"root_causes": [], "propagation": [edge], "reason": "Fixed draft"}
        _, errors, _ = synthesis.check_draft(self.case, draft, [snapshot], case_evidence_refs=["E1", "E2"])
        self.assertTrue(errors)
        conflict = synthesis._edge_conflicts([snapshot])[0]
        edge["conflict_resolution"] = {"conflict_ids": [conflict["conflict_id"]],
                                       "basis": "new_evidence", "evidence_refs": ["E2"]}
        _, errors, _ = synthesis.check_draft(self.case, draft, [snapshot], case_evidence_refs=["E1", "E2"])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
