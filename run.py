"""唯一预测入口：在本目录运行 python run.py。案例、并发和预算见 settings.py。"""

import sys
import argparse
import hashlib

sys.dont_write_bytecode = True

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import time
import traceback
import uuid

from settings import CONFIG, DATASET, PROJECT, RESULTS, Settings
from filesystem import filesystem_path, preflight_result_path
from reference import Model, use_installed_runtime
from llm_config import PROVIDERS, public_config_summary

if __name__ == "__main__":
    use_installed_runtime(__file__)
from output_contract import save_json
from local_answer_contract import LOCAL_FORMAT
from progress import BatchProgress, case_output
from clients.errors import AllProvidersFailedError, LLMConfigurationError
from clients.minimax_client import (
    redact, runtime_summary, USAGE_TOKEN_FIELDS, USAGE_COUNTER_FIELDS, TIMING_FIELDS,
)

BEIJING = timezone(timedelta(hours=8))


def choose_cases(dataset_root, settings):
    """按目录名选择，不读取含答案的清单。目录真名不传入 agent 提示词。"""
    cases = (dataset_root / "cases").resolve()
    names = settings.case_names or tuple(sorted(p.name for p in cases.iterdir() if p.is_dir()))
    if settings.case_limit is not None and not settings.case_names:
        if settings.case_limit < 1:
            raise ValueError("case_limit 应为正整数或 None")
        names = names[: settings.case_limit]
    if not names or len(names) != len(set(names)):
        raise ValueError("案例列表为空或存在重复项")
    for name in names:
        path = (cases / name).resolve()
        if path.parent != cases or path.name != name or not path.is_dir():
            raise ValueError(f"不是数据集内的案例目录：{name}")
    return list(names)


def run_one(case_dir, folder, config, handle, progress_channel=None, session_id=None):
    """一个子进程只处理一个案例，使原客户端的案例日志、缓存和阶段环境互不串用。"""
    from batch_resume import case_lock

    # 即便主进程被强制结束，正在写入的 worker 仍持有案例锁。
    with case_lock(folder.parent, handle, blocking=True):
        if session_id:
            summary = json.loads(filesystem_path(folder.parent / "summary.json").read_text(encoding="utf-8-sig"))
            row = next((row for row in summary.get("cases", []) if row.get("case") == case_dir.name), None)
            if not row or row.get("case_handle") != handle or row.get("session_id") != session_id or row.get("status") != "running":
                raise RuntimeError("案例派发已失效或已被新会话接管，旧 worker 不再写入")
        return _run_one_locked(case_dir, folder, config, handle, progress_channel, session_id)


def _run_one_locked(case_dir, folder, config, handle, progress_channel=None, session_id=None):
    from workflow import run_case

    filesystem_path(folder).mkdir()
    started = time.perf_counter()
    attempt = uuid.uuid4().hex
    result = {"case": case_dir.name, "case_handle": handle, "status": "running", "has_answer": False,
              "attempt_id": attempt, **({"session_id": session_id} if session_id else {})}
    save_json(folder / "run_manifest.json", result)
    with (
        filesystem_path(folder / "run.log").open("w", encoding="utf-8", buffering=1) as log,
        redirect_stdout(log),
        redirect_stderr(log),
        case_output(handle, progress_channel, redactor=redact),
    ):
        try:
            model = Model(folder, case_dir.name, attempt, cfg=config)
            report = run_case(case_dir, folder, model, config, handle)
            result.update(
                {key: report.get(key) for key in ("status", "completion_reason", "budget", "counts")}
            )
            result["all_investigation_failed"] = report.get("all_investigation_failed", False)
            result.update({key: report[key] for key in ("failure_stage", "error_type") if key in report})
            answer = report.get("answer") or {}
            result["synthesis_report"] = report.get("synthesis_report", {})
            empty_after_meta_failure = bool(report.get("meta_failure")) and not (
                answer.get("root_causes") or answer.get("propagation")
            )
            result["has_answer"] = filesystem_path(folder / "agent_answer.json").is_file() and result[
                "status"
            ] != "technical_failure" and not result["all_investigation_failed"] and not empty_after_meta_failure and bool(answer.get("root_causes"))
        except Exception as error:
            # 保留具体错误；原客户端已脱敏的接口异常决定是否停止派发下一例。
            result.update(
                status="technical_failure",
                error=redact(f"{type(error).__name__}: {error}"),
                stop_batch=isinstance(error, (AllProvidersFailedError, LLMConfigurationError)),
                filesystem_error=(f"{type(error).__name__}:{getattr(error, 'winerror', None)}:{getattr(error, 'errno', None)}"
                                  if isinstance(error, OSError) else None),
            )
            log.write(redact(traceback.format_exc()))
        finally:
            result.update(
                run_elapsed_seconds=round(time.perf_counter() - started, 2),
                model_usage=runtime_summary(attempt),
            )
            save_json(folder / "run_manifest.json", result)
            save_json(
                folder / "process.json", {"status": result["status"], "has_answer": result["has_answer"]}
            )
            # 索引相对于案例目录，不依赖外部工作目录。
            fs_folder = filesystem_path(folder)
            files = sorted(p.relative_to(fs_folder).as_posix() for p in fs_folder.rglob("*") if p.is_file())
            save_json(folder / "artifact_index.json", {"base_directory": ".", "files": files})
    return result


