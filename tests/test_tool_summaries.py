"""指标生命周期和 Trace 对比的回归测试；不调用模型。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

from test_sql_evidence import write_fixture
from agents.loop import abnormal_refs
from tools.telemetry import Telemetry


def write_rows(path, rows):
    table = pa.Table.from_pylist(rows)
    index = table.schema.get_field_index("time")
    table = table.set_column(index, "time", table["time"].cast(pa.timestamp("ns")))
    pq.write_table(table, path)


class ToolSummaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="rca_summary_test_")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        data = root / "data"
        write_fixture(data)
        for window in ("normal", "abnormal"):
            base = 1_700_000_000_000_000_000 + (60_000_000_000 if window == "abnormal" else 0)
            metrics = []
            for i in range(8):
                for metric, value in (
                    ("k8s.pod.uptime", 100.0 + i if window == "normal" else float(200 + i if i < 4 else i - 4)),
                    ("memory.usage", float(20 + i % 2 if window == "normal" else 50 + i % 2)),
                ):
                    metrics.append({"service_name": "slow", "time": base + i * 1_000_000_000,
                                    "metric": metric, "value": value, "attr.k8s.pod.uid": "pod-1"})
                for metric, value in (("k8s.pod.uptime", float((100 if window == "normal" else 200) + i)),
                                      ("memory.usage", float(20 + i % 2 if window == "normal" else 40 + i % 2))):
                    metrics.append({"service_name": "steady", "time": base + i * 1_000_000_000,
                                    "metric": metric, "value": value, "attr.k8s.pod.uid": "pod-2"})
                metrics.append({"service_name": "newpod", "time": base + i * 1_000_000_000,
                                "metric": "k8s.pod.uptime", "value": float(100 + i),
                                "attr.k8s.pod.uid": "pod-old" if window == "normal" else "pod-new"})
                metrics.append({"service_name": "unknown", "time": base + i * 1_000_000_000,
                                "metric": "k8s.pod.uptime", "value": float(100 + i if i < 4 else i - 4),
                                "attr.k8s.pod.uid": None})
            write_rows(data / f"{window}_metrics_sum.parquet", metrics)
            traces = []
            for service, count, duration in (("busy", 60, 20), ("slow", 8, 20 if window == "normal" else 2000)):
                for i in range(count):
                    traces.append({"service_name": service, "trace_id": f"{service}-{i}",
                                   "span_id": f"span-{i}", "parent_span_id": "",
                                   "time": base + i * 1_000_000, "duration": duration * 1_000_000,
                                   "span_name": "GET /work", "attr.span_kind": "Server",
                                   "attr.status_code": "ok"})
            if window == "abnormal":
                traces.append({"service_name": "new", "trace_id": "new-1", "span_id": "new-span",
                               "parent_span_id": "", "time": base, "duration": 50_000_000,
                               "span_name": "GET /new", "attr.span_kind": "Server", "attr.status_code": "ok"})
            write_rows(data / f"{window}_traces.parquet", traces)
        self.tele = Telemetry(data, root / "out")
        self.addCleanup(self.tele.close)

    def test_rank_keeps_ordinary_signal_and_observed_uptime_reset(self):
        result = self.tele.call("analyze_metrics", {"operation": "rank", "source": "metrics_sum"})
        self.assertEqual(result["status"], "ok", result)
        rows = {row["metric"]: row for row in result["rows"] if row["service"] == "slow"}
        self.assertIn("memory.usage", rows)
        self.assertEqual(rows["k8s.pod.uptime"]["lifecycle_signal"], "possible_reset")
        self.assertIsNone(rows["k8s.pod.uptime"]["peak_deviation"])
        steady = [row for row in result["rows"] if row["service"] == "steady"]
        self.assertEqual([row["metric"] for row in steady], ["memory.usage"])
        self.assertEqual(next(row for row in result["rows"] if row["service"] == "newpod")["lifecycle_signal"],
                         "possible_new_instance")
        self.assertEqual(next(row for row in result["rows"] if row["service"] == "unknown")["lifecycle_signal"],
                         "instance_identity_unknown")
        replay = self.tele.call("replay_evidence", {"evidence_ref": result["evidence_refs"][0]})
        self.assertEqual(replay["rows"], result["rows"])

    def test_trace_compare_aligns_before_limit_and_replays(self):
        result = self.tele.call("analyze_traces", {"operation": "operations_compare", "limit": 2})
        self.assertEqual(result["status"], "ok", result)
        rows = {row["service"]: row for row in result["rows"]}
        self.assertEqual(set(rows), {"busy", "slow"})
        self.assertEqual(rows["slow"]["normal_mean_ms"], 20)
        self.assertEqual(rows["slow"]["abnormal_mean_ms"], 2000)
        self.assertEqual(rows["slow"]["comparison_status"], "comparable")
        ref = result["evidence_refs"][0]
        self.assertTrue(abnormal_refs(SimpleNamespace(rt=SimpleNamespace(tele=self.tele)), [ref]))
        self.assertEqual(self.tele.call("replay_evidence", {"evidence_ref": ref})["rows"], result["rows"])

    def test_one_sided_operation_is_not_reported_as_normal(self):
        result = self.tele.call("analyze_traces", {"operation": "operations_compare", "component": "new"})
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["rows"][0]["comparison_status"], "missing_normal")
        self.assertIsNone(result["rows"][0]["normal_mean_ms"])


if __name__ == "__main__":
    unittest.main()
