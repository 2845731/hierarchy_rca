"""批次和案例进度的纯文本渲染。调用者负责计时、终端能力检测和加锁写出整行。"""

import math
import re
import unicodedata


# 只给阶段和少数状态上色；路径、耗时、计数使用终端默认文字颜色。
_STAGES = {
    "batch": ("整批", "◆", "96"),
    "case": ("案例", "●", "96"),
    "seed": ("Seed 候选调查", "🔎", "96"),
    "l1": ("L1 走链", "🧭", "94"),
    "l2": ("L2 审核", "🔬", "93"),
    "meta": ("Meta 裁决", "◎", "95"),
    "synthesis": ("答案综合", "✓", "92"),
    "output": ("程序组装答案", "✓", "92"),
}
_TOOLS = {
    "inspect_telemetry": "查数据目录或原始行",
    "query_telemetry": "查询原始遥测",
    "analyze_metrics": "比较指标",
    "analyze_traces": "检查请求",
    "analyze_logs": "查日志",
    "query_graph": "查调用邻居",
    "replay_evidence": "重放证据",
    "record_progress": "记录调查笔记",
}
_STATUS = {
    "ok": "成功",
    "reviewed": "审核完成",
    "completed": "流程完成",
    "submitted": "已提交",
    "rejected": "已拒绝",
    "withdrawn": "已撤回",
    "budget_exhausted": "本阶段可用预算不足",
    "insufficient_evidence": "证据不足",
    "investigation_stopped": "重复查询后调查未完成",
    "technical_failure": "技术失败",
    "technical_error": "工具执行失败",
    "invalid_query": "查询参数无效",
    "no_observations": "没有匹配记录",
    "stale_data": "数据版本已变",
    "reseed_requested": "已请求补候选",
    "failed": "失败",
    "error": "失败",
    "interrupted": "已中断",
    "running": "运行中",
    "replied": "已收到回复",
    "invalid_response": "回复格式无效",
    "provider_failure": "模型接口失败",
    "new_candidates": "已补充候选",
    "no_new_candidate": "未找到新候选",
    "not_dispatched": "未派发",
    "stopped": "已提前停止",
    "completed_with_issues": "已结束，部分案例未正常完成",
    "partial": "部分调查分支未完成",
    "recorded": "笔记已记录",
}
_ADVICE = {"keep": "保留", "query": "补查", "backtrack": "回退", "new_seed": "补充候选"}
_ACTION = {
    "extend": "已延伸一跳",
    "fork": "已分叉",
    "backtrack": "已执行回退",
    "review": "已收到本次审核返回",
    "submit": "已提交当前链",
    "reseed": "已请求补候选",
    "stop": "已停止本链",
    "choose_symptom": "已选目标症状",
}
_ESCAPES = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])")


def _text(value):
    """替换换行、回车、退格及 ANSI 控制串；原始事件不能插入另一行假进度。"""
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    raw = _ESCAPES.sub("", str(value))
    return " ".join("".join(" " if unicodedata.category(c).startswith("C") else c for c in raw).split())


def _cells(text):
    return sum(
        0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        for c in text
    )


def _clip(text, available, icons):
    if _cells(text) <= available:
        return text
    tail = "…" if icons else "..."
    left = available - _cells(tail)
    if left <= 0:
        return ""
    result = []
    for char in text:
        n = _cells(char)
        if n > left:
            break
        result.append(char)
        left -= n
    return "".join(result).rstrip() + tail


def _paint(text, code, enabled):
    return f"\x1b[{code}m{text}\x1b[0m" if enabled and text else text


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _duration(value):
    if not _number(value):
        return ""
    if value < 10:
        return f"{value:g} 秒" if float(value).is_integer() else f"{value:.1f} 秒"
    n = int(value)
    if n < 60:
        return f"{n} 秒"
    if n < 3600:
        return f"{n // 60}分{n % 60:02d}秒"
    return f"{n // 3600}时{n // 60 % 60:02d}分{n % 60:02d}秒"