def _run_cases(config, dataset_root, run_dir, names, report, progress):
    """进程间只传短小的进度摘要；主进程边等案例边打印，不等整例调查结束。"""
    from batch_resume import case_lock

    jobs, next_index, stop = {}, 0, False
    consecutive_filesystem_errors = 0
    previous_filesystem_error = None
    session_id = "initial_" + uuid.uuid4().hex
    ctx = multiprocessing.get_context("spawn")
    # put 返回时事件已经入队，收到案例的最终结果后可以先输出剩余进展，再打印结案。
    with (
        ctx.Manager() as manager,
        ProcessPoolExecutor(max_workers=config.case_workers, max_tasks_per_child=1, mp_context=ctx) as pool,
    ):
        channel = manager.Queue()
        while jobs or (next_index < len(names) and not stop):
            dispatched = False
            while not stop and next_index < len(names) and len(jobs) < config.case_workers:
                name = names[next_index]
                handle = f"case_{next_index + 1:04d}"
                with case_lock(run_dir, handle):
                    _upsert_case(report, {"case": name, "case_handle": handle, "status": "running",
                                          "has_answer": False, "session_id": session_id})
                    report["not_started"] = len(names) - next_index - 1
                    save_json(run_dir / "summary.json", report)
                    future = pool.submit(
                        run_one, dataset_root / "cases" / name, run_dir / name, config, handle, channel, session_id
                    )
                jobs[future] = (name, handle)
                next_index += 1
                report["not_started"] = len(names) - next_index
                progress.show("batch_case_start", case_handle=handle, detail=name)
                dispatched = True
            if dispatched:
                progress.bar(sum(row.get("status") != "running" for row in report["cases"]), len(jobs), report["not_started"])

            finished = wait(jobs, timeout=0.25, return_when=FIRST_COMPLETED).done
            progress.drain(channel)
            for future in finished:
                name, handle = jobs.pop(future)
                try:
                    row = future.result()
                except Exception as error:
                    row = {
                        "case": name,
                        "case_handle": handle,
                        "status": "technical_failure",
                        "has_answer": False,
                        "error": redact(f"{type(error).__name__}: {error}"),
                        "stop_batch": True,
                    }
                    save_json(run_dir / name / "run_manifest.json", row)
                _upsert_case(report, row)
                fs_error = row.get("filesystem_error")
                consecutive_filesystem_errors = (consecutive_filesystem_errors + 1 if fs_error and fs_error == previous_filesystem_error else int(bool(fs_error)))
                previous_filesystem_error = fs_error
                if consecutive_filesystem_errors >= 2:
                    row["stop_batch"] = True
                    row["error"] = (row.get("error", "") + "；连续两个案例发生同类文件系统错误，停止派发")
                stop = stop or row.get("stop_batch", False)
                answer = "已生成正式答案" if row["has_answer"] else "未生成正式答案"
                why = row.get("completion_reason") or row.get("error", "")
                progress.show(
                    "batch_case_end",
                    case_handle=handle,
                    status=row["status"],
                    elapsed_seconds=row.get("run_elapsed_seconds"),
                    detail=f"{answer}；{why}",
                )
                save_json(run_dir / "summary.json", report)
            if finished:
                progress.bar(sum(row.get("status") != "running" for row in report["cases"]), len(jobs), report["not_started"])
            progress.heartbeat()
    return stop


