"""验证真实综合和 Meta 文件读取；不运行调查流程或调用模型接口。"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agents import meta, synthesis
from agents.loop import Invalid
from filesystem import atomic_write_text, filesystem_path


class LongPathReadingTests(unittest.TestCase):
    def setUp(self):
        # mkdtemp 创建专用于本测试的唯一目录；清理时只删除此目录。
        self.temp_root = Path(tempfile.mkdtemp(prefix="rca_path_read_"))
        self.addCleanup(shutil.rmtree, filesystem_path(self.temp_root))

    def _fixture(self, *, long_path):
        out = self.temp_root / ("long" if long_path else "short")
        if long_path:
            # 每段远小于常见单个文件名限制，但完整路径超过 Windows 传统限制。
            out = out / ("batch_" + "a" * 70) / ("case_" + "b" * 70) / ("result_" + "c" * 70)
        chain_id = "S01-C01"
        root = {"service": "source", "fault_kind": "network_delay", "evidence_refs": ["E-1"]}
        versions = [
            {"chain_id": chain_id, "revision": 1, "nodes": ["source"], "edges": [], "root": None},
            {"chain_id": chain_id, "revision": 2, "nodes": ["source", "affected"],
             "edges": [{"from": "source", "to": "affected", "evidence_refs": ["E-2"]}], "root": root},
        ]
        review = {"chain_id": chain_id, "revision": 2, "review_id": "R1", "status": "reviewed",
                  "component_verdict": {"verdict": "support", "evidence_refs": ["E-1"]},
                  "fault_kind_verdict": {"verdict": "unknown", "evidence_refs": []},
                  "edge_verdicts": [{"from": "source", "to": "affected", "verdict": "support",
                                     "evidence_refs": ["E-2"]}]}
        chain = {"chain_id": chain_id, "revision": 2, "status": "submitted", "reviews": [review]}
        evidence = {
            "E-1": {"evidence_ref": "E-1", "data_version": "fixture", "kind": "metric",
                    "sql": "SELECT 1", "rows": [{"value": 1}], "truncated": False},
            "E-2": {"evidence_ref": "E-2", "data_version": "fixture", "kind": "trace",
                    "sql": "SELECT 2", "rows": [{"value": 2}], "truncated": False},
        }
        for version in versions:
            path = out / "stages" / "l1" / chain_id / f"v{version['revision']:03d}.json"
            atomic_write_text(path, json.dumps(version, ensure_ascii=False))
        for ref, item in evidence.items():
            atomic_write_text(out / "evidence" / f"{ref}.json", json.dumps(item, ensure_ascii=False))
        events = [
            {"seq": 2, "agent_id": "meta:arbitrate", "event": "concluded", "data": {}},
            {"seq": 1, "agent_id": "l1:S01-C01", "event": "action_rejected", "data": {"reason": "反馈"}},
        ]
        event_file = out / "stages" / "events.jsonl"
        atomic_write_text(event_file, "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n")
        case = SimpleNamespace(
            out=out, rt=SimpleNamespace(records=SimpleNamespace(root=out / "stages")),
            chains={chain_id: chain}, ordered=lambda: [chain],
            walkers={chain_id: SimpleNamespace(seen={"E-1"})},
            reviewers={chain_id: SimpleNamespace(seen={"E-2"})},
            investigation_stops=[], rounds=[], requests=[], followups=[], symptom_snapshot=lambda: [],
        )
        if long_path:
            self.assertGreater(len(str(out / "evidence" / "E-1.json")), 260)
            self.assertGreater(len(str(out / "stages" / "l1" / chain_id / "v002.json")), 260)
        return case, versions, evidence, events, event_file

    def test_long_and_short_paths_collect_complete_matching_trajectory(self):
        results = []
        for long_path in (False, True):
            with self.subTest(long_path=long_path):
                case, versions, evidence, events, _ = self._fixture(long_path=long_path)
                conclusion = {"termination_reason": "调查完成", "branch_assessments": [],
                              "confidence_scores": [], "open_issues": []}
                result = synthesis.collect_trajectory(case, conclusion)
                versions_saved = result["真实链版本和审核"]
                self.assertEqual([row["chain"] for row in versions_saved], versions)
                self.assertEqual([row["chain"]["revision"] for row in versions_saved], [1, 2])
                self.assertEqual(versions_saved[0]["reviews"], [])
                self.assertEqual(versions_saved[1]["reviews"], case.chains["S01-C01"]["reviews"])
                self.assertEqual(versions_saved[1]["allowed_evidence_refs"], ["E-1", "E-2"])
                self.assertEqual(result["实际证据"], evidence)
                self.assertEqual(result["事件按角色分类"]["L1_AGENT"], [events[1]])
                self.assertEqual(result["事件按角色分类"]["META_AGENT"], [events[0]])
                self.assertEqual(result["失败与未完成事件"], [events[1]])
                self.assertEqual(result["META结案"], conclusion)
                self.assertEqual(meta._version(case, "S01-C01", 2), versions[1])
                action = {"action": "conclude", "branch_assessments": [
                              {"chain_id": "S01-C01", "revision": 2, "assessment": "核对完成"}],
                          "confidence_scores": [{"chain_id": "S01-C01", "revision": 2,
                                                 "conclusion_ref": "root:source|network_delay",
                                                 "conclusion_confidence": 0.7, "reason": "证据支持"}],
                          "open_issues": [], "termination_reason": "调查完成"}
                self.assertEqual(meta.check_conclusion(case, action)["branch_assessments"], action["branch_assessments"])
                results.append(result)
        self.assertEqual(results[0], results[1])

    def test_missing_optional_events_remains_empty(self):
        case, versions, evidence, _, event_file = self._fixture(long_path=True)
        filesystem_path(event_file).unlink()
        result = synthesis.collect_trajectory(case, {})
        self.assertTrue(all(not group for group in result["事件按角色分类"].values()))
        self.assertEqual(result["失败与未完成事件"], [])
        self.assertEqual([row["chain"] for row in result["真实链版本和审核"]], versions)
        self.assertEqual(result["实际证据"], evidence)

    def test_missing_required_files_and_invalid_json_are_not_hidden(self):
        case, _, _, _, _ = self._fixture(long_path=True)
        missing = case.out / "evidence" / "missing.json"
        with self.assertRaises(FileNotFoundError):
            synthesis._read(missing)
        with self.assertRaises(Invalid):
            meta._version(case, "S01-C01", 9)
        broken = case.out / "evidence" / "broken.json"
        atomic_write_text(broken, "{not-json")
        with self.assertRaises(json.JSONDecodeError):
            synthesis.collect_trajectory(case, {})


if __name__ == "__main__":
    unittest.main()