def _nodes(values):
    if not isinstance(values, (tuple, list)):
        return []
    # 工作流输出服务名列表；也兼容带有 service 的节点记录，不展开其他元数据。
    return [s for node in values if (s := _text(node.get("service") if isinstance(node, dict) else node))]


def _budget(event):
    ledger = event.get("budget")
    ledger = ledger if isinstance(ledger, dict) else {}
    counts = []
    for name, label in (("model", "模型"), ("tool", "工具")):
        used = _count(event.get(f"{name}_calls", ledger.get(f"{name}_calls")))
        cap = _count(event.get(f"max_{name}_calls", ledger.get(f"max_{name}_calls")))
        if used is not None:
            counts.append(f"{label} {used}/{cap}" if cap is not None else f"{label} {used} 次")
    return "  ".join(counts)


def _advice(value):
    return _ADVICE.get(value, _text(value))


def _repeats(event):
    count = _count(event.get("consecutive_repeats"))
    limit = _count(event.get("max_consecutive_repeated_queries"))
    if count is None:
        return ""
    return f"连续重复 {count}/{limit} 次" if limit is not None else f"连续重复 {count} 次"


def _stage_for(kind):
    if kind.startswith("review_"):
        return "l2"
    if kind.startswith("chain_"):
        return "l1"
    if kind.startswith(("seeds_", "seed_")):
        return "seed"
    if kind.startswith("meta_"):
        return "meta"
    if kind.startswith("synthesis_"):
        return "synthesis"
    return "case" if "case" in kind else "batch"


def _tool_target(event):
    """只展示发布者提供的目标名、窗口和操作，不把原始 SQL 或模型消息当作任务说明。"""
    target = event.get("target")
    fields = target if isinstance(target, dict) else {}
    parts = [_text(target)] if isinstance(target, str) else []
    for key in ("component", "table", "metric"):
        if val := _text(fields.get(key)):
            parts.append(val)
    windows = {"normal": "正常窗口", "abnormal": "异常窗口", "both": "正常与异常窗口"}
    if window := _text(fields.get("window") or event.get("window")):
        parts.append(windows.get(window, window))
    if direction := _text(fields.get("direction")):
        parts.append(
            {"outgoing": "查被调用方", "incoming": "查调用方", "both": "查调用双方"}.get(direction, direction)
        )
    op = _text(fields.get("operation") or fields.get("action"))
    if op:
        parts.append(
            {
                "rank": "按服务排序",
                "compare": "对照两窗",
                "series": "时间序列",
                "operations": "按操作统计",
                "request": "展开请求",
                "pairs": "配对父子调用",
                "search": "检索原文",
                "inventory": "数据清单",
                "rows": "原始记录",
                "columns": "字段",
                "components": "组件清单",
            }.get(op, op)
        )
    return " / ".join(parts)