def validate_config(config):
    """启动任意模式前验证当前运行设置。"""
    if (
        min(
            config.case_workers,
            config.l1_workers,
            config.initial_candidates,
            config.chains_per_l1,
            config.reviews_per_chain,
            config.max_hops,
            config.max_model_calls,
            config.max_tool_calls,
        )
        < 1
    ):
        raise ValueError("并发、初始起点、路径长度和调查预算必须大于 0")
    if (
        config.initial_candidates > config.max_candidates
        or not 2 <= config.meta_reserve < config.max_model_calls
    ):
        raise ValueError("请检查候选上限及 Meta 的调用预留")
    if not config.progress_interval_seconds >= 1:
        raise ValueError("等待提示间隔至少为 1 秒")
    if (type(config.reseed_model_reserve) is not int or type(config.reseed_tool_reserve) is not int
            or config.reseed_model_reserve < 0 or config.reseed_tool_reserve < 0
            or config.reseed_model_reserve + config.meta_reserve >= config.max_model_calls
            or config.reseed_tool_reserve >= config.max_tool_calls):
        raise ValueError("请检查补种模型及工具预留，预留须小于相应总预算")
    if config.max_json_retries < 0:
        raise ValueError("JSON 格式纠错重试次数不能为负数")
    if (type(config.synthesis_max_attempts) is not int or config.synthesis_max_attempts < 1
            or type(config.synthesis_lookup_max) is not int or config.synthesis_lookup_max < 1
            or type(config.synthesis_max_meta_returns) is not int or config.synthesis_max_meta_returns < 0
            or type(config.synthesis_final_max_retries) is not int or config.synthesis_final_max_retries < 0
            or type(config.synthesis_recovery_followups_per_round) is not int or config.synthesis_recovery_followups_per_round < 1
            or config.meta_synthesis_reserve < max(config.synthesis_max_attempts + config.synthesis_lookup_max,
                                                    1 + config.synthesis_final_max_retries)
            or config.meta_reserve < config.meta_synthesis_reserve + 1):
        raise ValueError("须为回查、最终综合及一次 Meta 结案在案例总预算内预留调用次数")
    if (min(config.synthesis_context_limit, config.synthesis_output_reserve,
            config.synthesis_context_margin) < 1
            or config.synthesis_output_reserve + config.synthesis_context_margin >= config.synthesis_context_limit):
        raise ValueError("请检查综合上下文、输出预留及余量设置")
    if any(row.get("synthesis_max_tokens") is not None
           and row["synthesis_max_tokens"] > config.synthesis_output_reserve for row in PROVIDERS):
        raise ValueError("综合输出预留不能小于任何已启用模型的单次输出上限")
    if type(config.max_consecutive_repeated_queries) is not int or config.max_consecutive_repeated_queries < 1:
        raise ValueError("连续重复查询上限必须为正整数")
    if (type(config.context_exact_dedup) is not bool or type(config.context_prefix_cache) is not bool):
        raise ValueError("上下文传输优化开关须为布尔值")
    if type(config.http_max_concurrent_requests) is not int or not 1 <= config.http_max_concurrent_requests <= 64:
        raise ValueError("HTTP 请求槽上限须为 1 至 64 的整数；请依据实际接口额度配置")


