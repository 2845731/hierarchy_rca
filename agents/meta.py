"""全局调查仲裁；旧选择检查保留给历史调用入口。"""

import copy
import json
import math

import state
from filesystem import filesystem_path
from state import Limit, refs_in
from .loop import Invalid, StageFailure, require, abnormal_refs


def submitted_view(c):
    """仅显式提交旧版时选择该快照；调查中的当前版本始终不被覆盖。"""
    checkpoint = c.get("submitted_checkpoint") if c["status"] == "submitted" else None
    if not checkpoint:
        return c
    return {**c, **checkpoint["chain"], "status": "submitted", "last_review": checkpoint["review"]}


def meta_chains(case):
    return [
        {
            **state.view(submitted_view(c)),
            "reason": c["reason"],
            "last_review": submitted_view(c)["last_review"],
            "last_decision": c["decisions"][-1] if c["decisions"] else None,
            **({"unreviewed_proposals": c["unreviewed_proposals"]} if c.get("unreviewed_proposals") else {}),
            **({"unreviewed_revision": c["revision"]} if c.get("submitted_checkpoint") and c["status"] == "submitted" else {}),
        }
        for c in case.ordered()
    ]


def investigation_questions(case):
    """索引已有调查线索；问题本身不代表冲突未解决或故障已经成立。"""
    questions = []
    for original in case.ordered():
        c = submitted_view(original)
        cid, revision = c["chain_id"], c["revision"]
        root = c.get("root") or {}
        diagnosis = c.get("fault_diagnosis") or {}
        candidates = diagnosis.get("candidates") or []
        if len(candidates) > 1 or (root and root.get("fault_kind") is None):
            questions.append({
                "type": "fault_diagnosis",
                "chain_id": cid, "revision": revision,
                "service": root.get("service") or (c.get("nodes") or [None])[0],
                "candidates": copy.deepcopy(candidates),
                "missing_evidence": diagnosis.get("missing_evidence"),
                "next_check": diagnosis.get("next_check"),
                "source": "当前链版本的 fault_diagnosis；候选及缺口需结合后续证据核对",
            })
        # 优先寻找与当前主张不同的最近有效审核；确保版本与审核严格对应。
        for review in reversed(original.get("reviews", [])):
            if (review.get("status") != "reviewed" or type(review.get("revision")) is not int
                    or review["revision"] >= revision):
                continue
            try:
                old = _version(case, cid, review.get("revision"))
            except (Invalid, OSError, ValueError):
                continue
            old_root = old.get("root") or {}
            if (old_root.get("service") == root.get("service") and root.get("service")
                    and old_root.get("fault_kind") != root.get("fault_kind")):
                questions.append({
                    "type": "changed_fault_kind", "chain_id": cid, "revision": revision,
                    "service": root["service"], "current_root": copy.deepcopy(c.get("root")),
                    "current_review": copy.deepcopy(c.get("last_review")),
                    "previous_revision": review["revision"], "previous_root": copy.deepcopy(old.get("root")),
                    "previous_review_id": review.get("review_id"),
                    "previous_fault_kind_verdict": copy.deepcopy(review.get("fault_kind_verdict")),
                    "source": "历史版本与其审核；变化不自动表示存在未解决冲突",
                })
                break
    for request in case.requests:
        response = request.get("response") or {}
        if response.get("status") in {"pending", "failed", "technical_failure", "budget_exhausted", "not_dispatched"}:
            questions.append({
                "type": "supplement_request", "request_id": request.get("request_id"),
                "chain_id": request.get("chain_id"), "suggested_service": request.get("suggested_service"),
                "reason": request.get("reason"), "evidence_refs": request.get("evidence_refs", []),
                "response": copy.deepcopy(response),
                "source": "已有补种请求及其实际返回",
            })
    return questions


def selection_options(case):
    """从已提交的本版审核整理来源；只是索引，不替模型选择根因或边。"""
    options = {"root_causes": {}, "propagation": {}}
    for original in case.ordered():
        c = submitted_view(original)
        r = c["last_review"]
        if c["status"] != "submitted" or not r or r["status"] != "reviewed" or r["revision"] != c["revision"]:
            continue
        base = {"chain_id": c["chain_id"], "revision": c["revision"], "review_id": r["review_id"]}
        root = c["root"]
        if (root and root.get("fault_kind") is not None
                and (r.get("component_verdict") or {}).get("verdict") == "support"
                and (r.get("fault_kind_verdict") or {}).get("verdict") == "support"):
            key = (root["service"], root["fault_kind"])
            options["root_causes"].setdefault(key, []).append({
                **base, "evidence_refs": sorted(refs_in(root) | refs_in(r["component_verdict"])
                                                  | refs_in(r["fault_kind_verdict"])),
            })
        for i, (edge, verdict) in enumerate(zip(c["edges"], r["edge_verdicts"])):
            if verdict["verdict"] != "support":
                continue
            key = (edge["from"], edge["to"])
            options["propagation"].setdefault(key, []).append({
                **base,
                "evidence_refs": sorted(set(edge["evidence_refs"]) | refs_in(verdict)),
                "required_prefix": [{"from": e["from"], "to": e["to"]} for e in c["edges"][:i]],
            })
    return options


