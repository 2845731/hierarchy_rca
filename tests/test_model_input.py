"""输入表示层的集成验证；固定动作、离线运行，不访问模型接口。"""

import json
import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from agents.context_transport import FORMAT_VERSION, expand_context, is_encoded_context
from agents.loop import Session, model_dump, prompt
from agents.model_input import context_system_instruction, model_context, prefix_order, render_context, serialize_context
from settings import CONFIG
from state import Budget


class ModelInputTests(unittest.TestCase):
    def test_disabled_uses_original_json_and_no_extra_instruction(self):
        cfg = replace(CONFIG, context_exact_dedup=False, context_prefix_cache=False)
        payload = {"chain": {"nodes": ["B", "A"], "root": None}, "budget": 0, "catalog": {}}
        self.assertEqual(serialize_context(payload, cfg), model_dump(payload))
        self.assertEqual(context_system_instruction(cfg), "")

    def test_reordering_keeps_every_field_and_nested_order(self):
        payload = {"budget": {"remaining": 7}, "custom": [3, 2, 1],
                   "chain": {"z": "first", "a": "second"}, "catalog": {"tables": {}},
                   "fault_kinds": ["unknown", "cpu"], "本轮纠错指令": "保留原指令"}
        rendered = prefix_order(payload)
        self.assertEqual(rendered, payload)
        self.assertEqual(list(rendered)[:2], ["fault_kinds", "catalog"])
        self.assertEqual(list(rendered)[-1], "本轮纠错指令")
        self.assertEqual(list(rendered["chain"]), ["z", "a"])
        self.assertEqual(list(payload)[0], "budget")

    def test_small_request_has_no_unused_transport_instruction(self):
        payload = {"phase": "final", "budget": 2}
        serialized, instruction = render_context(payload, CONFIG)
        self.assertEqual(json.loads(serialized), payload)
        self.assertEqual(instruction, "")

    def test_render_roundtrip_preserves_evidence_scope_and_feedback(self):
        evidence = {"evidence_refs": ["E-real"], "rows": [{"window": "normal", "unit": "ns", "value": 0}],
                    "reason": "候选主张不等于事实；" * 100, "truncated": True}
        payload = {"case_handle": "fixture", "history": [evidence, evidence],
                   "available_refs": [], "last_review": {"verdict": "unknown"},
                   "本轮纠错指令": "证据未授权，不可使用"}
        encoded = model_context(payload, CONFIG)
        decoded = expand_context(json.loads(serialize_context(payload, CONFIG)))
        self.assertEqual(decoded, payload)
        self.assertEqual(decoded["available_refs"], [])
        self.assertLess(len(json.dumps(encoded, ensure_ascii=False)), len(json.dumps(payload, ensure_ascii=False)))

    def test_real_synthesis_session_sends_same_complete_context(self):
        repeated = {"reason": "完整原始观察内容" * 100, "evidence_refs": ["E-fixture"]}
        context = {"working_set": {"history": [repeated, repeated]}, "phase": "final"}
        model = Mock(call=Mock(return_value={"root_causes": []}))
        rt = SimpleNamespace(cfg=CONFIG, budget=Budget(CONFIG), handle="fixture", model=model,
                             provider_stop=threading.Event(), records=Mock())
        session = Session(rt, "synthesis", "synthesis:final")
        session.progress = Mock()
        expected = {"case_handle": "fixture", **context, "format_feedback": [], "incident_description": ""}
        prepared = session.synthesis_request(context)
        session.step(context, [], synthesis=True)
        system, user = model.call.call_args.args
        self.assertEqual((system, user), prepared)
        before, after = prompt("synthesis")[1].split("{{context}}")
        payload = json.loads(user[len(before):len(user) - len(after) if after else None])
        self.assertEqual(expand_context(payload), expected)
        self.assertEqual(rt.budget.models, 1)

    def test_reserved_literal_survives_serialization_without_reference_instruction(self):
        payload = {"$rca_transport": FORMAT_VERSION, "$rca_objects": {"o1": {"value": "literal"}},
                   "history": [{"$rca_ref": "o1"}], "budget": {"remaining": 1}}
        prepared = model_context(payload, CONFIG)
        self.assertFalse(is_encoded_context(prepared))
        text, instruction = render_context(payload, CONFIG)
        self.assertEqual(expand_context(json.loads(text), encoded=False), payload)
        self.assertIn("本轮未启用对象去重", instruction)
        self.assertIn("字面字段", instruction)

    def test_reordering_retains_encoded_status_and_last_correction(self):
        large = {"description": "完整内容不能被截断" * 100, "scope": "normal"}
        payload = {"history": [large, large], "本轮纠错指令": "保留最后提示"}
        prepared = model_context(payload, CONFIG)
        self.assertTrue(is_encoded_context(prepared))
        self.assertEqual(list(prepared)[-1], "本轮纠错指令")
        self.assertEqual(expand_context(json.loads(serialize_context(payload, CONFIG)), encoded=True), payload)


if __name__ == "__main__":
    unittest.main()