def run_batch(config: Settings = CONFIG, *, dataset_root=DATASET, results_root=RESULTS):
    """返回实际运行目录。独立测试可指定临时输出根目录，正常入口固定使用 RESULTS。"""
    validate_config(config)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    names = choose_cases(Path(dataset_root), config)
    routing = public_config_summary(config)
    primary = re.sub(r"[^A-Za-z0-9_.-]", "_", routing["providers"][0]["model"])
    now = datetime.now(BEIJING)
    run_dir = Path(results_root) / f"{now:%Y%m%d_%H%M%S_%f}_{primary}"
    filesystem_path(run_dir).mkdir(parents=True, exist_ok=False)
    # 新建批次也持锁，防止另一个进程误把仍在运行的批次当作中断批次恢复。
    from batch_resume import batch_lock
    with batch_lock(run_dir):
        return _run_new_batch_at(config, Path(dataset_root), names, routing, primary, now, run_dir)


def _run_new_batch_at(config, dataset_root, names, routing, primary, now, run_dir):
    preflight_result_path(run_dir, max(names, key=len))
    save_json(
        run_dir / "run_metadata.json",
        {
            "pipeline": "hierarchical_v1",
            "started_at": now.isoformat(),
            "dataset_root": str(Path(dataset_root).resolve()),
            "planned_cases": names,
            "model_configuration": routing,
            "python": sys.executable,
            "output_contract": "local OpenRCA 2.0 adaptation without evidence.claim; not the paper's original output schema",
            "answer_format": LOCAL_FORMAT,
        },
    )
    save_json(run_dir / "effective_config.json", asdict(config))
    shutil.copytree(PROJECT / "prompts", filesystem_path(run_dir / "prompt_snapshot"))
    report = {"status": "running", "planned": len(names), "cases": [], "not_started": len(names)}
    save_json(run_dir / "summary.json", report)
    with filesystem_path(run_dir / "run.log").open("w", encoding="utf-8") as log:
        progress = BatchProgress(log, len(names), config.progress_interval_seconds)
        progress.show("batch_start", total=len(names), model=primary, detail=str(run_dir))
        try:
            stop = _run_cases(config, Path(dataset_root), run_dir, names, report, progress)
            report["status"] = (
                "stopped"
                if stop
                else "completed"
                if all(r["status"] == "completed" for r in report["cases"])
                else "completed_with_issues"
            )
        except KeyboardInterrupt:
            report["status"] = "interrupted"
            raise
        except Exception:
            report["status"] = "technical_failure"
            raise
        finally:
            report["finished_at"] = datetime.now(BEIJING).isoformat()
            report["totals"] = {
                key: sum(row.get("model_usage", {}).get(key, 0) for row in report["cases"])
                for key in _USAGE_KEYS
            }
            save_json(run_dir / "summary.json", report)
            answers = sum(bool(row["has_answer"]) for row in report["cases"])
            ended = sum(row.get("status") != "running" for row in report["cases"])
            progress.show(
                "batch_end",
                status=report["status"],
                detail=(
                    f"已记录结束 {ended}/{len(names)} 例；有正式答案 {answers} 例，"
                    f"无正式答案 {ended - answers} 例，未派发 {report['not_started']} 例"
                ),
            )
    return run_dir


_USAGE_KEYS = ("logical_calls", "request_attempts", "retries", "fallback_calls", "cache_hits",
               *USAGE_TOKEN_FIELDS, *USAGE_COUNTER_FIELDS, *TIMING_FIELDS)


def _upsert_case(report, row):
    """一个案例在汇总中只保留最新一次结果。"""
    for i, old in enumerate(report["cases"]):
        if old["case"] == row["case"]:
            report["cases"][i] = row
            return
    report["cases"].append(row)


def _update_summary_counts(report, planned_names):
    report["planned"] = len(planned_names)
    report["not_started"] = len(set(planned_names) - {row["case"] for row in report["cases"]})
    report["totals"] = {key: sum(row.get("model_usage", {}).get(key, 0) for row in report["cases"])
                        for key in _USAGE_KEYS}


