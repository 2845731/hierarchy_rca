"""离线评估已结束的层级 RCA 批次；入口不参与推理，不读凭据，不调用模型。"""

import argparse
import json
import sys

sys.dont_write_bytecode = True
from collections import Counter
from pathlib import Path
from filesystem import filesystem_path, atomic_write_text

from reference import BASELINE, use_installed_runtime  # 只定位参考工程，不创建模型或读取密钥。

if __name__ == "__main__":
    use_installed_runtime(__file__)
import yaml
from settings import RESULTS
from contracts.agent_output import AgentRCAOutput
from local_answer_contract import LEGACY_FORMAT, LOCAL_FORMAT, LocalAgentRCAOutput
from data.parquet_store import ParquetStore
from evaluation.sql_evidence import replay_evidence
from scripts import paper_reference_scoring as metrics

# 日常运行 python evaluate.py。None 自动选最近结束的 hierarchical_v1 批次。
# 也可在命令行只传一个批次目录：python evaluate.py "D:/.../具体批次"
RUN_DIRECTORY = None
TERMINAL = {"completed", "completed_with_issues", "stopped", "interrupted"}
ANSWER_STATUSES = {"completed", "partial", "budget_exhausted"}
OUTCOME = ("EM", "Precision", "Recall", "F1", "AnySvc")
PROCESS = ("PR_positive", "PR_zero", "NodeF1", "EdgeF1")


def load(path):
    return json.loads(filesystem_path(path).read_text(encoding="utf-8-sig"))


