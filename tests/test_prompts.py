"""提示词加载和故障类型说明检查；不读案例数据，不调用模型接口。"""

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agents import loop, seed
from output_contract import FAULT_KINDS
from progress_display import render_event
from settings import CONFIG
from state import Budget, dump


class PromptTests(unittest.TestCase):
    def setUp(self):
        loop.prompt.cache_clear()
        loop.fault_kind_guide.cache_clear()
        loop.fault_kind_sections.cache_clear()

    def tearDown(self):
        loop.prompt.cache_clear()
        loop.fault_kind_guide.cache_clear()
        loop.fault_kind_sections.cache_clear()

    def test_guide_covers_every_type_once_with_four_parts(self):
        guide = loop.fault_kind_guide()
        sections = re.split(r"^### `([a-z_]+)`[^\n]*\n", guide, flags=re.MULTILINE)
        self.assertEqual(set(sections[1::2]), set(FAULT_KINDS))
        self.assertEqual(len(sections[1::2]), len(FAULT_KINDS))
        for name, section in zip(sections[1::2], sections[2::2]):
            with self.subTest(fault_kind=name):
                for label in ("中文含义", "支持线索", "容易混淆", "如何区分"):
                    self.assertEqual(section.count(f"**{label}**"), 1)

    def test_summary_covers_all_types_without_repeating_full_guide(self):
        guide = loop.fault_kind_guide()
        summary = loop.fault_kind_summary()
        for role in ("l1", "l2", "synthesis"):
            system, user = loop.prompt(role)
            self.assertEqual(system.count(summary), 1)
            self.assertNotIn(guide, system)
            self.assertNotIn("{{fault_kind_guide}}", system)
            self.assertNotIn("{{fault_kind_options}}", system)
            self.assertEqual(user.count("{{context}}"), 1)
            self.assertNotIn(guide, user)
        seed_system, _ = loop.prompt("seed")
        self.assertNotIn(guide, seed_system)
        meta_system, _ = loop.prompt("meta")
        self.assertNotIn(summary, meta_system)

    def test_prompt_loading_only_reads_project_prompt_files(self):
        original = Path.read_text
        reads = []

        def tracked_read(path, *args, **kwargs):
            reads.append(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", tracked_read):
            for _ in range(2):
                for role in ("seed", "l1", "l2", "meta", "synthesis", "synthesis_lookup"):
                    loop.prompt(role)
        self.assertEqual(set(reads), {
            loop.PROMPT_DIR / name
            for name in ("seed_runtime.md", "l1_runtime.md", "l2_runtime.md", "meta_runtime.md",
                         "synthesis_runtime.md", "fault_kinds.md")
        })
        self.assertEqual(reads.count(loop.PROMPT_DIR / "fault_kinds.md"), 1)

    def test_missing_duplicate_and_extra_types_fail_clearly(self):
        guide = loop.fault_kind_guide()
        bad_guides = [
            guide.replace("### `clock_skew`", "### clock_skew", 1),
            guide + "\n### `clock_skew`\n",
            guide + "\n### `made_up_kind`\n",
        ]
        with tempfile.TemporaryDirectory(prefix="rca_prompt_test_") as folder:
            directory = Path(folder)
            with patch.object(loop, "PROMPT_DIR", directory):
                for content in bad_guides:
                    (directory / "fault_kinds.md").write_text(content, encoding="utf-8")
                    loop.fault_kind_guide.cache_clear()
                    with self.assertRaisesRegex(RuntimeError, "FAULT_KINDS"):
                        loop.fault_kind_guide()

    def test_guide_does_not_include_case_answers_or_fixed_cutoffs(self):
        guide = loop.fault_kind_guide()
        self.assertNotRegex(guide, r"batch-[A-Za-z0-9]+|case_\d+|E-[0-9a-f]{10,}")
        self.assertNotRegex(guide, r"ts-[a-z]+-service|hotel-reserv-|\bgeo\b|\bprofile\b")
        self.assertNotRegex(guide, r"https?://\S*(?:label\.json|injection\.json|causal_graph\.json)")
        self.assertNotRegex(guide, r"\d+(?:\.\d+)?\s*(?:毫秒|秒|%|ms)\b")
        self.assertIn("不得读取标签", guide)
        self.assertIn("不是出现一项就确诊", guide)
        self.assertIn("只有在授权遥测实际包含时", guide)

    def test_distinctions_and_role_rules_remain_explicit(self):
        guide = loop.fault_kind_guide()
        self.assertIn("PodKill、ContainerKill", guide)
        self.assertIn("PodFailure", guide)
        self.assertIn("不能要求容器 CPU 必须正常", guide)
        self.assertIn("不能靠持续时长硬分两种类型", guide)
        self.assertIn("不能据此直接算自身耗时", guide)
        l1, _ = loop.prompt("l1")
        l2, _ = loop.prompt("l2")
        meta, _ = loop.prompt("meta")
        self.assertNotIn("root` 有充分类型依据时", l1)
        self.assertIn("fault_diagnosis", l1)
        self.assertIn("component_verdict", l2)
        self.assertIn("fault_kind_verdict", l2)
        self.assertIn("没有提交类型时", l2)
        self.assertIn("你不在该动作中输出最终根因列表或传播图", meta)
        self.assertIn('target_type="l1"', meta)

    def test_actual_session_passes_summary_to_model(self):
        # 固定响应只检查模型收到的提示词，不模拟调查或评估诊断准确率。
        model = Mock()
        model.call.return_value = {"action": "conclude"}
        runtime = SimpleNamespace(
            cfg=CONFIG, model=model, budget=Budget(CONFIG), handle="prompt-fixture",
            compact_catalog={}, provider_stop=Mock(is_set=Mock(return_value=False)),
            records=Mock(),
        )
        session = loop.Session(runtime, "synthesis", "synthesis:final")
        session.progress = Mock()
        session.step({"mode": "synthesis"}, [], synthesis=True)
        system, user = model.call.call_args.args
        self.assertIn(loop.fault_kind_summary(), system)
        self.assertNotIn(loop.fault_kind_guide(), system)
        template = loop.prompt("synthesis")[1]
        prefix, suffix = template.split("{{context}}")
        payload = json.loads(user[len(prefix):len(user) - len(suffix) if suffix else None])
        self.assertEqual(payload["mode"], "synthesis")
        self.assertNotIn("{{context}}", user)

    def test_compact_model_json_preserves_nested_values_and_order(self):
        payload = {
            "reason": '中文、标点 : , 与空格应保留；引号 " 和换行\n',
            "rows": [{"empty": "", "unknown": None, "flag": False, "value": 0.125}, 0],
            "ref": Path("evidence/example.json"),
        }
        compact = loop.model_dump(payload)
        self.assertEqual(json.loads(compact), json.loads(dump(payload)))
        self.assertEqual(list(json.loads(compact)), list(payload))
        self.assertLess(len(compact), len(dump(payload)))

    def test_compact_model_json_keeps_nonfinite_values_invalid(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    loop.model_dump({"value": value})

    def test_l1_and_l2_can_read_details_without_creating_case_evidence(self):
        for role in ("l1", "l2"):
            with self.subTest(role=role):
                runtime = SimpleNamespace(cfg=CONFIG, model=Mock(), budget=Budget(CONFIG),
                                          handle="guide-fixture", records=Mock(), tele=Mock())
                session = loop.Session(runtime, role, role)
                session.progress = Mock()
                session._tool({"tool": "read_fault_guide", "args": {"fault_kinds": ["pod_failure"]}})
                result = session.history[-1]["data"]["result"]
                self.assertEqual(result["status"], "ok")
                self.assertIn("如何区分", result["guide"]["pod_failure"])
                self.assertEqual(result["evidence_refs"], [])
                self.assertEqual(session.data_queries, 0)
                self.assertEqual(runtime.budget.tools, 1)
                runtime.tele.call.assert_not_called()

    def test_invalid_guide_request_is_a_correctable_action_not_tool_failure(self):
        runtime = SimpleNamespace(cfg=CONFIG, model=Mock(), budget=Budget(CONFIG),
                                  handle="guide-fixture", records=Mock(), tele=Mock())
        session = loop.Session(runtime, "l2", "l2")
        with self.assertRaisesRegex(loop.Invalid, "正式类型名"):
            session._tool({"tool": "read_fault_guide", "args": {"fault_kinds": ["unknown"]}})
        self.assertEqual(runtime.budget.tools, 0)
        runtime.tele.call.assert_not_called()

    def test_seed_query_summary_keeps_index_and_separates_question_from_sample(self):
        history = loop.QueryHistory()
        history.remember(("fixture", "{}"), "query_telemetry", {"sql": "SELECT 1"},
                         {"status": "ok", "rows": [{"v": 1}], "evidence_refs": ["E-1"]},
                         "seed", "尚待确认的问题")
        summary = history.query_summary()
        self.assertEqual(summary["queries"][0]["evidence_refs"], ["E-1"])
        self.assertEqual(summary["queries"][0]["agent_question"], "尚待确认的问题")
        self.assertEqual(summary["recent_observations"][0]["rows_preview"], [{"v": 1}])
        self.assertTrue(summary["recent_observations"][0]["preview_only"])


class SeedHandoffProgressTests(unittest.TestCase):
    def test_handoff_trigger_and_model_reason(self):
        # 固定响应只验证 Seed 交接事件和显示，不访问数据集或真实模型。
        for trigger in ("model_decision", "repeat_limit", "tool_budget_exhausted"):
            with self.subTest(trigger=trigger):
                model = Mock()
                model.call.return_value = {
                    "action": "propose_seeds",
                    "seeds": [{"service": "geo", "evidence_refs": ["E-1"], "reason": "异常证据"}],
                    "symptoms": [],
                    "reason": "异常时段有证据，交给 L1 继续调查。",
                }
                budget = Budget(CONFIG)
                if trigger == "tool_budget_exhausted":
                    budget.tools = CONFIG.max_tool_calls
                rt = SimpleNamespace(
                    cfg=CONFIG, model=model, budget=budget, handle="fixture",
                    compact_catalog={}, catalog={"tables": {}},
                    provider_stop=Mock(is_set=Mock(return_value=False)), records=Mock(),
                    service=Mock(side_effect=lambda name: name), refs=Mock(return_value=["E-1"]),
                )
                session = loop.Session(rt, "seed", "seed")
                session.progress = Mock()
                session.seen.add("E-1")
                if trigger == "repeat_limit":
                    session.consecutive_repeats = CONFIG.max_consecutive_repeated_queries
                case = SimpleNamespace(
                    cfg=CONFIG, rt=rt, seed_agent=session, rounds=[], seeds=[], symptoms=[],
                )
                with patch("agents.seed.report") as report:
                    result = seed.seed_round(case, [])
                self.assertEqual(result[0]["service"], "geo")
                fields = report.call_args.kwargs
                self.assertEqual(fields["handoff_trigger"], trigger)
                event = {"kind": "seeds_ready", **fields, "width": 40}
                line = render_event(event, color=False, icons=False)
                self.assertIn("候选交接准备", line)
                self.assertIn("候选：geo", line)
                self.assertIn("模型说明：异常时段有证据，交给 L1 继续调查。", line)
                self.assertIn({
                    "model_decision": "模型提交候选",
                    "repeat_limit": "连续重复查询达到上限",
                    "tool_budget_exhausted": "案例工具额度已用尽",
                }[trigger], line)

    def test_empty_candidate_is_not_reported_as_l1_handoff(self):
        event = {
            "kind": "seeds_ready", "stage": "seed", "mode": "supplement", "count": 0,
            "services": [], "handoff_trigger": "model_decision", "reason": "暂无新增证据。",
        }
        line = render_event(event, color=False, icons=False)
        self.assertIn("本轮没有候选", line)
        self.assertNotIn("候选交接准备", line)
        self.assertIn("模型说明：暂无新增证据。", line)


if __name__ == "__main__":
    unittest.main()