def _account_attempt(report, row):
    """按尝试编号去重计费；崩溃后从已结案 manifest 恢复时也可重算。"""
    attempt_id = row.get("attempt_id")
    accounted = report.setdefault("attempt_usage_accounted", [])
    if not attempt_id or attempt_id in accounted or row.get("status") == "running":
        return
    for key in _USAGE_KEYS:
        report["attempt_totals"][key] += row.get("model_usage", {}).get(key, 0)
    accounted.append(attempt_id)


def _code_hashes():
    """只记录代码内容的摘要，不保存环境变量或密钥。"""
    paths = list(PROJECT.glob("*.py")) + list((PROJECT / "agents").glob("*.py")) + list((PROJECT / "clients").glob("*.py")) + list((PROJECT / "tools").glob("*.py"))
    return {p.relative_to(PROJECT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def _run_existing_cases(config, dataset_root, run_dir, selected, report, progress, session_id):
    """只派发所选案例，原计划编号不改变；派发前先将旧目录归档。"""
    from batch_resume import archive_case, case_lock

    jobs, next_index, stop = {}, 0, False
    consecutive_filesystem_errors, previous_filesystem_error = 0, None
    ctx = multiprocessing.get_context("spawn")
    with (ctx.Manager() as manager,
          ProcessPoolExecutor(max_workers=config.case_workers, max_tasks_per_child=1, mp_context=ctx) as pool):
        channel = manager.Queue()
        while jobs or (next_index < len(selected) and not stop):
            dispatched = False
            while not stop and next_index < len(selected) and len(jobs) < config.case_workers:
                name, handle = selected[next_index]
                # 先持久化意图：归档后若父进程退出，下次仍会识别为未完成。
                running = {"case": name, "case_handle": handle, "status": "running",
                           "has_answer": False, "session_id": session_id}
                with case_lock(run_dir, handle):
                    _upsert_case(report, running)
                    _update_summary_counts(report, report["_planned_names"])
                    save_json(run_dir / "summary.json", {k: v for k, v in report.items() if k != "_planned_names"})
                    archive_case(run_dir, name, session_id)
                    future = pool.submit(run_one, dataset_root / "cases" / name, run_dir / name,
                                         config, handle, channel, session_id)
                jobs[future] = (name, handle)
                next_index += 1
                progress.show("batch_case_start", case_handle=handle, detail=name)
                dispatched = True
            if dispatched:
                progress.bar(sum(row.get("status") != "running" for row in report["cases"]),
                             len(jobs), report["not_started"])
            finished = wait(jobs, timeout=0.25, return_when=FIRST_COMPLETED).done
            progress.drain(channel)
            for future in finished:
                name, handle = jobs.pop(future)
                try:
                    row = future.result()
                except Exception as error:
                    row = {"case": name, "case_handle": handle, "status": "technical_failure",
                           "has_answer": False, "session_id": session_id,
                           "error": redact(f"{type(error).__name__}: {error}"), "stop_batch": True}
                    filesystem_path(run_dir / name).mkdir(parents=True, exist_ok=True)
                    save_json(run_dir / name / "run_manifest.json", row)
                    save_json(run_dir / name / "process.json", {"status": row["status"], "has_answer": False})
                fs_error = row.get("filesystem_error")
                consecutive_filesystem_errors = (consecutive_filesystem_errors + 1
                    if fs_error and fs_error == previous_filesystem_error else int(bool(fs_error)))
                previous_filesystem_error = fs_error
                if consecutive_filesystem_errors >= 2:
                    row["stop_batch"] = True
                    row["error"] = row.get("error", "") + "；连续两个案例发生同类文件系统错误，停止派发"
                stop = stop or row.get("stop_batch", False)
                _upsert_case(report, row)
                _account_attempt(report, row)
                _update_summary_counts(report, report["_planned_names"])
                save_json(run_dir / "summary.json", {k: v for k, v in report.items() if k != "_planned_names"})
                answer = "已生成正式答案" if row["has_answer"] else "未生成正式答案"
                progress.show("batch_case_end", case_handle=handle, status=row["status"],
                              elapsed_seconds=row.get("run_elapsed_seconds"),
                              detail=f"{answer}；{row.get('completion_reason') or row.get('error', '')}")
            if finished:
                progress.bar(sum(row.get("status") != "running" for row in report["cases"]),
                             len(jobs), report["not_started"])
            progress.heartbeat()
    return stop


def run_existing_batch(config: Settings = CONFIG, *, run_dir, resume=False, cases=None, status=None,
                       dataset_root=DATASET):
    """恢复已有批次或重跑指定案例；每例均从头执行，不使用旧轨迹续跑。"""
    from batch_resume import batch_lock, load_batch, select_cases, safe_child

    validate_config(config)
    run_dir = Path(run_dir).absolute()
    dataset_root = Path(dataset_root).resolve()
    with batch_lock(run_dir):
        metadata, report = load_batch(run_dir)
        if Path(metadata["dataset_root"]).resolve() != dataset_root:
            raise ValueError("当前数据集路径与原批次不一致；请核对 settings.py 中 DATASET")
        # 先记住旧汇总中已计入的尝试，随后 select 才可能补回崩溃前已结案的 manifest。
        if "attempt_totals" not in report:
            report["attempt_totals"] = {key: report.get("totals", {}).get(key, 0) for key in _USAGE_KEYS}
            report["attempt_usage_accounted"] = [row["attempt_id"] for row in report["cases"]
                                                 if row.get("attempt_id") and row.get("status") != "running"]
        else:
            for key in _USAGE_KEYS:
                report["attempt_totals"].setdefault(key, 0)
        previous_rows = list(report["cases"])
        selected = select_cases(run_dir, metadata, report, resume=resume, cases=cases, status=status)
        planned_names = metadata["planned_cases"]
        for row in report["cases"]:
            _account_attempt(report, row)
        # 限定案例必须仍在原始数据集中；绝不允许目录名越出案例根目录。
        cases_root = (dataset_root / "cases").resolve()
        for name, handle in selected:
            case_path = (cases_root / name).resolve()
            if case_path.parent != cases_root or not case_path.is_dir():
                raise ValueError(f"数据集缺少原批次案例：{name}")
        if not selected:
            _update_summary_counts(report, planned_names)
            if report.get("status") in {"running", "interrupted", "technical_failure"} and not any(
                    row.get("status") == "running" for row in report["cases"]):
                report["status"] = ("stopped" if report["not_started"] else
                    "completed" if all(row.get("status") == "completed" for row in report["cases"])
                    else "completed_with_issues")
            if report["cases"] != previous_rows:
                report["result_revision"] = int(report.get("result_revision", 0)) + 1
                if filesystem_path(safe_child(run_dir, "evaluation")).is_dir():
                    save_json(safe_child(run_dir, "evaluation", "STALE.json"), {
                        "result_revision": report["result_revision"],
                        "reason": "已恢复案例结果，请重新评估", "time": datetime.now(BEIJING).isoformat()})
            report["last_session_status"] = "no_cases"
            save_json(run_dir / "summary.json", report)
            print("没有需要运行的案例。")
            return run_dir
        preflight_result_path(run_dir, max((name for name, _ in selected), key=len))
        session_id = datetime.now(BEIJING).strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8]
        session_dir = safe_child(run_dir, "_sessions", session_id)
        filesystem_path(session_dir).mkdir(parents=True, exist_ok=False)
        mode = "resume" if resume else "case" if cases else "status"
        session_record = {"session_id": session_id, "mode": mode, "status": "running",
                  "started_at": datetime.now(BEIJING).isoformat(), "cases": [
                      {"case": name, "case_handle": handle} for name, handle in selected],
                  "model_configuration": public_config_summary(config), "code_sha256": _code_hashes(),
                  "result_revision": int(report.get("result_revision", 0)) + 1}
        save_json(session_dir / "session.json", session_record)
        save_json(session_dir / "effective_config.json", asdict(config))
        shutil.copytree(PROJECT / "prompts", filesystem_path(session_dir / "prompt_snapshot"))
        # 历史批次无此字段时，原 totals 已包含当时所有案例的用量。
        report["result_revision"] = int(report.get("result_revision", 0)) + 1
        report["status"] = "running"
        report["last_session_id"] = session_id
        report["last_session_status"] = "running"
        _update_summary_counts(report, planned_names)
        evaluation_dir = safe_child(run_dir, "evaluation")
        evaluation = filesystem_path(evaluation_dir)
        if evaluation.is_dir():
            save_json(safe_child(run_dir, "evaluation", "STALE.json"), {"result_revision": report["result_revision"],
                      "reason": "案例结果已更新，请重新评估", "time": datetime.now(BEIJING).isoformat()})
        save_json(run_dir / "summary.json", report)
        report["_planned_names"] = planned_names
        with filesystem_path(run_dir / "run.log").open("a", encoding="utf-8") as log:
            progress = BatchProgress(log, len(planned_names), config.progress_interval_seconds)
            progress.show("batch_start", total=len(planned_names), model=public_config_summary(config)["providers"][0]["model"],
                          detail=f"{mode} 会话 {session_id}，本次计划 {len(selected)} 例")
            try:
                stop = _run_existing_cases(config, dataset_root, run_dir, selected, report, progress, session_id)
                report["status"] = ("stopped" if stop or report["not_started"] or any(
                    row.get("status") == "running" for row in report["cases"]) else
                    "completed" if all(row.get("status") == "completed" for row in report["cases"])
                    else "completed_with_issues")
            except KeyboardInterrupt:
                report["status"] = "interrupted"
                raise
            except Exception:
                report["status"] = "technical_failure"
                raise
            finally:
                report.pop("_planned_names", None)
                _update_summary_counts(report, planned_names)
                report["finished_at"] = datetime.now(BEIJING).isoformat()
                session_rows = [row for row in report["cases"] if row.get("session_id") == session_id]
                session_status = ("completed" if len(session_rows) == len(selected) and all(
                    row.get("status") in {"completed", "partial"} and row.get("has_answer") is True
                    for row in session_rows) else
                    "interrupted" if report["status"] == "interrupted" else
                    "technical_failure" if report["status"] == "technical_failure" else "completed_with_issues")
                report["last_session_status"] = session_status
                session_record.update(status=session_status, finished_at=report["finished_at"],
                    results=[{k: row.get(k) for k in ("case", "case_handle", "attempt_id", "status", "has_answer")}
                             for row in session_rows])
                save_json(session_dir / "session.json", session_record)
                save_json(run_dir / "summary.json", report)
                ended = sum(row.get("status") != "running" for row in report["cases"])
                answers = sum(bool(row.get("has_answer")) for row in report["cases"])
                progress.show("batch_end", status=report["status"], detail=(
                    f"已记录结束 {ended}/{len(planned_names)} 例；有正式答案 {answers} 例，"
                    f"无正式答案 {ended - answers} 例，未派发 {report['not_started']} 例"))
        return run_dir


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="新建批次、断点继续或重跑指定案例")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--resume", action="store_true", help="继续指定批次的失败及未派发案例")
    group.add_argument("--case", action="append", help="只重跑指定案例，可重复；接受原编号或目录名")
    group.add_argument("--rerun-status", help="只重跑当前汇总中指定状态的案例")
    parser.add_argument("--run-dir", help="已有批次结果目录")
    args = parser.parse_args(argv)
    if bool(args.run_dir) != bool(args.resume or args.case or args.rerun_status):
        parser.error("--run-dir 必须与 --resume、--case 或 --rerun-status 其中一种同时使用")
    return args


if __name__ == "__main__":
    args = parse_args()
    directory = (run_existing_batch(run_dir=args.run_dir, resume=args.resume, cases=args.case, status=args.rerun_status)
                 if args.run_dir else run_batch())
    summary = json.loads(filesystem_path(directory / "summary.json").read_text(encoding="utf-8"))
    status = summary.get("last_session_status") if args.run_dir else summary["status"]
    raise SystemExit(0 if status in {"completed", "no_cases"} else 1)
