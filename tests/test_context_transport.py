"""Offline invariants for lossless model-input deduplication; no model calls."""

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agents.context_transport import (
    FORMAT_KEY, FORMAT_VERSION, POOL_KEY, REF_KEY, TRANSPORT_INSTRUCTION,
    encode_context, expand_context, is_encoded_context,
)


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False, separators=(",", ":"))


def fact():
    return {
        "kind": "metric", "evidence_refs": ["E-fixture-a"],
        "window": "abnormal", "start": "2026-05-01T09:19:07Z", "end": "2026-05-01T09:24:07Z",
        "unit": "ms", "truncated": True, "row_count": 100,
        "rows": [{"service": "service-a", "value": i / 10, "state": None, "observed": True} for i in range(16)],
        "notes": "完整保留观测、反证及范围，不把缺失值解释为正常。" * 12,
    }


class ContextTransportTests(unittest.TestCase):
    def test_repeated_evidence_roundtrips_with_scope_positions_and_order(self):
        evidence = fact()
        payload = {
            "catalog": {"tables": ["normal_metrics", "abnormal_metrics"]},
            "history": [{"event": "tool_result", "data": evidence}],
            "investigation_summary": {"recent_observations": [copy.deepcopy(evidence)]},
            "branch_frontier": {"items": [{"observation": copy.deepcopy(evidence), "claim": "unconfirmed"}]},
            "available_refs": ["E-fixture-a"], "budget": {"models": 12},
        }
        before = dump(payload)
        encoded = encode_context(payload)
        self.assertTrue(is_encoded_context(encoded))
        self.assertEqual(dump(expand_context(encoded)), before)
        self.assertEqual(dump(payload), before)
        self.assertEqual(list(encoded)[:len(payload)], list(payload))
        self.assertEqual(len(encoded[POOL_KEY]), 1)
        self.assertLess(len(dump(encoded)) + len(TRANSPORT_INSTRUCTION), len(before))
        self.assertLess(len(dump(encoded).encode()) + len(TRANSPORT_INSTRUCTION.encode()), len(before.encode()))

    def test_references_are_input_only_and_do_not_grant_evidence_permissions(self):
        evidence = fact()
        payload = {
            "chain_a": {"seen": ["E-fixture-a"], "data": evidence},
            "chain_b": {"seen": [], "data": evidence},
            "chain_c": {"seen": [], "data": evidence},
        }
        restored = expand_context(encode_context(payload))
        self.assertEqual(restored["chain_a"]["seen"], ["E-fixture-a"])
        self.assertEqual(restored["chain_b"]["seen"], [])
        self.assertEqual(restored["chain_c"]["seen"], [])
        self.assertIn("不是证据编号", TRANSPORT_INSTRUCTION)
        self.assertIn("原动作协议", TRANSPORT_INSTRUCTION)

    def test_different_windows_units_statuses_and_values_are_never_merged(self):
        base = fact()
        variants = [copy.deepcopy(base) for _ in range(6)]
        variants[0]["window"] = "normal"
        variants[1]["unit"] = "ns"
        variants[2]["truncated"] = False
        variants[3]["rows"][0]["value"] = 0
        variants[4]["rows"][0]["observed"] = 1
        variants[5]["evidence_refs"] = ["E-fixture-b"]
        payload = {"original": [base, base, base], "different": variants}
        self.assertEqual(dump(expand_context(encode_context(payload))), dump(payload))

    def test_nested_duplicates_use_full_original_pool_values(self):
        leaf = fact()
        parent = {"first": leaf, "second": leaf, "state": "unknown"}
        payload = {"history": [parent, parent, parent], "other": leaf}
        encoded = encode_context(payload)
        self.assertTrue(is_encoded_context(encoded))
        self.assertEqual(len(encoded[POOL_KEY]), 1)
        for value in encoded[POOL_KEY].values():
            self.assertNotIn('"$rca_ref"', dump(value))
        self.assertEqual(dump(expand_context(encoded)), dump(payload))

    def test_restored_occurrences_are_independent(self):
        evidence = fact()
        restored = expand_context(encode_context({"a": evidence, "b": evidence, "c": evidence}))
        restored["a"]["rows"][0]["value"] = -999
        self.assertEqual(restored["b"]["rows"][0]["value"], 0.0)
        self.assertEqual(evidence["rows"][0]["value"], 0.0)

    def test_small_unique_or_unprofitable_data_is_unchanged(self):
        for payload in ({}, {"a": []}, {"a": [1, 2], "b": [1, 2]}, {"a": fact()}):
            with self.subTest(payload=payload):
                encoded = encode_context(payload)
                self.assertFalse(is_encoded_context(encoded))
                self.assertEqual(dump(encoded), dump(payload))
        small = {"value": "a" * 245}
        payload = {"a": small, "b": small}
        self.assertEqual(encode_context(payload), payload)

    def test_reserved_key_in_nested_data_disables_encoding(self):
        for key in (POOL_KEY, REF_KEY, FORMAT_KEY):
            payload = {"a": fact(), "b": fact(), "collision": [{key: "literal application data"}]}
            self.assertEqual(dump(encode_context(payload)), dump(payload))
        payload = {POOL_KEY: "ordinary", "a": fact(), "b": fact()}
        self.assertEqual(expand_context(encode_context(payload)), payload)

    def test_reserved_words_inside_strings_are_plain_data(self):
        evidence = fact()
        evidence["body"] = 'Log text mentions {"$rca_ref":"fake"} but is not an object.'
        payload = {"a": evidence, "b": evidence, "c": evidence}
        self.assertTrue(is_encoded_context(encode_context(payload)))
        self.assertEqual(expand_context(encode_context(payload)), payload)

    def test_literal_transport_shaped_business_data_remains_unencoded(self):
        payload = {FORMAT_KEY: FORMAT_VERSION, POOL_KEY: {"ordinary": {"value": "business data"}},
                   "a": {REF_KEY: "ordinary"}, "b": fact(), "c": fact()}
        encoded = encode_context(payload)
        self.assertFalse(is_encoded_context(encoded))
        self.assertEqual(dump(encoded), dump(payload))
        self.assertEqual(dump(expand_context(encoded)), dump(payload))

    def test_serialized_known_transport_can_be_expanded_for_replay(self):
        payload = {"a": fact(), "b": fact(), "c": fact()}
        transport = json.loads(dump(encode_context(payload)))
        self.assertEqual(dump(expand_context(transport)), dump(payload))
        self.assertEqual(dump(expand_context(transport, encoded=True)), dump(payload))

    def test_explicit_false_preserves_reserved_literal_data_across_json_boundary(self):
        payload = {FORMAT_KEY: FORMAT_VERSION, POOL_KEY: {"ordinary": {"value": "business data"}},
                   "a": {REF_KEY: "ordinary"}, "b": fact(), "c": fact()}
        prepared = encode_context(payload)
        encoded_flag = is_encoded_context(prepared)
        restored_json = json.loads(dump(prepared))
        self.assertFalse(encoded_flag)
        self.assertEqual(dump(expand_context(restored_json, encoded=encoded_flag)), dump(payload))
        # Explicit literal mode also preserves unknown references and malformed
        # pools verbatim: they are application fields, not transport metadata.
        restored_json[POOL_KEY] = ["literal", None]
        restored_json["a"] = {REF_KEY: "missing", "extra": "must remain"}
        self.assertEqual(expand_context(restored_json, encoded=False), restored_json)

    def test_explicit_true_requires_valid_metadata_and_flag_type(self):
        payload = {"a": fact(), "b": fact(), "c": fact()}
        transport = json.loads(dump(encode_context(payload)))
        for bad in ({"ordinary": True}, {**transport, FORMAT_KEY: "unsupported"},
                    {key: value for key, value in transport.items() if key != POOL_KEY}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    expand_context(bad, encoded=True)
        for bad_flag in (0, 1, "true", []):
            with self.assertRaises(TypeError):
                expand_context(transport, encoded=bad_flag)

    def test_transport_instruction_is_conditional_for_small_unencoded_requests(self):
        self.assertIn("若输入含对象池", TRANSPORT_INSTRUCTION)
        payload = {"catalog": {}, "budget": {"models": 1}}
        self.assertFalse(is_encoded_context(encode_context(payload)))

    def test_same_input_produces_same_ids_and_same_encoding(self):
        payload = {"a": fact(), "b": fact(), "c": fact()}
        self.assertEqual(dump(encode_context(payload)), dump(encode_context(copy.deepcopy(payload))))
        second = {"new": "stable other field", **payload}
        self.assertEqual(list(encode_context(payload)[POOL_KEY]), list(encode_context(second)[POOL_KEY]))

    def test_hash_collision_falls_back_without_merging_distinct_objects(self):
        first, second = fact(), fact()
        second["window"] = "normal"
        payload = {"a": [first, first], "b": [second, second]}
        with patch("agents.context_transport._object_id", return_value="collision"):
            self.assertEqual(dump(encode_context(payload)), dump(payload))

    def test_default_serializer_semantics_and_exact_key_order(self):
        evidence = fact()
        evidence["path"] = Path("evidence/fixture.json")
        evidence["tuple"] = (None, False, 1, 1.0, "标点 , : 和空格")
        evidence["ordered"] = {"b": 1, "a": 2}
        payload = {"second": evidence, "first": evidence, "third": evidence}
        self.assertEqual(dump(expand_context(encode_context(payload))), dump(payload))

    def test_nonfinite_and_cyclic_values_remain_invalid(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.assertRaises(ValueError):
                encode_context({"value": value})
        cyclic = {"a": []}
        cyclic["a"].append(cyclic)
        with self.assertRaises(ValueError):
            encode_context(cyclic)
        with self.assertRaises(ValueError):
            expand_context(cyclic)

    def test_decoder_rejects_unknown_malformed_nested_and_unused_references(self):
        encoded = encode_context({"a": fact(), "b": fact(), "c": fact()})
        mutations = []
        bad = copy.deepcopy(encoded)
        bad["a"] = {REF_KEY: "missing"}
        mutations.append(bad)
        bad = copy.deepcopy(encoded)
        bad["a"]["extra"] = "do not discard this"
        mutations.append(bad)
        bad = copy.deepcopy(encoded)
        bad["a"] = {REF_KEY: []}
        mutations.append(bad)
        bad = copy.deepcopy(encoded)
        identifier = next(iter(bad[POOL_KEY]))
        bad[POOL_KEY][identifier] = {REF_KEY: identifier}
        mutations.append(bad)
        bad = copy.deepcopy(encoded)
        bad[POOL_KEY]["unused"] = {"x": 1}
        mutations.append(bad)
        bad = copy.deepcopy(encoded)
        bad[POOL_KEY] = []
        mutations.append(bad)
        for value in mutations:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    expand_context(value)

    def test_disable_and_parameter_validation(self):
        payload = {"a": fact(), "b": fact(), "c": fact()}
        self.assertIs(encode_context(payload, enabled=False), payload)
        for kwargs in ({"min_chars": 0}, {"min_chars": True}, {"min_savings": -1}):
            with self.assertRaises(ValueError):
                encode_context(payload, **kwargs)
        with self.assertRaises(TypeError):
            encode_context([])


if __name__ == "__main__":
    unittest.main()