def selection_catalog(options):
    """供提示词直接读取的逐项清单；内部元组键不泄漏到 JSON。"""
    return {
        group: [
            {**dict(zip(keys, key)), "sources": sources}
            for key, sources in options[group].items()
        ]
        for group, keys in (("root_causes", ("service", "fault_kind")), ("propagation", ("from", "to")))
    }


def check_selection(case, action, options=None):
    """一次收集可确定的问题。正常选择与删减后的结果始终使用同一套检查。"""
    options = selection_options(case) if options is None else options
    issues = []
    selected = {"selected_chain_ids": [], "root_causes": [], "propagation": [], "reason": ""}
    report = {"valid": False, "issues": issues, "selection": selected}

    def problem(code, message, **details):
        issues.append({"code": code, "message": message, **details})

    if not isinstance(action, dict):
        problem("invalid_action", "选择必须是 JSON 对象")
        return report
    why = action.get("reason")
    if not isinstance(why, str) or not why.strip():
        problem("invalid_reason", "必须说明选择依据及未完成问题")
    else:
        selected["reason"] = why.strip()
    ids = action.get("selected_chain_ids")
    if not isinstance(ids, list) or not all(isinstance(cid, str) for cid in ids):
        problem("invalid_chain_ids", "selected_chain_ids 必须是链编号列表")
        return report
    selected_ids = list(dict.fromkeys(ids))
    if len(selected_ids) != len(ids):
        problem("duplicate_chain_ids", "来源链编号重复")
    usable_ids = []
    for cid in selected_ids:
        original = case.chains.get(cid)
        c = submitted_view(original) if original else None
        r = c["last_review"] if c else None
        if not c:
            problem("unknown_chain", "来源链不存在", chain_id=cid)
        elif c["status"] != "submitted" or not r or r["status"] != "reviewed" or r["revision"] != c["revision"]:
            problem("unreviewed_chain", "来源链未提交或没有当前版本的有效审核", chain_id=cid)
        else:
            usable_ids.append(cid)

    contributed = set()
    edge_sources = {}
    for group, keys in (("root_causes", ("service", "fault_kind")), ("propagation", ("from", "to"))):
        items = action.get(group)
        if not isinstance(items, list):
            problem("invalid_items", f"{group} 必须是列表", group=group)
            continue
        used = set()
        for i, entry in enumerate(items):
            target = {"group": group, "index": i, "item": entry}
            if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in keys):
                problem("invalid_item", "条目缺少必要的文字字段", **target)
                continue
            key = tuple(entry[k] for k in keys)
            if key in used:
                problem("duplicate_item", "同一个判断不能重复列出", **target)
                continue
            used.add(key)
            sources = [s for s in options[group].get(key, []) if s["chain_id"] in usable_ids]
            if not sources:
                problem("unsupported_item", "当前所选链中没有本版 L2 明确支持的同方向、同类型判断",
                        available_sources=options[group].get(key, []), **target)
                continue
            allowed = set().union(*(set(s["evidence_refs"]) for s in sources))
            refs = entry.get("evidence_refs")
            if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) for ref in refs):
                problem("invalid_refs", "每项需填写非空证据编号列表", allowed_refs=sorted(allowed), **target)
                continue
            bad_refs = sorted(set(refs) - allowed)
            if bad_refs:
                providers = {
                    ref: [s["chain_id"] for s in options[group].get(key, []) if ref in s["evidence_refs"]]
                    for ref in bad_refs
                }
                problem("evidence_scope", "引用不属于当前所选来源链中该判断的允许证据，不表示证据从未出现",
                        invalid_refs=bad_refs, evidence_sources=providers, allowed_refs=sorted(allowed), **target)
                continue
            try:
                refs = case.rt.refs(refs, allowed)
                require(abnormal_refs(case, refs), "正常调用或基线不能单独证明本案异常")
                if group == "propagation":
                    require(any(case.rt.tele.evidence(ref)["kind"] in {"metric", "trace"} for ref in refs),
                            "传播边至少包含指标或调用证据，不能只有日志")
            except Invalid as exc:
                problem("invalid_evidence", str(exc), **target)
                continue
            selected[group].append({**dict(zip(keys, key)), "evidence_refs": refs})
            contributed.update(s["chain_id"] for s in sources if set(s["evidence_refs"]) & set(refs))
            if group == "propagation":
                edge_sources[key] = (i, entry, sources)

    # 用已经通过逐项检查的边检查连续性；不让一条证据非法的边替后段搭桥。
    edge_keys = set(edge_sources)
    for key, (i, entry, sources) in edge_sources.items():
        missing = [
            {
                "chain_id": s["chain_id"], "revision": s["revision"],
                "missing_prefix": [
                    e for e in s["required_prefix"] if (e["from"], e["to"]) not in edge_keys
                ],
            }
            for s in sources
        ]
        if all(s["missing_prefix"] for s in missing):
            problem("missing_prefix", "缺少来源链前段；反向边或总图连通不能替代原链前段",
                    group="propagation", index=i, item=entry, sources=missing)
    for cid in usable_ids:
        if cid not in contributed:
            problem("unused_chain", "来源链未贡献本次有效根因或边", chain_id=cid)
    selected["selected_chain_ids"] = [cid for cid in usable_ids if cid in contributed]
    report["valid"] = not issues
    return report