def render_event(event, *, color: bool, icons: bool) -> str:
    """返回无换行的一条完整事件。width 只缩短可选说明，不截断案例、链、路径或结果目录。"""
    kind = _text(event.get("kind"))
    stage = _text(event.get("stage") or event.get("role")).lower()
    if stage not in _STAGES:
        stage = _stage_for(kind)
    label, icon, color_code = _STAGES[stage]
    width = _count(event.get("width")) or 120
    sep = "  ·  " if icons and width >= 84 else " | "
    arrow = " → " if icons else " -> "
    clock = _text(event.get("time"))
    case = _text(event.get("case_handle") or event.get("case"))
    chain = _text(event.get("chain_id") or event.get("chain"))
    agent = _text(event.get("agent_id") or event.get("agent"))
    revision = _count(event.get("revision"))
    path = _nodes(event.get("after_nodes") or event.get("nodes") or event.get("path"))
    root = _text(event.get("seed_service") or event.get("root"))
    hop = _count(event.get("hop"))
    if hop is None and path:
        hop = len(path) - 1

    badge = "[" + (icon + " " if icons else "") + label + "]"
    head = " ".join(x for x in (clock, case, _paint(badge, color_code, color), chain) if x)
    if not chain and agent and agent.lower() not in {stage, label.lower()}:
        head += " " + agent
    if chain and revision is not None:
        head += f" v{revision}"

    facts = []
    action = _text(event.get("action"))
    status = _text(event.get("status"))
    status_label = _STATUS.get(status, status)
    detail = _text(event.get("detail") or event.get("reason"))
    elapsed = _duration(event.get("elapsed_seconds"))
    show_path = False
    prominent = color_code
    marker = ""
    if kind in ("batch_start", "batch_end"):
        title = "开始批次" if kind == "batch_start" else "批次结束"
        if kind == "batch_start" and detail and not detail.startswith("结果目录："):
            detail = "结果目录：" + detail
        if kind == "batch_end" and status:
            title += "：" + (_STATUS.get(status, status) if status != "completed" else "全部流程完成")
            if status != "completed":
                prominent = "93"
        if _count(event.get("total")) is not None:
            facts.append(f"计划 {event['total']} 例")
        if model := _text(event.get("model")):
            facts.append("模型 " + model)
    elif kind in ("batch_case_start", "case_start", "batch_case_end", "case_end"):
        # 父进程派发/回收、子进程进入/离开调查流程不是同一个事件。
        title = {
            "batch_case_start": "已派发案例",
            "case_start": "开始调查",
            "case_end": "调查流程结束",
            "batch_case_end": "案例结案",
        }[kind]
        if status_label and kind.endswith("end"):
            facts.append(status_label)
        if kind == "case_end":
            counts = event.get("counts")
            if isinstance(counts, dict):
                for key, name, unit in (
                    ("chains", "调查链", "条"),
                    ("submitted", "已提交", "条"),
                    ("l2_reviews", "L2 审核", "次"),
                ):
                    if _count(counts.get(key)) is not None:
                        facts.append(f"{name} {counts[key]} {unit}")
                if _count(counts.get("investigation_stopped")):
                    facts.append(f"未完成调查 {counts['investigation_stopped']} 项")
            for key, label in (("backtrack_advice", "L2 建议回退"), ("backtracks", "L1 实际回退")):
                if _count(event.get(key)) is not None:
                    facts.append(f"{label} {event[key]} 次")
        if elapsed:
            facts.append("用时 " + elapsed)
    elif kind in ("stage_start", "stage_end"):
        title = "开始" if kind == "stage_start" else "本阶段结束"
        if stage == "seed" and kind == "stage_start":
            title = "开始补充候选" if event.get("mode") == "supplement" else "开始初次调查"
            if ids := _nodes(event.get("request_ids")):
                facts.append("回应请求 " + "、".join(ids))
        if kind == "stage_end":
            if status_label:
                title = "本阶段完成" if status == "completed" else "本阶段结束：" + status_label
            if elapsed:
                facts.append("用时 " + elapsed)
    elif kind in ("synthesis_empty_root", "synthesis_recovery_request", "synthesis_recovery_result",
                  "synthesis_final_mode", "synthesis_final_retry"):
        title = {
            "synthesis_empty_root": "根因为空，等待综合复核",
            "synthesis_recovery_request": "综合请求 Meta 补查",
            "synthesis_recovery_result": "Meta 回流处理结束",
            "synthesis_final_mode": "进入最终综合",
            "synthesis_final_retry": "最终答案校验未通过，准备纠错",
        }[kind]
        if kind in ("synthesis_empty_root", "synthesis_final_retry"):
            prominent = "93"
        number = _count(event.get("recovery_no"))
        maximum = _count(event.get("max_recoveries"))
        if number is not None:
            facts.append(f"Meta 回流 {number}/{maximum}" if maximum is not None else f"Meta 回流 {number} 次")
        if (round_no := _count(event.get("round_no"))) is not None:
            facts.append(f"综合第 {round_no} 轮")
        attempt = _count(event.get("attempt"))
        max_attempts = _count(event.get("max_attempts"))
        if attempt is not None:
            facts.append(f"提交 {attempt}/{max_attempts}" if max_attempts is not None else f"提交第 {attempt} 次")
        if kind == "synthesis_recovery_result" and status_label:
            facts.append(status_label)
    elif kind in ("seed_request_queued", "seed_request_result"):
        title = (
            "补候选请求已排队"
            if kind == "seed_request_queued"
            else "补候选请求：" + (status_label or "已返回")
        )
        if request := _text(event.get("request_id")):
            facts.append(request)
        if added := _nodes(event.get("new_seed_ids")):
            facts.append("新候选 " + "、".join(added))
    elif kind == "seeds_ready":
        first = event.get("mode") == "initial" or (event.get("mode") is None and event.get("round") == 1)
        services = _nodes(event.get("services"))
        count = _count(event.get("count"))
        count = count if count is not None else len(services)
        title = "候选交接准备" if count else "候选调查结束：本轮没有候选"
        marker, prominent = ("✓", "92") if count else ("⚠", "33")
        facts.append(("初始候选" if first else "补充候选") + f" {count} 个")
        if services:
            facts.append("候选：" + "、".join(services))
        trigger = {
            "model_decision": "模型提交候选",
            "repeat_limit": "连续重复查询达到上限",
            "tool_budget_exhausted": "案例工具额度已用尽",
        }.get(event.get("handoff_trigger"))
        if trigger:
            facts.append("交接条件：" + trigger)
        if detail:
            detail = "模型说明：" + detail
    elif kind in ("chain_created", "chain_step", "chain_closed"):
        show_path = True
        if kind == "chain_created":
            parent = _text(event.get("parent_chain_id"))
            title = f"从 {parent} 分叉" if parent else "新建调查链"
            if _count(event.get("case_chain_no")) is not None:
                facts.append(f"本例第 {event['case_chain_no']} 条")
            if _count(event.get("l1_chain_no")) is not None:
                facts.append(f"本 L1 第 {event['l1_chain_no']} 条")
        elif kind == "chain_closed":
            title = "本链结束" + ("：" + status_label if status_label else "")
        else:
            title = _ACTION.get(action, _text(action) or "更新调查链")
            if _count(event.get("step_no")) is not None:
                facts.append(f"第 {event['step_no']} 次决定")
            if consumed := _text(event.get("review_id")):
                prior = _advice(event.get("recommendation"))
                facts.append(f"回应 {consumed}" + (f"（建议{prior}）" if prior else ""))
            if action == "fork" and event.get("child_chain_id"):
                child = arrow.join(_nodes(event.get("child_nodes")))
                facts.append("子链 " + _text(event["child_chain_id"]) + ("：" + child if child else ""))
            if action == "backtrack":
                before = _nodes(event.get("before_nodes"))
                if before and path:
                    old_hop, new_hop = len(before) - 1, len(path) - 1
                    # 只有确实退回原路径的前缀时，才列出从旧链尾依次退掉的节点。
                    if len(path) < len(before) and before[: len(path)] == path:
                        returned = arrow.join([*reversed(before[len(path) :]), path[-1]])
                    else:
                        returned = f"{before[-1]} 至 {path[-1]}"
                    title = f"已实际回退：{returned}（第 {old_hop} 跳至第 {new_hop} 跳）"
                prominent = "33"
    elif kind in ("review_sent", "review_returned"):
        show_path = True
        if rid := _text(event.get("review_id")):
            facts.append(rid)
        if kind == "review_returned" and elapsed:
            facts.append("审核用时 " + elapsed)
        if kind == "review_sent":
            title = "交给 L2 审核"
            if not detail:
                detail = _text(event.get("question"))
        elif status and status != "reviewed":
            title = "审核未完成：" + status_label
        else:
            recommendation = _advice(event.get("recommendation"))
            title = (
                "L2 建议" + recommendation if recommendation else "审核返回：" + (status_label or "未给建议")
            )
            if _number(event.get("score")):
                facts.append(f"评分 {event['score']:.2f}")
            prefix = _nodes(event.get("supported_prefix"))
            if prefix:
                facts.append(f"支持至 {prefix[-1]}（{len(prefix) - 1} 跳）")
            elif isinstance(event.get("supported_prefix"), list):
                facts.append("无获支持的前缀")
            for label, key in (("根因组件", "component_verdict"), ("故障类型", "fault_kind_verdict")):
                if verdict := _text(event.get(key)):
                    facts.append(label + "：" + {
                        "support": "有证据支持", "contradict": "存在反证", "unknown": "尚无法确认"
                    }.get(verdict, verdict))
    elif kind.startswith("meta_"):
        title = {
            "meta_followup": "请求补查",
            "meta_followup_result": "补查已返回",
            "meta_selection": "已选定结果",
            "meta_handoff": "保留已有结果，转入 Meta",
            "meta_selection_rejected": "选择内容校验未通过",
            "meta_selection_issue": "选择中的具体问题",
            "meta_selection_resolved": "上一轮问题已解决",
            "meta_selection_retry": "准备内容纠错",
            "meta_selection_valid": "内容纠错通过" if event.get("selection_failures") else "选择校验通过",
            "meta_selection_partial": "仅保留合格部分，转入程序组装",
            "meta_selection_stopped": "选择停止：没有合格部分",
        }.get(kind, "裁决进展")
        if kind in ("meta_selection_rejected", "meta_selection_issue", "meta_selection_partial"):
            marker, prominent = "⚠", "33"
        elif kind == "meta_selection_retry":
            marker = "↻"
            facts.append(f"内容纠错 {event['retry_no']}/{event['max_retries']}")
        elif kind in ("meta_selection_valid", "meta_selection_resolved"):
            marker = "✓"
        if (count := _count(event.get("issue_count"))) is not None:
            facts.append(f"待解决 {count} 项")
        if (count := _count(event.get("selection_failures"))) is not None:
            facts.append(f"选择校验失败累计 {count} 次")
        if kind == "meta_selection_partial":
            facts.append(f"剔除根因 {event['removed_root_count']} 项、传播边 {event['removed_edge_count']} 条")
            facts.append("结果状态：partial（部分结果）")
        if kind in ("meta_selection_issue", "meta_selection_resolved"):
            issue = event["issue"]
            item = issue.get("item")
            if isinstance(item, dict):
                if "from" in item and "to" in item:
                    facts.append("问题边 " + _text(item["from"]) + " → " + _text(item["to"]))
                elif "service" in item:
                    facts.append("根因 " + _text(item["service"]) + " / " + _text(item.get("fault_kind")))
            if issue.get("chain_id"):
                facts.append("来源链 " + _text(issue["chain_id"]))
            for source in issue.get("sources", []):
                missing = "、".join(_text(e["from"]) + " → " + _text(e["to"]) for e in source["missing_prefix"])
                label = " 原缺少 " if kind == "meta_selection_resolved" else " 缺少 "
                facts.append("来源链 " + _text(source["chain_id"]) + label + missing)
            for ref in issue.get("invalid_refs", []):
                providers = "、".join(issue["evidence_sources"][ref]) or "无支持该判断的来源"
                label = "原不允许引用 " if kind == "meta_selection_resolved" else "不允许引用 "
                facts.append(label + _text(ref) + "；绑定来源 " + _text(providers))
            if "allowed_refs" in issue:
                facts.append("本项可用证据 " + "、".join(_nodes(issue["allowed_refs"])))
        if kind == "meta_handoff":
            marker = "→"
            for key, field, unit in (
                ("submitted_count", "已提交链", "条"),
                ("stopped_count", "未完成调查", "项"),
            ):
                if _count(event.get(key)) is not None:
                    facts.append(f"{field} {event[key]} {unit}")
            if event.get("finalize_only"):
                facts.append("只综合已有证据，不再派发补查")
        if target := _text(event.get("target_type")):
            facts.append("交候选调查" if target == "seed" else "交 L1 走链" if target == "l1" else target)
        for key, field in (("root_count", "根因"), ("edge_count", "传播边")):
            if _count(event.get(key)) is not None:
                facts.append(f"{field} {event[key]}")
        if kind == "meta_selection":
            if isinstance(event.get("selected_chain_ids"), list):
                ids = _nodes(event["selected_chain_ids"])
                facts.append("采纳链 " + ("、".join(ids) or "0 条"))
            if isinstance(event.get("root_causes"), list):
                roots = _nodes(event["root_causes"])
                facts.append("根因 " + ("、".join(roots) or "0 项"))
            if isinstance(event.get("propagation"), list):
                facts.append(f"传播边 {len(event['propagation'])} 条")
    elif kind == "tool_reused":
        tool = _text(event.get("tool"))
        title = "复用已有结果：" + _TOOLS.get(tool, tool)
        marker = "↻"
        if target := _tool_target(event):
            facts.append(target)
        facts.append(f"查询 #{event['query_no']}，复用 {event['reuse_count']} 次")
        if repeated := _repeats(event):
            facts.append(repeated)
        facts.append("未重新查询，不增加工具次数")
        if status_label and status != "ok":
            facts.append("原结果：" + status_label)
    elif kind == "query_repeated":
        title, marker = "再次重放已读证据", "↻"
        if repeated := _repeats(event):
            facts.append(repeated)
        facts.append("已执行 SQL 并计入工具次数，但没有新增观测")
    elif kind == "query_decision_required":
        title, prominent, marker = "停止查询，下一轮只作决定", "93", "⚠"
        if repeated := _repeats(event):
            facts.append(repeated)
        facts.append("根据已有证据提交本阶段结果")
    elif kind == "investigation_stopped":
        title, prominent, marker = "当前调查未完成，保留其他分支结果", "93", "⚠"
        if status_label:
            facts.append(status_label)
        if stop_id := _text(event.get("stop_id")):
            facts.append(stop_id)
        if ids := _nodes(event.get("request_ids")):
            facts.append("补查请求 " + "、".join(ids))
        show_path = bool(chain)
    elif kind in ("model_start", "model_end", "tool_start", "tool_end"):
        tool = _text(event.get("tool"))
        tool_label = _TOOLS.get(tool, tool) or "工具"
        if kind.startswith("model"):
            title = "已发起模型调用" if kind == "model_start" else "模型调用结束"
            retry_no = _count(event.get("json_retry_no"))
            retry_label = f"JSON 纠错重试 {retry_no}/{event['max_json_retries']}" if retry_no else ""
            if status == "invalid_response":
                title, prominent, marker = f"{retry_label} 未返回有效 JSON".strip(), "93", "⚠"
                status_label = ""
            elif retry_label and kind == "model_start":
                title, prominent, marker = retry_label + "：发起模型调用", "92", "↻"
            elif retry_label and status == "replied":
                title, prominent, marker = retry_label + " 成功", "92", "✓"
                status_label = "已返回有效 JSON"
            elif retry_label:
                status_label = retry_label + " 未完成：" + status_label
            if model := _text(event.get("model")):
                facts.append(model)
        else:
            title = ("开始" if kind == "tool_start" else "已返回：") + tool_label
            if target := _tool_target(event):
                facts.append(target)
        call = _count(event.get("call_no"))
        if call is not None:
            title += f" #{call}"
        if kind.endswith("end"):
            if status_label and status not in ("ok", "completed"):
                facts.append(status_label)
            for key, unit in (("row_count", "行"), ("edge_count", "条边"), ("evidence_count", "个查询引用")):
                if _count(event.get(key)) is not None:
                    facts.append(f"{event[key]} {unit}")
            if elapsed:
                facts.append("用时 " + elapsed)
    elif kind == "heartbeat":
        # 等待只是观测状态，不等于超时、触发重试，更不能推进案例百分比。
        title = "仍在运行"
        wait = _duration(event.get("wait_seconds"))
        if wait:
            facts.append("已等待 " + wait)
    elif kind == "action_rejected":
        title, prominent = "动作未获接受", "33"
    elif kind == "json_retry_stopped":
        title, prominent = "JSON 纠错重试停止", "33"
        if status == "technical_failure":
            title, marker = "JSON 纠错重试失败：已达上限", "✗"
            if (call := _count(event.get("call_no"))) is not None:
                title += f" #{call}"
            if (cap := _count(event.get("max_json_retries"))) is not None:
                facts.append(f"已重试 {cap}/{cap} 次")
    else:
        title = kind or "进度"

    failed = status in (
        "technical_failure",
        "technical_error",
        "provider_failure",
        "error",
        "failed",
        "interrupted",
    )
    if failed:
        prominent = "91"
        if error_type := _text(event.get("error_type")):
            facts.append(error_type)
    if show_path:
        if path:
            facts.append((f"{hop} 跳：" if hop is not None else "") + arrow.join(path))
        elif root:
            facts.append("起点 " + root)
    # 只在额度刚被使用、案例边界或耗尽时报告；交接、正常返回不挤占审核原因的位置。
    if (
        kind in ("model_start", "tool_start", "case_start", "case_end", "batch_case_start", "batch_case_end")
        or status == "budget_exhausted"
    ):
        if budget := _budget(event):
            facts.append(budget)
    if icons and marker:
        title = f"{marker} {title}"
    main = head + "  " + _paint(title, prominent, color)
    if facts:
        main += sep + sep.join(facts)
    if detail:
        # 结果地址和长等待提示必须能读全。其余说明一行显示；长链保留首尾及中间所有节点。
        preserve = (
            failed
            or kind in (
                "batch_start", "batch_end", "batch_case_start", "batch_case_end", "heartbeat",
                "query_decision_required", "investigation_stopped", "meta_handoff", "seeds_ready",
                "meta_selection_issue", "meta_selection_partial", "meta_selection_stopped",
                "meta_selection_resolved",
            )
            or bool(re.search(r"[A-Za-z]:[\\/]", detail))
        )
        left = max(14, width - _cells(_ESCAPES.sub("", main)) - _cells(sep))
        main += sep + (detail if preserve else _clip(detail, left, icons))
    return main


