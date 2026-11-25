"""断点续跑的批次分类与历史归档；测试仅使用临时目录。"""

import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from batch_resume import archive_case, batch_lock, case_lock, load_batch, select_cases


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class BatchResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "result"
        self.root.mkdir()
        self.names = ["batch-completed", "batch-failed", "batch-empty", "batch-unstarted"]
        self.metadata = {"pipeline": "hierarchical_v1", "planned_cases": self.names}
        rows = [
            {"case": self.names[0], "case_handle": "case_0001", "status": "completed", "has_answer": True},
            {"case": self.names[1], "case_handle": "case_0002", "status": "technical_failure", "has_answer": False},
            {"case": self.names[2], "case_handle": "case_0003", "status": "insufficient_evidence", "has_answer": True},
        ]
        self.summary = {"status": "stopped", "planned": 4, "cases": rows, "not_started": 1}
        write_json(self.root / "run_metadata.json", self.metadata)
        write_json(self.root / "summary.json", self.summary)
        for row in rows:
            folder = self.root / row["case"]
            folder.mkdir()
            write_json(folder / "run_manifest.json", row)
            write_json(folder / "process.json", {"status": row["status"], "has_answer": row["has_answer"]})
            if row["has_answer"]:
                write_json(folder / "agent_answer.json", {"root_causes": [], "propagation": []})
                write_json(folder / "prediction.json", {"root_causes": []})

    def test_resume_preserves_completed_and_selects_failed_and_unstarted(self):
        meta, summary = load_batch(self.root)
        selected = select_cases(self.root, meta, summary, resume=True)
        self.assertEqual(selected, [(self.names[1], "case_0002"), (self.names[3], "case_0004")])

    def test_explicit_cases_resolve_stable_handles_and_do_not_dispatch_remaining(self):
        selected = select_cases(self.root, self.metadata, self.summary,
                                cases=["case_0003", self.names[0]])
        self.assertEqual(set(selected), {(self.names[2], "case_0003"), (self.names[0], "case_0001")})
        self.assertNotIn((self.names[3], "case_0004"), selected)

    def test_status_selection_only_picks_matching_final_case(self):
        selected = select_cases(self.root, self.metadata, self.summary, status="insufficient_evidence")
        self.assertEqual(selected, [(self.names[2], "case_0003")])

    def test_archive_moves_only_selected_case_and_retains_original_files(self):
        source = self.root / self.names[1]
        (source / "run.log").write_text("old error", encoding="utf-8")
        backup = archive_case(self.root, self.names[1], "session-fixture")
        self.assertFalse(source.exists())
        self.assertEqual((Path(backup) / "run.log").read_text(encoding="utf-8"), "old error")
        self.assertTrue((self.root / self.names[0] / "run_manifest.json").is_file())

    def test_rejects_unknown_identifier_and_path_traversal(self):
        for value in ("case_9999", "../outside"):
            with self.subTest(value=value), self.assertRaises((ValueError, FileNotFoundError)):
                select_cases(self.root, self.metadata, self.summary, cases=[value])

    def test_interrupted_attempt_is_selected_with_original_handle(self):
        folder = self.root / self.names[3]
        folder.mkdir()
        write_json(folder / "run_manifest.json", {
            "case": self.names[3], "case_handle": "case_0004",
            "status": "running", "has_answer": False,
        })
        self.assertIn((self.names[3], "case_0004"),
                      select_cases(self.root, self.metadata, self.summary, resume=True))

    def test_manifest_completion_recovers_when_summary_was_not_flushed(self):
        row = self.summary["cases"][0]
        row.update(status="running", has_answer=False)
        selected = select_cases(self.root, self.metadata, self.summary, resume=True)
        self.assertNotIn((self.names[0], "case_0001"), selected)
        recovered = next(r for r in self.summary["cases"] if r["case"] == self.names[0])
        self.assertEqual(recovered["status"], "completed")
        self.assertTrue(recovered["has_answer"])

    def test_new_attempt_before_archive_does_not_restore_old_completed_result(self):
        # 新派发意图已写入汇总，但进程在旧目录归档前退出。
        self.summary["cases"][0].update(status="running", has_answer=False, session_id="new-session")
        selected = select_cases(self.root, self.metadata, self.summary, resume=True)
        self.assertIn((self.names[0], "case_0001"), selected)
        self.assertEqual(self.summary["cases"][0]["status"], "running")

    def test_completed_case_missing_manifest_is_rejected(self):
        (self.root / self.names[0] / "run_manifest.json").unlink()
        with self.assertRaisesRegex(ValueError, "缺失"):
            select_cases(self.root, self.metadata, self.summary, resume=True)

    def test_conflicting_terminal_status_is_rejected(self):
        self.summary["cases"][0]["status"] = "partial"
        with self.assertRaisesRegex(ValueError, "不一致"):
            select_cases(self.root, self.metadata, self.summary, resume=True)

    def test_duplicate_case_in_summary_is_rejected(self):
        self.summary["cases"].append(dict(self.summary["cases"][0]))
        write_json(self.root / "summary.json", self.summary)
        with self.assertRaisesRegex(ValueError, "重复"):
            load_batch(self.root)

    def test_explicit_selection_rejects_duplicate_handle_and_name(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            select_cases(self.root, self.metadata, self.summary,
                         cases=["case_0001", self.names[0]])

    def test_batch_lock_blocks_second_writer(self):
        with batch_lock(self.root):
            with self.assertRaises((OSError, RuntimeError, ValueError)):
                with batch_lock(self.root):
                    self.fail("应拒绝同一批次的并发写入")

    def test_case_lock_blocks_orphan_worker_from_competing_attempt(self):
        # 模拟上一调度器退出后，旧 worker 仍持有案例锁。
        with case_lock(self.root, "case_0001"):
            with self.assertRaises((OSError, RuntimeError, ValueError)):
                with case_lock(self.root, "case_0001"):
                    self.fail("同一案例不能被两个执行进程同时写入")
            # 不同案例可以独立持锁，避免单案例锁阻塞整个批次。
            with case_lock(self.root, "case_0002"):
                pass

    def test_late_worker_rejected_after_session_changes(self):
        from run import run_one

        row = self.summary["cases"][0]
        row.update(status="running", has_answer=False, session_id="new-session")
        write_json(self.root / "summary.json", self.summary)
        dataset_case = self.root / "source" / self.names[0]
        destination = self.root / self.names[0]
        with patch("run._run_one_locked") as execute:
            with self.assertRaisesRegex(RuntimeError, "失效"):
                run_one(dataset_case, destination, object(), "case_0001", session_id="old-session")
            execute.assert_not_called()
        self.assertTrue((destination / "run_manifest.json").is_file())

    def test_worker_with_current_running_session_is_admitted(self):
        from run import run_one

        self.summary["cases"][0].update(status="running", has_answer=False, session_id="current")
        write_json(self.root / "summary.json", self.summary)
        dataset_case = self.root / "source" / self.names[0]
        destination = self.root / self.names[0]
        with patch("run._run_one_locked", return_value={"status": "completed"}) as execute:
            result = run_one(dataset_case, destination, object(), "case_0001", session_id="current")
        self.assertEqual(result["status"], "completed")
        execute.assert_called_once()

    def test_rejects_batch_reserved_name_ignoring_case(self):
        self.metadata["planned_cases"][0] = "_SESSIONS"
        write_json(self.root / "run_metadata.json", self.metadata)
        with self.assertRaisesRegex(ValueError, "冲突"):
            load_batch(self.root)

    def test_rejects_case_names_differing_only_by_letter_case(self):
        self.metadata["planned_cases"] = ["Batch-A", "batch-a"]
        self.summary.update(planned=2, cases=[])
        write_json(self.root / "run_metadata.json", self.metadata)
        write_json(self.root / "summary.json", self.summary)
        with self.assertRaises(ValueError):
            load_batch(self.root)

    def test_rejects_real_name_that_ambiguously_matches_other_case_handle(self):
        self.metadata["planned_cases"] = ["first-case", "case_0001"]
        self.summary.update(planned=2, cases=[])
        write_json(self.root / "run_metadata.json", self.metadata)
        write_json(self.root / "summary.json", self.summary)
        with self.assertRaises(ValueError):
            load_batch(self.root)

    def test_case_lock_rejects_invalid_handle(self):
        with self.assertRaises(ValueError):
            with case_lock(self.root, "../case_0001"):
                self.fail("锁路径不能包含目录穿越")


class BatchResumeArgumentTests(unittest.TestCase):
    def test_resume_and_multiple_case_arguments(self):
        from run import parse_args

        args = parse_args(["--resume", "--run-dir", "D:/result/run"])
        self.assertTrue(args.resume)
        self.assertEqual(args.run_dir, "D:/result/run")
        args = parse_args(["--run-dir", "D:/result/run", "--case", "case_0023",
                           "--case", "case_0213"])
        self.assertEqual(args.case, ["case_0023", "case_0213"])
        args = parse_args(["--run-dir", "D:/result/run", "--rerun-status", "insufficient_evidence"])
        self.assertEqual(args.rerun_status, "insufficient_evidence")

    def test_conflicting_or_incomplete_arguments_fail(self):
        from run import parse_args

        for argv in (["--resume"], ["--run-dir", "D:/result/run"],
                     ["--resume", "--run-dir", "D:/result/run", "--case", "case_0001"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(argv)

    def test_rerun_upserts_case_without_duplicate_and_retains_planned_count(self):
        from run import _upsert_case, _update_summary_counts

        report = {"cases": [{"case": "a", "case_handle": "case_0001", "status": "completed"}]}
        replacement = {"case": "a", "case_handle": "case_0001", "status": "technical_failure"}
        _upsert_case(report, replacement)
        _update_summary_counts(report, ["a", "b"])
        self.assertEqual(report["cases"], [replacement])
        self.assertEqual(report["planned"], 2)
        self.assertEqual(report["not_started"], 1)

    def test_recovered_attempt_usage_counted_once(self):
        from run import _account_attempt, _USAGE_KEYS

        report = {"attempt_totals": {key: 0 for key in _USAGE_KEYS}, "attempt_usage_accounted": []}
        recovered = {"attempt_id": "attempt-1", "status": "completed",
                     "model_usage": {"total_tokens": 123, "logical_calls": 3}}
        _account_attempt(report, recovered)
        _account_attempt(report, recovered)
        self.assertEqual(report["attempt_totals"]["total_tokens"], 123)
        self.assertEqual(report["attempt_totals"]["logical_calls"], 3)
        self.assertEqual(report["attempt_usage_accounted"], ["attempt-1"])


if __name__ == "__main__":
    unittest.main()