def select(case, action):
    """保留原来的调用入口；检查失败时不保存任何正式选择。"""
    result = check_selection(case, action)
    require(result["valid"], state.dump(result["issues"]))
    return result["selection"]


def keep_valid_selection(case, action):
    """只删最后提案中的非法条目和无贡献来源；不换证据、不造边、不改变类型。"""
    candidate = copy.deepcopy(action)
    removed = []
    fatal = {"invalid_action", "invalid_reason", "invalid_chain_ids", "invalid_items"}
    while True:
        result = check_selection(case, candidate)
        if result["valid"]:
            return result["selection"], removed
        if any(issue["code"] in fatal for issue in result["issues"]):
            raise StageFailure("最后选择缺少可解释的完整结构，不能自动保留：" + state.dump(result["issues"]))
        removed.extend(result["issues"])
        bad = {
            group: {x["index"] for x in result["issues"] if x.get("group") == group and "index" in x}
            for group in ("root_causes", "propagation")
        }
        candidate = {
            **candidate,
            "selected_chain_ids": result["selection"]["selected_chain_ids"],
            **{
                group: [entry for i, entry in enumerate(candidate[group]) if i not in bad[group]]
                for group in bad
            },
        }
        # 每轮至少删一个条目/来源。删前段后，再用完整规则检查后段，不提前宣布通过。
        if not candidate["root_causes"] and not candidate["propagation"]:
            candidate["selected_chain_ids"] = []


def _selection_event(case, kind, **fields):
    case.rt.records.event(case.meta.id, kind, fields)
    state.report(case.rt, kind, stage="meta", agent_id=case.meta.id, **fields)


def _issue_key(issue):
    """用判断对象而非列表位置比较两轮问题，避免条目重排被误报为修复。"""
    item = issue.get("item")
    item = item if isinstance(item, dict) else {}
    return (
        issue["code"], issue.get("group"), issue.get("chain_id"),
        *(item.get(k) if isinstance(item.get(k), str) else None
          for k in ("service", "fault_kind", "from", "to")),
    )


def _save_selection(case, selection, report):
    case.meta_selection_report = report
    case.rt.records.save("meta/selection_validation.json", report)
    case.rt.records.save("meta/selection.json", selection)
    case.meta.accepted()
    return selection


def _finish_partial(case, proposal, failures, stop_reason):
    """只处理已知选择失败或仲裁额度耗尽；未知异常、接口失败不会进入此分支。"""
    selection, removed = keep_valid_selection(case, proposal)
    report = {
        "selection_failures": failures, "used_partial_selection": True,
        "stop_reason": stop_reason, "last_proposal": proposal, "removed": removed,
        "retained": selection,
    }
    case.meta_selection_report = report
    case.rt.records.save("meta/selection_validation.json", report)
    if not selection["root_causes"] and not selection["propagation"]:
        _selection_event(case, "meta_selection_stopped", status="technical_failure", reason="最后提案没有可保留的合格条目")
        raise StageFailure("Meta 选择未完成，最后提案没有可保留的合格条目；" + stop_reason)
    # 不照抄声称已验证所有分支的旧理由。完整模型原文在 last_proposal 中保留。
    selection["reason"] = "Meta 纠错未完成；程序仅保留最后提案中通过原有审核、证据及连续性检查的条目。"
    selection["reason"] += " 剔除原因：" + "；".join(dict.fromkeys(x["message"] for x in removed))
    report["retained"] = selection
    _selection_event(
        case, "meta_selection_partial", status="partial",
        root_count=len(selection["root_causes"]), edge_count=len(selection["propagation"]),
        removed_root_count=len(proposal["root_causes"]) - len(selection["root_causes"]),
        removed_edge_count=len(proposal["propagation"]) - len(selection["propagation"]),
        reason=stop_reason + "；只保留合格部分，转入程序组装",
    )
    return _save_selection(case, selection, report)