def latest_finished(directory):
    candidates = []
    for run in filesystem_path(directory).iterdir() if filesystem_path(directory).is_dir() else ():
        meta, summary = run / "run_metadata.json", run / "summary.json"
        if not (meta.is_file() and summary.is_file()):
            continue
        try:
            eligible = (
                load(meta).get("pipeline") == "hierarchical_v1" and load(summary).get("status") in TERMINAL
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # 尚未写完或旧格式损坏的目录不是可自动选中的已结束批次。
        if eligible:
            candidates.append((summary.stat().st_mtime_ns, run.name, run))
    if not candidates:
        raise ValueError(f"找不到已结束的 hierarchical_v1 批次：{directory}")
    return max(candidates)[2]


def label_context(directory):
    # 保留旧 scripts/paper_reference_scoring.py 的标签投影；只调用其计分纯函数，
    # 不调用附带文件摘要计算的 read()/truth()。注入记录只用于已有的网络端点约定。
    label = load(directory / "label.json")
    graph = load(directory / "causal_graph.json")
    injection = load(directory / "injection.json")
    roles = {"required", "optional", "not_effective"}
    faults = []
    norm = metrics.norm_service
    for f in label["faults"]:
        if f["role"] not in roles:
            raise ValueError(f"未知根因标签角色：{f['role']}")
        aliases = f.get("also_accept") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        endpoints, _ = metrics.network_endpoints(label, injection, f)
        faults.append(
            {
                "role": f["role"],
                "service": norm(f["service"]),
                "services": {norm(s) for s in [f["service"], *aliases, *endpoints]},
                "kind": metrics.norm_kind(f["chaos_type"]),
            }
        )
    mapping = graph["component_to_service"]

    def projected(group):
        return {
            norm(mapping[node["component"] if isinstance(node, dict) else node])
            for node in graph.get(group, [])
        } - metrics.LOAD

    edges = clean_edges(label["service_edges"])
    return {
        "system": label["system"],
        "applicable": label["process_applicable"],
        "faults": faults,
        "edges": edges,
        "alarms": projected("alarm_nodes"),
        "nodes": projected("nodes") | {s for edge in edges for s in edge},
    }


def clean_edges(pairs):
    pairs = {(metrics.norm_service(a), metrics.norm_service(b)) for a, b in pairs}
    return {pair for pair in pairs if pair[0] != pair[1] and not (set(pair) & metrics.LOAD)}


def score_answer(answer, truth):
    roots = answer["root_causes"]
    typed = metrics.root_score(roots, truth["faults"], prediction_is_standard=True)
    service = metrics.root_score(roots, truth["faults"], typed=False)
    outcome = {
        "EM": float(typed["exact"]),
        "Precision": typed["precision"],
        "Recall": typed["recall"],
        "F1": typed["f1"],
        "AnySvc": float(bool(service["hit_services"])),
    }
    # 与旧 prediction.json 投影一致：所有根服务及所有预测边的端点，随后去掉测量端点。
    raw_edges = [(e["from"], e["to"]) for e in answer["propagation"]]
    nodes = {metrics.norm_service(s) for pair in raw_edges for s in pair}
    nodes.update(metrics.norm_service(r["service"]) for r in roots)
    nodes -= metrics.LOAD
    edges = clean_edges(raw_edges)
    if truth["applicable"]:
        hits = set(service["hit_services"]) - metrics.LOAD
        process = {
            "NodeF1": metrics.prf(nodes, truth["nodes"])["f1"],
            "EdgeF1": metrics.prf(edges, truth["edges"])["f1"],
        }
        for key, zero in (("PR_positive", False), ("PR_zero", True)):
            process[key] = float(any(metrics.reachable(s, truth["alarms"], edges, zero) for s in hits))
    else:
        process = dict.fromkeys(PROCESS)
    counts = {k: typed[k] for k in ("tp", "fp", "fn", "ignored_optional")}
    return {**outcome, **process}, counts


def grade_case(case, case_output, case_dir, batch_row, runtime, answer_format=LEGACY_FORMAT):
    truth = label_context(case_dir)
    manifest_path = filesystem_path(case_output / "run_manifest.json")
    started = batch_row is not None or manifest_path.is_file()
    row = {
        "case": case,
        "system": truth["system"],
        "started": started,
        "process_applicable": truth["applicable"],
        "valid_answer": False,
        "sql_query_counts": {},
        "metrics": dict.fromkeys((*OUTCOME, *PROCESS, "SQLExec")),
    }
    if not started:
        return dict(row, status="not_started")

    # 运行失败、答案缺失或结构无效保留在已启动分母里：不能用空集合交集获得分数。
    failure = {k: 0.0 for k in (*OUTCOME, "SQLExec")}
    failure.update(dict.fromkeys(PROCESS, 0.0 if truth["applicable"] else None))
    row["metrics"] = failure
    row["status"] = (batch_row or {}).get("status", "manifest_missing")
    if not manifest_path.is_file():
        return dict(row, error="已启动但缺少 run_manifest.json")
    try:
        manifest = load(manifest_path)
        row["status"] = manifest["status"]
        if row["status"] not in ANSWER_STATUSES or manifest.get("has_answer") is not True:
            return dict(row, error="案例未成功提交正式答案")
        if batch_row and (
            batch_row["status"] not in ANSWER_STATUSES or batch_row.get("has_answer") is not True
        ):
            return dict(row, error="批次未登记成功提交，不能把残留答案计为成功")
        if answer_format == LEGACY_FORMAT:
            answer_type = AgentRCAOutput
        elif answer_format == LOCAL_FORMAT:
            answer_type = LocalAgentRCAOutput
        else:
            raise ValueError(f"未知答案格式：{answer_format}")
        answer = answer_type.model_validate(load(case_output / "agent_answer.json")).model_dump(
            mode="json", by_alias=True
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return dict(row, error=f"缺少或无法验证正式输出：{exc}")

    scores, counts = score_answer(answer, truth)
    sql = replay_evidence(answer, ParquetStore(case_dir, runtime), zero_evidence_score=0.0)
    checks = [{k: v for k, v in item.items() if k != "sample"} for item in sql["items"]]
    return dict(
        row,
        valid_answer=True,
        metrics={**scores, "SQLExec": sql["sql_exec"]},
        root_counts=counts,
        sql_query_counts=dict(Counter(i["status"] for i in checks)),
        sql_checks=checks,
    )


def means(rows, *, planned=False):
    use = list(rows) if planned else [r for r in rows if r["started"]]

    def group(keys, selected):
        return {
            "denominator": len(selected),
            **{
                k: sum((r["metrics"][k] or 0.0) for r in selected) / len(selected) if selected else None
                for k in keys
            },
        }

    return {
        "outcome": group(OUTCOME, use),
        "process": group(PROCESS, [r for r in use if r["process_applicable"]]),
        "evidence": group(("SQLExec",), use),
    }


def evaluate(run):
    # 调度持批次锁，并在派发 worker 前持久化 running；残留 worker 也须验证此状态。
    from batch_resume import batch_lock
    run = Path(run).resolve()
    with batch_lock(run):
        summary = load(run / "summary.json")
        if any(row.get("status") in {"running", "interrupted"}
               for row in summary.get("cases", []) if isinstance(row, dict)):
            raise ValueError("批次包含运行中或中断的案例，请先断点继续或等待案例结束后再评估。")
        return _evaluate_locked(run)


def _evaluate_locked(run):
    run = Path(run).resolve()
    meta, summary = load(run / "run_metadata.json"), load(run / "summary.json")
    if meta.get("pipeline") != "hierarchical_v1" or summary.get("status") not in TERMINAL:
        raise ValueError("只评估已结束且 pipeline=hierarchical_v1 的运行。")
    # 原批次没有版本字段时视为第 0 版；每次续跑或重跑会递增版本。
    revision = summary.get("result_revision", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("批次 result_revision 必须是非负整数。")
    answer_format = meta.get("answer_format", LEGACY_FORMAT)
    if answer_format not in (LEGACY_FORMAT, LOCAL_FORMAT):
        raise ValueError(f"未知答案格式：{answer_format}")
    planned = meta["planned_cases"]
    if (
        not isinstance(planned, list)
        or not planned
        or any(not isinstance(c, str) or not c or Path(c).name != c or c in {".", ".."} for c in planned)
        or len(set(planned)) != len(planned)
    ):
        raise ValueError("批次 planned_cases 不是有效、非重复的案例清单。")
    records = summary["cases"]
    by_case = {item["case"]: item for item in records}
    if len(by_case) != len(records) or not set(by_case) <= set(planned):
        raise ValueError("批次案例记录与计划清单不一致。")
    cases_dir = (Path(meta["dataset_root"]) / "cases").resolve()
    runtime = yaml.safe_load((BASELINE / "configs/runtime.yaml").read_text(encoding="utf-8"))
    rows = []
    for case in planned:
        try:
            rows.append(grade_case(case, run / case, cases_dir / case, by_case.get(case), runtime, answer_format))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(
                f"评估器无法读取/处理案例 {case}；不将标签或数据读取错误当作诊断零分：{exc}"
            ) from exc
    started = sum(r["started"] for r in rows)
    valid = sum(r["valid_answer"] for r in rows)
    report = {
        "run": str(run),
        "source_result_revision": revision,
        "dataset_root": str(cases_dir.parent),
        "batch_status": summary["status"],
        "answer_format": answer_format,
        "planned": len(rows),
        "started": started,
        "not_started": len(rows) - started,
        "valid_predictions": valid,
        "started_without_valid_answer": started - valid,
        "case_statuses": dict(Counter(r["status"] for r in rows)),
        "score_unit": "0..1",
        "started_case_means": means(rows),
        "all_planned_conservative_means": means(rows, planned=True),
        "sql_query_counts": dict(sum((Counter(r["sql_query_counts"]) for r in rows), Counter())),
        "conventions": {
            "reference": "旧实验 paper_reference_scoring 的服务规范化、故障类型表、网络端点、根因匹配、图集合与有向可达性。是本地口径，不是官方 SDK。",
            "fault_kind": "数据集原始 chaos_type 按论文映射；正式答案的标准 fault_kind 原样比较，不重新映射。",
            "labels": "required 必须命中；optional 匹配后不计错；仅匹配 not_effective 视为误报。类型未知不猜映射。",
            "denominators": "主结果按已启动案例；失败/无正式输出记零。另列全部计划案例保守均值，未启动仅在该口径中记零。",
            "process": "仅 process_applicable=true 进入传播分母；PR_positive 要求至少一条边，PR_zero 也接受根本身为告警。均按正确服务、不要求类型正确作为起点。",
            "sql": "每个案例分别用新连接重放每条正式证据的 SQL，非空才计 OK。SQLExec 先按案例计分再求均值；无证据记 0。不判定 SQL 是否支持语义。",
        },
    }
    out = run / "evaluation"
    filesystem_path(out).mkdir(exist_ok=True)
    # 评分可能耗时较长；期间重新运行或改变结果时，不保存过期评分。
    latest = load(run / "summary.json")
    if latest.get("status") not in TERMINAL or latest.get("result_revision", 0) != revision:
        raise ValueError("评分期间批次结果发生变化，请待运行结束后重新评估。")
    for name, data in (("cases.json", rows), ("summary.json", report)):
        path = out / name
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    # 此标记只由续跑入口创建。最新结果已评分时移除；旧批次无需标记。
    marker = filesystem_path(out / "STALE.json")
    if marker.is_file():
        latest = load(run / "summary.json")
        if latest.get("status") in TERMINAL and latest.get("result_revision", 0) == revision:
            marker.unlink()
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", nargs="?", type=Path, help="已结束批次目录；不填则使用文件顶部设置")
    run = parser.parse_args().run_directory or RUN_DIRECTORY or latest_finished(RESULTS)
    result = evaluate(run)
    print(
        f"评分目录：{Path(run).resolve() / 'evaluation'}；计划 {result['planned']}，"
        f"已启动 {result['started']}，有效答案 {result['valid_predictions']}，未启动 {result['not_started']}。"
    )
    print(json.dumps(result["started_case_means"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