def wrap_terminal(line, width):
    """交互终端按完整字段换行并缩进；不拆组件名或省略路径，文件仍是一条事件一行。"""
    sep = "  ·  " if "  ·  " in line else " | "
    parts = line.split(sep)
    rows = [parts[0]]
    for part in parts[1:]:
        same_row = rows[-1] + sep + part
        if _cells(_ESCAPES.sub("", same_row)) < width:
            rows[-1] = same_row
        else:
            rows.append("    " + part)
    return "\n".join(rows)


def render_batch(done, total, running, queued, elapsed, *, color: bool, icons: bool, width=88) -> str:
    """按真正结束的案例数填进度条，不把模型调用、工具调用或正在执行的案例折成百分比。"""
    n, target = _count(done), _count(total)
    if n is None or target is None:
        raise ValueError("done、total 需要非负整数")
    if n > target:
        raise ValueError("已结束案例数不能超过计划案例数")
    tight = width < 68
    length = 6 if tight else 14
    fill = length * n // target if target else 0
    glyph, empty = ("█", "░") if icons else ("#", "-")
    bar = "[" + _paint(glyph * fill, "32", color) + empty * (length - fill) + "]"
    prefix = "批次" if tight else "批次进度"
    text = f"{_paint(prefix, '36', color)} {bar} 已结束 {n}/{target}"
    if target:
        # 未真正全部结束不打印 100%，避免如 454/455 四舍五入后误显完成。
        text += f" ({100 * n // target}%)"
    for label, value in (("运行", running), ("未派发", queued)):
        if _count(value) is not None:
            text += f"  {label} {value}"
    if duration := _duration(elapsed):
        text += f"  用时 {duration}"
    return text