def _version(case, chain_id, revision, cache=None):
    """核对结案引用来自本案例实际保存的链版本。"""
    require(isinstance(chain_id, str) and chain_id in case.chains, "结案引用了不存在的链")
    require(type(revision) is int and revision >= 0, "链版本必须是非负整数")
    path = filesystem_path(case.out / "stages" / "l1" / chain_id / f"v{revision:03d}.json")
    require(path.is_file(), "结案引用的链版本不存在")
    text = path.read_text(encoding="utf-8")
    if cache is None:
        return json.loads(text)
    if text not in cache:
        cache[text] = json.loads(text)
    try:
        return copy.deepcopy(cache[text])
    except RecursionError:
        return json.loads(text)


def _explains_symptom(case, chain, symptom):
    """Validate structural coverage; a different interface cannot borrow root-only support."""
    target = chain.get("target_symptom") or {}
    service = symptom["service"]
    same_observation = (target.get("service") == service
                        and bool(set(target.get("evidence_refs", [])) & set(symptom["evidence_refs"])))
    for key in ("operation", "anomaly_kind", "scope"):
        if symptom.get(key) and target.get(key) != symptom[key]:
            same_observation = False
    for review in case.chains[chain["chain_id"]].get("reviews", []):
        if review.get("revision") != chain["revision"] or review.get("status") != "reviewed":
            continue
        component = review.get("component_verdict") or {}
        if component.get("verdict") != "support":
            continue
        if service == chain["nodes"][0]:
            if set(component.get("evidence_refs", [])) & set(symptom["evidence_refs"]):
                return "root_local"
        elif same_observation and service in chain["nodes"]:
            pos = chain["nodes"].index(service)
            supported = {(e["from"], e["to"]) for e in review.get("edge_verdicts", [])
                         if e.get("verdict") == "support" and not e.get("direction_mismatch")}
            if all((e["from"], e["to"]) in supported for e in chain["edges"][:pos]) and pos <= len(chain["edges"]):
                return "reviewed_path"
    return None


