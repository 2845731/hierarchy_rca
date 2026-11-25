"""Offline request/audit regression tests; all SDK responses are explicit fixtures."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from clients import minimax_client as client


def usage(prompt=100, cached=60, output=20, reasoning=12):
    return {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": reasoning}}


def response(content='{"action":"stop","reason":"fixture"}', reported_usage=None):
    return types.SimpleNamespace(
        model="fixture-model", usage=reported_usage,
        choices=[types.SimpleNamespace(finish_reason="stop", message=types.SimpleNamespace(content=content))],
    )


class UsageTests(unittest.TestCase):
    def test_missing_cache_is_not_inferred_as_an_uncached_response(self):
        record = {"usage": None, **dict.fromkeys(client.USAGE_COUNTER_FIELDS, 0)}
        client._accumulate_usage(record, usage())
        client._accumulate_usage(record, {"prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57})
        client._accumulate_usage(record, None)
        self.assertEqual(record["usage"]["prompt_tokens"], 150)
        self.assertEqual(record["usage"]["provider_cached_tokens"], 60)
        self.assertEqual(record["usage"]["uncached_prompt_tokens"], 40)
        self.assertEqual(record["usage"]["cache_usage_prompt_tokens"], 100)
        self.assertEqual(record["usage"]["reasoning_tokens"], 12)
        self.assertEqual(record["cache_usage_responses"], 1)
        self.assertEqual(record["missing_cache_usage_responses"], 2)
        self.assertEqual(record["missing_reasoning_usage_responses"], 2)
        self.assertEqual(record["missing_usage_responses"], 1)

    def test_invalid_cache_values_preserve_raw_total_but_not_a_fake_hit(self):
        for cached in (None, -1, 101, True, float("nan"), float("inf")):
            with self.subTest(cached=cached):
                record = {"usage": None, **dict.fromkeys(client.USAGE_COUNTER_FIELDS, 0)}
                client._accumulate_usage(record, usage(cached=cached))
                self.assertEqual(record["usage"]["prompt_tokens"], 100)
                self.assertNotIn("provider_cached_tokens", record["usage"])
                self.assertEqual(record["missing_cache_usage_responses"], 1)


class AuditTests(unittest.TestCase):
    def test_interrupted_states_restore_prompt_and_final_snapshot_wins(self):
        with tempfile.TemporaryDirectory(prefix="rca_audit_") as folder:
            writer = client._AuditWriter(folder, "fixture")
            record = {"call_id": "fixture", "audit_revision": 1, "status": "waiting_for_slot",
                      "system": "same system" * 100, "user": "same request" * 100}
            writer.write(record)
            path = Path(folder) / "fixture.json"
            record.update(audit_revision=2, status="requesting", api_requests=1)
            writer.write(record)
            state = json.loads((Path(folder) / "_states/fixture.json").read_text(encoding="utf-8"))
            self.assertNotIn("system", state)
            self.assertNotIn("user", state)
            self.assertEqual(client.read_audit_record(path), record)
            record.update(audit_revision=3, status="completed", answer="{}")
            writer.write(record, final=True)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)
            self.assertEqual(client.read_audit_record(path), record)  # rev 2 sidecar cannot override rev 3.

    def test_legacy_record_and_mismatched_state(self):
        with tempfile.TemporaryDirectory(prefix="rca_audit_") as folder:
            path = Path(folder) / "fixture.json"
            legacy = {"call_id": "fixture", "system": "system", "user": "user", "status": "completed"}
            path.write_text(json.dumps(legacy), encoding="utf-8")
            self.assertEqual(client.read_audit_record(path), legacy)
            (path.parent / "_states").mkdir()
            (path.parent / "_states" / path.name).write_text(
                json.dumps({"call_id": "wrong", "audit_revision": 2}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "编号不一致"):
                client.read_audit_record(path)

    def test_terminal_snapshot_is_self_contained_with_damaged_old_sidecar(self):
        with tempfile.TemporaryDirectory(prefix="rca_audit_") as folder:
            path = Path(folder) / "fixture.json"
            final = {"call_id": "fixture", "audit_final": True, "audit_revision": 4,
                     "status": "completed", "system": "system", "user": "user", "answer": "{}"}
            path.write_text(json.dumps(final), encoding="utf-8")
            (path.parent / "_states").mkdir()
            (path.parent / "_states" / path.name).write_text("incomplete", encoding="utf-8")
            self.assertEqual(client.read_audit_record(path), final)

    def test_prompt_is_redacted_once_and_intermediate_updates_stay_small(self):
        with tempfile.TemporaryDirectory(prefix="rca_audit_") as folder, patch.object(client, "_SECRETS", {"test-secret"}):
            writer = client._AuditWriter(folder, "fixture")
            record = {"call_id": "fixture", "audit_revision": 1, "system": "test-secret", "user": "u" * 8000}
            with patch.object(client, "_write_audit_json", wraps=client._write_audit_json) as writes:
                writer.write(record)
                record["audit_revision"] = 2
                writer.write(record)
                record["audit_revision"] = 3
                writer.write(record)
                record["audit_revision"] = 4
                writer.write(record, final=True)
            body_writes = [call for call in writes.call_args_list if "system" in call.args[1]]
            self.assertEqual(len(body_writes), 2)
            self.assertTrue(all(call.args[1]["system"] == "[REDACTED]" for call in body_writes))
            state_path = Path(folder) / "_states/fixture.json"
            self.assertLess(state_path.stat().st_size, 1000)


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rca_client_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = 0.0
        self.waits = []
        env = {"PEARLRCA_LLM_AUDIT_DIR": str(self.root / "llm_calls"),
               "PEARLRCA_LLM_CACHE_DIR": str(self.root / "llm_cache"),
               "PEARLRCA_RUN_ATTEMPT_ID": "fixture-attempt", "PEARLRCA_CASE_NAME": "fixture-case",
               "PEARLRCA_RUN_STAGE": "investigation"}
        self.enterContext(patch.dict(os.environ, env))
        self.enterContext(patch.object(client, "_RECORDS", {}))
        self.enterContext(patch.object(client, "_event"))
        self.enterContext(patch.object(client.time, "perf_counter", lambda: self.clock))
        self.enterContext(patch.object(client.time, "sleep", self.sleep))
        self.enterContext(patch.object(client, "request_slot", self.slot))
        grid = types.ModuleType("shared.time_grid")
        grid.bucket_seconds = lambda: 10
        self.enterContext(patch.dict("sys.modules", {"shared.time_grid": grid}))
        self.settings = {"investigation_max_tokens": 131072, "synthesis_max_tokens": 131072,
                         "max_retries": 1, "retry_initial_seconds": 2, "retry_max_seconds": 32,
                         "temperature": None, "timeout_seconds": 600, "base_url": "https://fixture.invalid"}

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.clock += seconds

    @contextmanager
    def slot(self):
        self.clock += 3
        yield

    def make_client(self, callback):
        sdk = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=Mock(side_effect=callback))))
        return client.MiniMaxClient(sdk, "fixture-model", settings=self.settings,
                                    extra_body={"reasoning_effort": "low"}), sdk

    def records(self):
        return [client.read_audit_record(path) for path in (self.root / "llm_calls").glob("*.json")]

    def test_success_preserves_request_and_splits_queue_from_http_time(self):
        def answer(**request):
            self.clock += 7
            self.assertEqual(request["model"], "fixture-model")
            self.assertEqual(request["max_tokens"], 131072)
            self.assertEqual(request["extra_body"], {"reasoning_effort": "low"})
            self.assertNotIn("temperature", request)
            self.assertEqual(request["messages"][1], {"role": "user", "content": "input"})
            live = self.records()[0]
            self.assertEqual(live["status"], "requesting")
            self.assertEqual(live["api_requests"], 1)
            self.assertIn("system", live)
            return response(reported_usage=usage())
        llm, sdk = self.make_client(answer)
        llm.chat("system", "input")
        record = self.records()[0]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["queue_wait_seconds"], 3)
        self.assertEqual(record["http_seconds"], 7)
        self.assertEqual(record["request_attempts"][0]["queue_wait_seconds"], 3)
        self.assertEqual(record["request_attempts"][0]["http_seconds"], 7)
        self.assertEqual(record["request_attempts"][0]["usage"], usage())
        summary = client.runtime_summary("fixture-attempt")
        self.assertEqual(summary["provider_cached_tokens"], 60)
        self.assertEqual(summary["cache_hits"], 0)
        self.assertEqual(summary["total_tokens"], 120)
        self.assertEqual(summary["reasoning_tokens"], 12)
        self.assertEqual(summary["request_attempts"], 1)
        sdk.chat.completions.create.assert_called_once()

    def test_retry_counts_failed_http_duration_and_keeps_original_retry_policy(self):
        import httpx
        from openai import APIConnectionError
        calls = []
        def answer(**request):
            self.clock += 7
            calls.append(request)
            if len(calls) == 1:
                raise APIConnectionError(request=httpx.Request("POST", "https://fixture.invalid"))
            return response(reported_usage=usage())
        llm, _ = self.make_client(answer)
        llm.chat("system", "input")
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(self.waits, [2])
        record = self.records()[0]
        self.assertEqual(record["api_requests"], 2)
        self.assertEqual(record["transport_retry_count"], 1)
        self.assertEqual(record["queue_wait_seconds"], 6)
        self.assertEqual(record["http_seconds"], 14)
        self.assertEqual([a["status"] for a in record["request_attempts"]], ["failed", "completed"])
        self.assertEqual(record["usage"]["prompt_tokens"], 100)
        self.assertEqual(record["missing_usage_responses"], 0)  # The failed transport returned no response.
        self.assertEqual(client.runtime_summary()["failed_requests"], 1)

    def test_content_failure_still_finalizes_full_audit_and_does_not_retry(self):
        def answer(**request):
            self.clock += 7
            return response(content="<think>unfinished", reported_usage=usage())
        llm, sdk = self.make_client(answer)
        with self.assertRaisesRegex(RuntimeError, "未完整结束"):
            llm.chat("system", "input")
        raw = json.loads(next((self.root / "llm_calls").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(raw["status"], "failed")
        self.assertEqual(raw["user"], "input")
        self.assertEqual(raw["http_seconds"], 7)
        sdk.chat.completions.create.assert_called_once()
        self.assertEqual(self.waits, [])

    def test_local_cache_does_not_count_provider_tokens_twice(self):
        llm, sdk = self.make_client(lambda **kwargs: response(reported_usage=usage()))
        first = llm.chat("system", "input")
        second = llm.chat("system", "input")
        self.assertEqual(first, second)
        sdk.chat.completions.create.assert_called_once()
        summary = client.runtime_summary()
        self.assertEqual(summary["logical_calls"], 2)
        self.assertEqual(summary["request_attempts"], 1)
        self.assertEqual(summary["cache_hits"], 1)
        self.assertEqual(summary["provider_cached_tokens"], 60)
        self.assertEqual(summary["total_tokens"], 120)
        cached = next(r for r in self.records() if r["cache_hit"])
        self.assertEqual(cached["queue_wait_seconds"], 0)
        self.assertEqual(cached["http_seconds"], 0)


class ConfigurationTests(unittest.TestCase):
    def test_invalid_slot_count_fails_before_opening_slot_files(self):
        for value in ("0", "65", "no", "2.5"):
            with self.subTest(value=value), patch.dict(os.environ, {"PEARLRCA_LLM_MAX_CONCURRENT_REQUESTS": value}):
                with self.assertRaisesRegex(ValueError, "1 至 64"):
                    with client.request_slot():
                        self.fail("Invalid configuration must not acquire a slot")

    def test_model_uses_case_config_and_can_preserve_original_prompt_order(self):
        import reference
        for enabled in (True, False):
            cfg = types.SimpleNamespace(http_max_concurrent_requests=4, context_prefix_cache=enabled)
            fake = Mock(chat=Mock(return_value='{"action":"stop"}'))
            with self.subTest(enabled=enabled), patch.object(reference, "load_project_client", return_value=fake), patch.dict(os.environ):
                model = reference.Model(Path("fixture-out"), "fixture-case", "fixture-attempt", cfg=cfg)
                self.assertEqual(os.environ["PEARLRCA_LLM_MAX_CONCURRENT_REQUESTS"], "4")
                self.assertEqual(model.call("system", "user context", agent_id="l1:C1"), {"action": "stop"})
                text = fake.chat.call_args.args[1]
                self.assertEqual(text, "user context\n当前角色编号：l1:C1" if enabled else "当前角色编号：l1:C1\nuser context")


if __name__ == "__main__":
    unittest.main()