def check_conclusion(case, action, priority_gaps=None, coverage=None, symptom_page=None, displayed_tasks=None):
    cache = {}
    require(isinstance(action, dict) and action.get("action") == "conclude", "需要 conclude 决定")
    require(isinstance(action.get("termination_reason"), str) and action["termination_reason"].strip(),
            "需要说明结束调查的原因")
    for key in ("branch_assessments", "open_issues"):
        require(isinstance(action.get(key), list) and all(isinstance(x, dict) for x in action[key]),
                f"{key} 必须是对象列表")
    for item in action["branch_assessments"]:
        _version(case, item.get("chain_id"), item.get("revision"), cache=cache)
        require(isinstance(item.get("assessment"), str) and item["assessment"].strip(), "分支评估需要说明")
    scores, validation_issues = [], []
    optional_scores = action.get("confidence_scores", [])
    if not isinstance(optional_scores, list):
        validation_issues.append({"field": "confidence_scores", "error": "已忽略非列表辅助评分"})
        optional_scores = []
    for i, item in enumerate(optional_scores):
        try:
            require(isinstance(item, dict), "评分须为对象")
            chain = _version(case, item.get("chain_id"), item.get("revision"), cache=cache)
            root = chain.get("root")
            valid = {f"edge:{e['from']}|{e['to']}" for e in chain.get("edges", [])}
            if root and root.get("fault_kind") is not None:
                valid.add(f"root:{root['service']}|{root['fault_kind']}")
            require(isinstance(item.get("conclusion_ref"), str) and item["conclusion_ref"] in valid,
                    "辅助评分引用不属于该版本")
            value = item.get("conclusion_confidence")
            require(type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1,
                    "置信度须为 0 至 1 的有限数")
            require(isinstance(item.get("reason"), str) and item["reason"].strip(), "评分缺少理由")
            scores.append(copy.deepcopy(item))
        except Invalid as exc:
            validation_issues.append({"field": f"confidence_scores[{i}]", "error": str(exc)})
    for item in action["open_issues"]:
        require(all(isinstance(item.get(k), str) and item[k].strip() for k in ("issue", "reason")),
                "未解决问题需要具体问题和原因")
    # 兼容旧版 Meta 输出；模型遗漏的条目按未处理留痕，不能解释成已核查。
    priority_gaps = priority_gaps or []
    assessments = action.get("propagation_gap_assessments", [])
    require(isinstance(assessments, list) and all(isinstance(x, dict) for x in assessments),
            "propagation_gap_assessments 必须是对象列表")
    expected = {(g["seed_id"], g["task_id"]) for g in priority_gaps}
    expected.update(displayed_tasks or {})
    provided = set()
    for item in assessments:
        require(isinstance(item.get("seed_id"), str) and isinstance(item.get("task_id"), str),
                "传播缺口须填写真实 seed_id 和 task_id")
        key = (item.get("seed_id"), item.get("task_id"))
        require(key in expected and key not in provided,
                f"传播缺口编号须来自本轮已展示任务且不可重复；合法编号：{sorted(expected)}")
        if displayed_tasks and key in displayed_tasks:
            task_source = displayed_tasks[key]
            _version(case, task_source[0], task_source[1], cache=cache)
        require(item.get("disposition") in {"already_checked", "contradicted", "insufficient_evidence",
                                             "budget_limited", "deferred"},
                "传播缺口处置须为 already_checked、contradicted、insufficient_evidence、budget_limited 或 deferred")
        require(isinstance(item.get("reason"), str) and item["reason"].strip(), "传播缺口需要具体处置理由")
        provided.add(key)
    assessments = copy.deepcopy(assessments)
    for gap in priority_gaps:
        if (gap["seed_id"], gap["task_id"]) not in provided:
            assessments.append({"seed_id": gap["seed_id"], "task_id": gap["task_id"],
                                "disposition": "deferred",
                                "reason": "结案记录未逐项给出处置；该缺口仍待核查。"})
    symptom_page = symptom_page or {"items": [], "total": 0, "truncated": False}
    symptom_items = action.get("symptom_assessments", [])
    require(isinstance(symptom_items, list) and all(isinstance(x, dict) for x in symptom_items),
            "symptom_assessments 必须是对象列表")
    expected_symptoms = {item["symptom_index"] for item in symptom_page["items"]}
    all_symptoms = getattr(case, "symptoms", [])
    assessed = set()
    for item in symptom_items:
        index = item.get("symptom_index")
        require(type(index) is int and 0 <= index < len(all_symptoms) and index not in assessed,
                "symptom_index 必须来自整案症状列表且不可重复")
        require(item.get("status") in {"explained", "unresolved", "contradicted", "deferred"},
                "症状状态须为 explained、unresolved、contradicted 或 deferred")
        require(isinstance(item.get("reason"), str) and item["reason"].strip(), "症状评估需要具体理由")
        if item["status"] == "explained":
            chain = _version(case, item.get("chain_id"), item.get("revision"), cache=cache)
            explanation = _explains_symptom(case, chain, all_symptoms[index])
            if explanation is None:
                item = dict(item, status="unresolved", reason=item["reason"] +
                            "；程序未确认同一观测范围的根因支持或连续支持路径，保留未决。")
                validation_issues.append({"field": f"symptom_assessments[{index}]",
                                          "error": "解释范围或审核支持不足"})
            else:
                item = dict(item, explanation_kind=explanation)
            # Change the saved assessment, without rewriting the model's original event.
            symptom_items = [item if x.get("symptom_index") == index else x for x in symptom_items]
        assessed.add(index)
    symptom_items = copy.deepcopy(symptom_items)
    for item in symptom_page["items"]:
        if item["symptom_index"] not in assessed:
            symptom_items.append({"symptom_index": item["symptom_index"], "status": "unresolved",
                                  "reason": "结案记录未逐项评估该症状；是否已解释仍待核查。"})
    for index in range(len(all_symptoms)):
        if index not in expected_symptoms and index not in assessed:
            symptom_items.append({"symptom_index": index, "status": "unresolved",
                                  "reason": "此症状不在结案时展示页；是否已解释仍待核查。"})
    conclusion = {k: copy.deepcopy(action[k]) for k in
                  ("branch_assessments", "open_issues", "termination_reason")}
    conclusion["confidence_scores"] = scores
    conclusion["validation_issues"] = validation_issues
    conclusion["propagation_gap_assessments"] = assessments
    conclusion["priority_propagation_gaps"] = copy.deepcopy(priority_gaps)
    conclusion["family_coverage"] = copy.deepcopy(coverage or [])
    conclusion["symptom_coverage"] = copy.deepcopy(symptom_page)
    conclusion["symptom_coverage"]["all_items"] = [
        {**copy.deepcopy(symptom), "symptom_index": index}
        for index, symptom in enumerate(all_symptoms)
    ]
    conclusion["symptom_assessments"] = symptom_items
    return conclusion


def family_coverage(case, page_offsets=None):
    """汇总各候选起点已发现的传播待办；未知或受限不等于传播被否定。"""
    result = []
    page_offsets = page_offsets or {}
    limit = max(1, getattr(case.cfg, "branch_frontier_summary_limit", 12))
    for seed_id, ids in case.families.items():
        frontier = case.branch_frontiers.get(seed_id)
        if frontier is None:
            continue
        family = [case.chains[cid] for cid in ids if cid in case.chains]
        # 家族汇总先取全量以保证计数准确，再限制送入模型的条目数。
        view = frontier.family_summary(family, case.symptoms, case.meta.seen, limit=None)
        items = view.get("items", [])
        counts = {}
        for task in items:
            status = task.get("status", "pending")
            counts[status] = counts.get(status, 0) + 1
        verdict_counts = {"support": 0, "contradict": 0, "unknown": 0}
        for task in items:
            verdict = task.get("review_verdict")
            if verdict in verdict_counts:
                verdict_counts[verdict] += 1
        open_items = [task for task in items if task.get("status") in
                      {"pending", "in_progress", "unresolved", "deferred"}]
        compact = []
        offset = page_offsets.get(seed_id, 0)
        # 调查状态变化可能缩短列表；将游标收回到有效页。
        if offset >= len(open_items):
            offset = max(0, ((len(open_items) - 1) // limit) * limit) if open_items else 0
        for task in open_items[offset:offset + limit]:
            compact.append({k: copy.deepcopy(task[k]) for k in
                            ("task_id", "at", "neighbor", "source_chain_id", "source_revision", "source_status",
                             "status", "review_verdict", "reason", "evidence_refs", "symptom_clues", "observations")
                            if k in task})
        decided = [task for task in items if task.get("review_verdict") in {"support", "contradict"}]
        # 展示少量已裁决样例；仅统计 L2 对应版本的结论，并提示样例是否省略。
        decided_samples = [{k: copy.deepcopy(task[k]) for k in
                            ("task_id", "at", "neighbor", "source_chain_id", "source_revision",
                             "status", "review_verdict") if k in task}
                           for task in decided[:min(limit, 3)]]
        result.append({
            "seed_id": seed_id,
            "frontier_revision": view.get("frontier_revision", 0),
            "chains_used": len(ids),
            "chains_limit": case.cfg.chains_per_l1,
            "known_tasks": view.get("total", len(items)),
            "status_counts": counts,
            "review_verdict_counts": verdict_counts,
            "decided_samples": decided_samples,
            "decided_samples_truncated": len(decided_samples) < len(decided),
            "open_tasks": compact,
            "priority_tasks": [copy.deepcopy(task) for task in open_items
                               if task.get("status") in {"pending", "unresolved", "deferred"}
                               and any(clue.get("evidence_refs") for clue in task.get("symptom_clues", []))][:3],
            "open_total": len(open_items),
            "truncated": len(compact) < len(open_items),
            "page_offset": offset,
            "next_offset": offset + limit if offset + limit < len(open_items) else None,
            "query_records": copy.deepcopy(view.get("query_records", []))[:limit],
            "query_total": view.get("query_records_total", len(view.get("query_records", []))),
            "queries_truncated": bool(view.get("query_records_truncated", False))
                                 or len(view.get("query_records", [])) > limit,
            "note": "仅反映已发现的线索；邻接不等于传播，未知或受限不是反证。",
        })
    return result


def priority_propagation_gaps(coverage, limit=3):
    """仅挑选有已登记症状证据的开放邻居，供 Meta 判断处置；线索不是传播结论。"""
    gaps = []
    for family in coverage:
        for task in family.get("priority_tasks", family.get("open_tasks", [])):
            if task.get("status") not in {"pending", "unresolved", "deferred"}:
                continue
            clues = [clue for clue in task.get("symptom_clues", []) if clue.get("evidence_refs")]
            if not clues:
                continue
            gaps.append({"seed_id": family["seed_id"], **copy.deepcopy(task),
                         "symptom_clues": clues, "chains_used": family["chains_used"],
                         "chains_limit": family["chains_limit"]})
    return gaps[:limit]


def arbitrate(case, *, synthesis_feedback=None, recovery_history=None):
    """全局比较与回溯；结案只交接调查，不选择最终答案。"""
    handoff_reported = False
    page_offsets = {}
    symptom_offset = 0
    # 查看摘要也消耗模型调用。限制每轮仲裁翻页，并避免在相同范围来回翻页。
    page_turns = 0
    page_turn_limit = 4
    visited_family_pages = set()
    visited_symptom_pages = set()
    displayed_tasks = {}
    while True:
        if case.finalize_only and not handoff_reported:
            case.handoff_to_meta()
            handoff_reported = True
        chains = meta_chains(case)
        questions = investigation_questions(case)
        case.meta.seen.update(refs_in(chains) | refs_in(case.symptoms)
                              | refs_in(case.investigation_stops) | refs_in(case.requests) | refs_in(questions)
                              | refs_in(getattr(case, "seed_graph_handoffs", {}))
                              | refs_in(synthesis_feedback or {}))
        coverage = family_coverage(case, page_offsets)
        priority_gaps = priority_propagation_gaps(coverage)
        for family in coverage:
            for task in family["open_tasks"] + [g for g in priority_gaps if g["seed_id"] == family["seed_id"]]:
                displayed_tasks[(family["seed_id"], task["task_id"])] = (
                    task["source_chain_id"], task["source_revision"])
            family.pop("priority_tasks", None)
        symptom_limit = max(1, getattr(case.cfg, "branch_frontier_summary_limit", 12))
        if symptom_offset >= len(case.symptoms):
            symptom_offset = max(0, ((len(case.symptoms) - 1) // symptom_limit) * symptom_limit) if case.symptoms else 0
        symptom_page = {
            "items": [{**copy.deepcopy(case.symptoms[i]), "symptom_index": i}
                      for i in range(symptom_offset, min(len(case.symptoms), symptom_offset + symptom_limit))],
            "total": len(case.symptoms), "page_offset": symptom_offset,
            "next_offset": symptom_offset + symptom_limit if symptom_offset + symptom_limit < len(case.symptoms) else None,
            "truncated": len(case.symptoms) > symptom_limit,
        }
        investigated_targets = {t.get("target_symptom_index") for t in case.followups}
        priority_symptoms = []
        slots = max(0, 3 - len(priority_gaps))
        if slots and case.symptoms:
            for row in reversed(case.symptom_snapshot()):
                if row["symptom_index"] in investigated_targets:
                    continue
                if any(_explains_symptom(case, submitted_view(c), row) for c in case.chains.values()):
                    continue
                priority_symptoms.append(row)
                if len(priority_symptoms) >= slots:
                    break
        visited_family_pages.update((family["seed_id"], family["page_offset"]) for family in coverage)
        visited_symptom_pages.add(symptom_offset)
        recovery_no = synthesis_feedback.get("meta_return_no") if synthesis_feedback else None
        used_recovery = sum(t.get("recovery_no") == recovery_no for t in case.followups) if recovery_no else 0
        normal_used = sum(not t.get("recovery_no") for t in case.followups)
        left = (case.cfg.synthesis_recovery_followups_per_round - used_recovery
                if recovery_no else case.cfg.meta_followups - normal_used)
        left = max(0, left)
        ctx = {
            "mode": "arbitrate", "chains": chains, "symptoms": symptom_page["items"],
            "seed_rounds": case.rounds, "followups": case.followups,
            "followups_left": 0 if case.finalize_only else left,
            "supplement_requests": case.requests, "investigation_stops": case.investigation_stops,
            "open_investigation_questions": questions,
            "family_coverage": coverage,
            "priority_propagation_gaps": priority_gaps,
            "priority_unexplained_symptoms": priority_symptoms,
            "symptom_coverage": symptom_page,
            "page_turns_left": max(0, page_turn_limit - page_turns),
            "finalize_only": case.finalize_only,
            "synthesis_feedback": copy.deepcopy(synthesis_feedback),
            "recovery_history": copy.deepcopy(recovery_history or []),
        }
        actions = ["conclude"]
        if not case.finalize_only and page_turns < page_turn_limit:
            actions.extend(["view_family_tasks", "view_symptoms"])
        if not case.finalize_only and left:
            actions.append("followup")
        action = case.meta.step(ctx, actions)
        try:
            if action["action"] == "view_symptoms":
                offset = action.get("offset")
                require(type(offset) is int and 0 <= offset < len(case.symptoms)
                        and offset % symptom_limit == 0 and offset not in visited_symptom_pages,
                        "offset 必须是尚未展示的有效症状页起点")
                symptom_offset = offset
                page_turns += 1
                case.meta.accepted()
                continue
            if action["action"] == "view_family_tasks":
                seed_id = action.get("seed_id")
                family = next((item for item in coverage if item["seed_id"] == seed_id), None)
                require(family is not None, "请选择 family_coverage 中的真实 seed_id")
                offset = action.get("offset")
                limit = max(1, getattr(case.cfg, "branch_frontier_summary_limit", 12))
                require(type(offset) is int and 0 <= offset < family["open_total"]
                        and offset % limit == 0, "offset 必须是有效页起点")
                require((seed_id, offset) not in visited_family_pages, "该家族页已经展示，请选择尚未展示的页")
                page_offsets[seed_id] = offset
                page_turns += 1
                case.meta.accepted()
                continue
            if action["action"] == "followup":
                if recovery_no:
                    case.followup(action, meta_calls_to_keep=1 + case.cfg.meta_synthesis_reserve,
                                  recovery_no=recovery_no)
                else:
                    case.followup(action, meta_calls_to_keep=1 + case.cfg.meta_synthesis_reserve)
                case.meta.accepted()
                continue
            conclusion = check_conclusion(case, action, priority_gaps, coverage, symptom_page, displayed_tasks)
        except Invalid as exc:
            case.meta.reject(exc)
            continue
        case.meta.accepted()
        case.rt.records.save(f"meta/recovery_{recovery_no:02d}/conclusion.json" if recovery_no
                             else "meta/conclusion.json", conclusion)
        case.rt.records.event(case.meta.id, "concluded", conclusion)
        case.meta_conclusion = conclusion
        return conclusion


def arbitrate_legacy(case):
    handoff_reported = False
    failures, last_proposal, feedback = 0, None, []
    while True:
        if case.finalize_only and not handoff_reported:
            case.handoff_to_meta()
            handoff_reported = True
        chains = meta_chains(case)
        options = selection_options(case)
        case.meta.seen.update(
            refs_in(chains) | refs_in(case.symptoms) | refs_in(case.investigation_stops) | refs_in(case.requests)
        )
        retries_left = max(0, case.cfg.max_meta_selection_retries - max(0, failures - 1))
        ctx = {
            "mode": "arbitrate", "chains": chains, "selection_catalog": selection_catalog(options),
            "selection_feedback": feedback,
            "selection_retry": {"failures": failures, "retries_left": retries_left,
                                "max_retries": case.cfg.max_meta_selection_retries},
            "symptoms": case.symptoms, "seed_rounds": case.rounds,
            "followups": case.followups,
            "followups_left": 0 if case.finalize_only else case.cfg.meta_followups - len(case.followups),
            "supplement_requests": case.requests, "investigation_stops": case.investigation_stops,
            "finalize_only": case.finalize_only,
        }
        if failures:
            _selection_event(case, "meta_selection_retry", retry_no=failures,
                             max_retries=case.cfg.max_meta_selection_retries, issue_count=len(feedback))
        try:
            a = case.meta.step(ctx, ["select"] if case.finalize_only else ["select", "followup"])
        except Limit as exc:
            if last_proposal is None:
                raise
            return _finish_partial(case, last_proposal, failures, str(exc))
        if a["action"] == "followup":
            try:
                remaining_attempts = 1 + case.cfg.max_meta_selection_retries - failures
                case.followup(a, meta_calls_to_keep=remaining_attempts + case.cfg.meta_synthesis_reserve)
                case.meta.accepted()
                # 补查会更新链版本；旧选择不能在新版本上作为保留答案的替身。
                last_proposal, feedback = None, []
            except Invalid as exc:
                case.meta.reject(exc)
            continue
        last_proposal = copy.deepcopy(a)
        result = check_selection(case, a, options)
        remaining_keys = {_issue_key(issue) for issue in result["issues"]}
        resolved = [issue for issue in feedback if _issue_key(issue) not in remaining_keys]
        for issue in resolved:
            _selection_event(case, "meta_selection_resolved", issue=issue,
                             reason="该问题已修正，或对应不合格条目已不再选择")
        if result["valid"]:
            _selection_event(case, "meta_selection_valid", selection_failures=failures,
                             root_count=len(result["selection"]["root_causes"]),
                             edge_count=len(result["selection"]["propagation"]),
                             reason="内容纠错通过" if failures else "选择校验通过")
            return _save_selection(case, result["selection"], {
                "selection_failures": failures, "used_partial_selection": False, "issues": [],
                "resolved_issues": resolved,
                "retained": result["selection"],
            })
        failures += 1
        feedback = result["issues"]
        case.meta_selection_report = {
            "selection_failures": failures, "used_partial_selection": False,
            "last_proposal": last_proposal, "issues": feedback,
            "resolved_issues": resolved,
        }
        case.rt.records.save("meta/selection_validation.json", case.meta_selection_report)
        _selection_event(case, "meta_selection_rejected", selection_failures=failures,
                         max_retries=case.cfg.max_meta_selection_retries, issues=feedback, issue_count=len(feedback))
        for issue in feedback:
            _selection_event(case, "meta_selection_issue", issue=issue, reason=issue["message"])
        if failures > case.cfg.max_meta_selection_retries:
            return _finish_partial(case, last_proposal, failures, "Meta 内容纠错次数已用完")
        # 内容纠错不走共用三次 reject；格式错误、工具错误仍由原 Session 规则处理。


