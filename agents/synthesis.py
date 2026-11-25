"""综合完整调查记录，用本案例的证据支撑根因预测和传播关系。"""

import copy
import json
from pathlib import Path

from filesystem import filesystem_path
from .loop import Invalid, Session, StageFailure, abnormal_refs, require
from output_contract import EvidenceSelectionError, FAULT_KINDS, export_selected_answer
from state import dump, refs_in
import state


def _read(path):
    return json.loads(filesystem_path(path).read_text(encoding="utf-8-sig"))


def _review_categories(case):
    """审核按具体根因和边分类；旧版本与反对、未知不被丢弃。"""
    groups = {"支持": [], "反对": [], "尚无法确认": [], "审核技术失败或未完成": []}
    names = {"support": "支持", "contradict": "反对", "unknown": "尚无法确认"}
    for c in case.ordered():
        for review in c.get("reviews", []):
            base = {"chain_id": c["chain_id"], "revision": review.get("revision"),
                    "review_id": review.get("review_id")}
            if review.get("status") != "reviewed":
                groups["审核技术失败或未完成"].append({**base, "review": review})
                continue
            for key, label in (("component_verdict", "根因组件"), ("fault_kind_verdict", "故障类型"),
                               ("root_verdict", "旧协议合并根因（无法拆分）")):
                claim = review.get(key) or {}
                if claim.get("verdict") in names:
                    groups[names[claim["verdict"]]].append({**base, "object": label, "verdict": claim})
            for edge in review.get("edge_verdicts", []):
                if edge.get("verdict") in names:
                    groups[names[edge["verdict"]]].append({**base, "object": "传播边", "verdict": edge})
    return groups


def _snapshots(case):
    """保留每个真实链版本及该版本审核；不以最终提交状态预先筛掉候选。"""
    result = []
    for c in case.ordered():
        folder = case.rt.records.root / "l1" / c["chain_id"]
        allowed = set(case.walkers[c["chain_id"]].seen) | set(case.reviewers[c["chain_id"]].seen)
        paths = (path for path in filesystem_path(folder).glob("v*.json") if path.stem[1:].isdigit())
        for path in sorted(paths, key=lambda path: int(path.stem[1:])):
            snapshot = _read(path)
            rev = snapshot["revision"]
            reviews = [r for r in c.get("reviews", []) if r.get("revision") == rev]
            refs = allowed | refs_in(snapshot) | refs_in(reviews)
            result.append({"chain": snapshot, "reviews": reviews,
                           "allowed_evidence_refs": sorted(refs),
                           "current_revision": c["revision"], "final_status": c["status"]})
    return result


def collect_trajectory(case, conclusion):
    """不截取事件；保留事件顺序、四类角色、证据及实际失败状态。"""
    event_file = filesystem_path(case.rt.records.root / "events.jsonl")
    events = []
    if event_file.is_file():
        with event_file.open(encoding="utf-8") as stream:
            events = [json.loads(line) for line in stream if line.strip()]
    events.sort(key=lambda x: x["seq"])
    groups = {"seed_agent": [], "L1_AGENT": [], "L2AGENT": [], "META_AGENT": [], "其他阶段": []}
    failures = []
    for event in events:
        agent = event.get("agent_id", "")
        group = ("seed_agent" if agent.startswith("seed") else
                 "L1_AGENT" if agent.startswith("l1:") else
                 "L2AGENT" if agent.startswith("l2:") else
                 "META_AGENT" if agent.startswith("meta") else "其他阶段")
        groups[group].append(event)
        if event.get("event") in {"investigation_stopped", "action_rejected", "tool_budget_exhausted",
                                  "evidence_validation_failed", "root_stop_confirmation_unfinished"}:
            failures.append(event)
    snapshots = _snapshots(case)
    changes, previous = [], {}
    for item in snapshots:
        chain = item["chain"]
        cid = chain["chain_id"]
        old = previous.get(cid)
        if old and (old.get("root") != chain.get("root") or old.get("edges") != chain.get("edges")):
            changes.append({"chain_id": cid, "previous_revision": old["revision"],
                            "revision": chain["revision"], "previous_root": old.get("root"),
                            "root": chain.get("root"), "previous_edges": old.get("edges"),
                            "edges": chain.get("edges"),
                            "note": "版本内容发生变化；是否被证据推翻须结合审核和后续证据判断"})
        previous[cid] = chain
    evidence = {}
    for path in sorted(filesystem_path(case.out / "evidence").glob("*.json")):
        item = _read(path)
        # 仅交本案例保存的查询和数据；不交数据目录、案例文件名或隐藏标签。
        evidence[path.stem] = {k: item[k] for k in ("evidence_ref", "data_version", "tool_version", "kind", "sql",
                                                       "rows", "truncated", "details", "saved_limit") if k in item}
    return {
        "事件按角色分类": groups,
        "审核按支持反对未确认和失败分类": _review_categories(case),
        "历史候选和传播路径的版本变化": changes,
        "失败与未完成事件": failures,
        "调查停止记录": case.investigation_stops,
        "初始及补充候选": case.rounds,
        "整案症状": case.symptom_snapshot(),
        "补查请求及实际返回": case.requests,
        "META补查及实际返回": case.followups,
        "META结案": conclusion,
        "首次META结案": copy.deepcopy(getattr(case, "initial_meta_conclusion", None)),
        "综合回流历史": copy.deepcopy(getattr(case, "recovery_history", [])),
        "真实链版本和审核": snapshots,
        "实际证据": evidence,
        "轨迹说明": "事件序号为记录顺序，并行分支的先后不自动代表因果；原始数据可能有截断标记。",
    }


def _problem(path, message):
    return {"path": path, "message": str(message)}


def _brief_reason(text):
    """理由摘录最多 160 字符，完整原文由所在条目的对象编号提供。"""
    text = str(text or "").strip()
    if len(text) <= 160:
        return {"summary": text, "summary_truncated": False}
    prefix = text[:159]
    sentence_end = max(prefix.rfind(mark) for mark in ("。", "！", "？", "\n")) + 1
    if sentence_end >= 80:
        prefix = prefix[:sentence_end]
    return {"summary": prefix.rstrip() + "…", "summary_truncated": True}


def _chain_trajectory(versions, events, index):
    """每条链汇总一次实际动作和各版审核，回查编号指向已有原文。"""
    latest = max(versions, key=lambda version: version["revision"])
    by_revision = {version["revision"]: version for version in versions}
    decisions = [event for event in events if event["event"] == "decision_applied"]
    actions = ("choose_symptom", "extend", "fork", "backtrack", "reseed", "review", "submit", "stop")
    # 旧记录缺少实际动作事件时，用 null 表示次数未知。
    counts = dict.fromkeys(actions, 0 if decisions else None)
    steps, reviews = [], {}
    for version in versions:
        for entry in version["reviews"]:
            review = index[entry["object_id"]]["object"]
            key = (review.get("review_id"), review.get("revision"))
            reviews[key] = (review, entry["object_id"], None)
    review_requests = [event for event in events if event["event"] == "review_requested"]
    for event in events:
        if event["event"] == "review_received":
            data = index[event["object_id"]]["object"]["data"]
            review = data.get("review", data)
            key = (review.get("review_id"), review.get("revision"))
            reviews[key] = (review, event["object_id"], event["seq"])
    for event in decisions:
        data = index[event["object_id"]]["object"]["data"]
        action = data.get("action")
        if action not in counts:
            continue
        counts[action] += 1
        # 首次送审由 L2 条目呈现，复审保留对上一轮审核的处理理由。
        if action == "review" and not data.get("review_received"):
            continue
        step = {"seq": event["seq"], "agent": "L1", "action": action,
                "input_revision": data.get("input_revision"),
                "output_revision": data.get("output_revision"),
                **_brief_reason(data.get("reason")), "source_object_ids": [event["object_id"]]}
        for key in ("review_received", "child_chain_id", "submitted_revision", "unreviewed_revision"):
            if data.get(key) is not None:
                step[key] = data[key]
        if action == "review":
            request = next((item for item in reversed(review_requests)
                            if item["seq"] < event["seq"]
                            and item.get("revision") == data.get("output_revision")), None)
            if request is not None:
                step["seq"] = request["seq"]
                step["source_object_ids"].append(request["object_id"])
        result_revision = data.get("submitted_revision", data.get("output_revision"))
        result = by_revision.get(result_revision, {})
        if "nodes" in result:
            step["nodes_after"] = list(result["nodes"])
        steps.append(step)
    for review, object_id, seq in reviews.values():
        steps.append({"seq": seq, "agent": "L2", "action": "audit",
                      "review_id": review.get("review_id"), "revision": review.get("revision"),
                      "status": review.get("status"),
                      "component_verdict": (review.get("component_verdict") or {}).get("verdict"),
                      "fault_kind_verdict": (review.get("fault_kind_verdict") or {}).get("verdict"),
                      **({"legacy_root_verdict": review["root_verdict"].get("verdict")}
                         if isinstance(review.get("root_verdict"), dict) else {}),
                      "recommendation": review.get("recommendation"),
                      **_brief_reason(review.get("reason") or review.get("error")),
                      "source_object_ids": [object_id]})
    requested = max(len(review_requests), len(reviews), counts["review"] or 0)
    completed = sum(review.get("status") == "reviewed" for review, _, _ in reviews.values())
    failed = sum(review.get("status") in {"technical_failure", "budget_exhausted", "investigation_stopped"}
                 for review, _, _ in reviews.values())
    counts["review"] = requested
    steps.sort(key=lambda step: (step["seq"] is None, step["seq"]))
    return {"chain_id": latest["chain_id"], "current_revision": latest["current_revision"],
            "final_status": latest["final_status"],
            "action_counts": {"L1": counts, "L2": {"requested": requested, "completed": completed,
                                                      "failed": failed, "unfinished": requested - completed - failed}},
            "key_steps": steps}


def _trajectory_summary(working_set, index):
    """沿显式链号、审核号和补查请求关联过程，Meta 全局操作只记录一次。"""
    versions_by_chain, events_by_chain = {}, {}
    for version in working_set["真实链版本和审核"]:
        versions_by_chain.setdefault(version["chain_id"], []).append(version)
    for event in working_set["事件索引"]:
        events_by_chain.setdefault(event.get("chain_id"), []).append(event)

    seed_requests, requests_by_chain = {}, {}
    for entry in working_set["补查请求及实际返回"]:
        request = index[entry["object_id"]]["object"]
        requests_by_chain.setdefault(request.get("chain_id"), []).append((request, entry["object_id"]))
        for seed_id in (request.get("response") or {}).get("new_seed_ids", []):
            seed_requests.setdefault(seed_id, []).append((request, entry["object_id"]))
    seed_returns = {event["request_id"]: event for event in working_set["事件索引"]
                    if event["event"] == "seed_response" and event.get("request_id")}
    initial_seed_ids = {
        seed["seed_id"] for round_entry in working_set["初始及补充候选"]
        if round_entry.get("mode") == "initial" for seed in round_entry["seeds"]
    }
    assessments = {}
    for entry in working_set["META结案"]["branch_assessments"]:
        assessment = index[entry["object_id"]]["object"]
        assessments.setdefault(assessment.get("chain_id"), []).append({
            "revision": assessment.get("revision"), **_brief_reason(assessment.get("assessment")),
            "source_object_ids": [entry["object_id"]],
        })

    chains = []
    for chain_id, versions in versions_by_chain.items():
        summary = _chain_trajectory(versions, events_by_chain.get(chain_id, []), index)
        latest = max(versions, key=lambda version: version["revision"])
        chain = index[latest["object_id"]]["object"]
        seed_id = (chain.get("seed") or {}).get("seed_id")
        origin = {"type": "unknown", "seed_id": seed_id, "source_object_ids": [latest["object_id"]]}
        if chain.get("parent_chain_id"):
            origin.update(type="fork", parent_chain_id=chain["parent_chain_id"],
                          parent_revision=chain.get("parent_revision"))
        elif seed_id in seed_requests:
            sources = seed_requests[seed_id]
            origin = {"type": "reseed", "seed_id": seed_id, "requests": []}
            for request, object_id in sources:
                if request.get("target_type") == "seed":
                    origin["type"] = "meta_followup"
                origin["requests"].append({
                    "source_chain_id": request.get("chain_id"), "request_id": request.get("request_id"),
                    **_brief_reason((request.get("response") or {}).get("reason")),
                    "source_object_ids": [object_id],
                })
        elif seed_id in initial_seed_ids:
            origin["type"] = "initial_seed"
        for request, object_id in requests_by_chain.get(chain_id, []):
            if request.get("target_type") == "seed":
                continue  # Meta 派发的补种结果在全局 Meta 摘要中呈现。
            response = request.get("response") or {}
            event = seed_returns.get(request.get("request_id"), {})
            summary["key_steps"].append({
                "seq": event.get("seq"), "agent": "Seed", "action": "reseed_response",
                "request_id": request.get("request_id"), "status": response.get("status", "unknown"),
                "new_seed_ids": response.get("new_seed_ids", []), **_brief_reason(response.get("reason")),
                "source_object_ids": [object_id] + ([event["object_id"]] if event else []),
            })
        summary["key_steps"].sort(key=lambda step: (step["seq"] is None, step["seq"]))
        summary.update(origin=origin, meta_assessments=assessments.get(chain_id, []))
        chains.append(summary)

    # followup 的派发和返回属于同一次补查，使用调度器保存的 ticket 去重。
    followup_events, conclusion_events = {}, []
    for event in working_set["事件索引"]:
        data = index[event["object_id"]]["object"].get("data")
        if event["event"] in {"followup_dispatched", "followup_returned"} and isinstance(data, dict):
            key = (data.get("target_type"), data.get("chain_id"), data.get("question"))
            followup_events.setdefault(key, {})[event["event"]] = event
        elif event["event"] in {"concluded", "arbitration_failed"}:
            conclusion_events.append(event)
    meta_counts = {"followup_l1": 0, "followup_seed": 0, "conclude": 0}
    meta_steps = []
    for entry in working_set["META补查及实际返回"]:
        ticket = index[entry["object_id"]]["object"]
        target = ticket.get("target_type")
        if target in {"l1", "seed"}:
            meta_counts[f"followup_{target}"] += 1
        related = followup_events.get((target, ticket.get("chain_id"), ticket.get("question")), {})
        dispatched = related.get("followup_dispatched", {})
        returned = related.get("followup_returned", {})
        response = ticket.get("response") or {}
        step = {"seq": dispatched.get("seq"), "returned_seq": returned.get("seq"),
                "agent": "Meta", "action": "followup", "target_type": target,
                "chain_id": ticket.get("chain_id"),
                **_brief_reason(ticket.get("question") or ticket.get("reason")),
                "returned_status": ticket.get("returned_status") or response.get("status", "unfinished"),
                "returned_revision": ticket.get("returned_revision"),
                "new_seed_ids": ticket.get("new_seeds", response.get("new_seed_ids", [])),
                "source_object_ids": [entry["object_id"]] + [event["object_id"] for event in related.values()]}
        if response.get("reason"):
            step["response"] = _brief_reason(response["reason"])
        meta_steps.append(step)
    for event in conclusion_events:
        data = index[event["object_id"]]["object"]["data"]
        if event["event"] == "concluded":
            meta_counts["conclude"] += 1
        meta_steps.append({"seq": event["seq"], "agent": "Meta",
                           "action": "conclude" if event["event"] == "concluded" else "arbitration_failed",
                           **_brief_reason(data.get("termination_reason")),
                           "source_object_ids": [event["object_id"]]})
    if not conclusion_events:
        conclusion_id = working_set["META结案"]["object_id"]
        conclusion = index[conclusion_id]["object"]
        concluded = (not conclusion.get("failure") and conclusion.get("status") != "未完成"
                     and bool(conclusion.get("termination_reason"))
                     and all(isinstance(conclusion.get(key), list)
                             for key in ("branch_assessments", "confidence_scores", "open_issues")))
        meta_counts["conclude"] = int(concluded)
        if conclusion.get("termination_reason"):
            meta_steps.append({"seq": None, "agent": "Meta",
                               "action": "conclude" if concluded else "unfinished",
                               **_brief_reason(conclusion["termination_reason"]),
                               "source_object_ids": [conclusion_id]})
    meta_steps.sort(key=lambda step: (step["seq"] is None, step["seq"]))
    return {"chains": chains, "meta": {"action_counts": meta_counts, "key_steps": meta_steps}}


def _working_set(trajectory):
    """只按记录中显式存在的关系编目；所有原始对象均可按编号回查。"""
    index = {}

    def add(object_id, source, value):
        index[object_id] = {"source": source, "object": value}
        return object_id

    def fields(value, keys):
        return {key: value[key] for key in keys if key in value}

    versions = []
    for i, item in enumerate(trajectory["真实链版本和审核"]):
        chain = item["chain"]
        prefix = f"V-{chain['chain_id']}-{chain['revision']}"
        oid = add(prefix, f"/真实链版本和审核/{i}/chain", chain)
        reviews = []
        for j, review in enumerate(item["reviews"]):
            rid = add(f"{prefix}-R{j + 1:03d}", f"/真实链版本和审核/{i}/reviews/{j}", review)
            summary = {"object_id": rid, **fields(review, ("chain_id", "revision", "review_id", "status",
                                                            "supported_prefix", "score", "recommendation"))}
            for key in ("component_verdict", "fault_kind_verdict"):
                if isinstance(review.get(key), dict):
                    summary[key] = fields(review[key], ("service", "fault_kind", "verdict", "reason", "evidence_refs"))
            if isinstance(review.get("root_verdict"), dict):
                summary["legacy_root_verdict"] = fields(review["root_verdict"], ("verdict", "reason", "evidence_refs"))
            if isinstance(review.get("edge_verdicts"), list):
                summary["edge_verdicts"] = [{**fields(v, ("from", "to", "verdict", "reason", "evidence_refs",
                                            "assessed_direction", "assessed_verdict", "direction_mismatch", "mechanism", "scope")),
                                            "direction_status": "assessed" if "assessed_direction" in v else "legacy_unchecked"}
                                            for v in review["edge_verdicts"]]
            reviews.append(summary)
        version = {"object_id": oid, **fields(chain, ("chain_id", "revision", "l1_id", "l2_id",
                   "nodes", "root", "fault_diagnosis", "parent_chain_id", "parent_revision", "status")),
                   "reviews": reviews, "allowed_evidence_refs": item["allowed_evidence_refs"],
                   "current_revision": item["current_revision"], "final_status": item["final_status"],
                   "edges": []}
        for j, edge in enumerate(chain.get("edges", [])):
            edge_id = add(f"{prefix}-EDGE{j + 1:03d}", f"/真实链版本和审核/{i}/chain/edges/{j}", edge)
            version["edges"].append({"object_id": edge_id, **fields(edge, ("from", "to", "evidence_refs", "observed_windows", "evidence_scopes"))})
        for key in ("root", "fault_diagnosis", "seed", "symptom", "target_symptom"):
            if chain.get(key) is not None:
                linked = add(f"{prefix}-{key.upper()}", f"/真实链版本和审核/{i}/chain/{key}", chain[key])
                version[f"{key}_object_id"] = linked
        versions.append(version)

    # 按组件和类型索引已有主张及竞争候选；不把候选、审核或引用次数变成新的证据。
    comparisons = {}
    for version in versions:
        root = version.get("root") or {}
        service = root.get("service") or (version.get("nodes") or [None])[0]
        if not service:
            continue
        kinds = comparisons.setdefault(service, {})
        source = {"chain_id": version["chain_id"], "revision": version["revision"],
                  "version_object_id": version["object_id"]}
        kind = root.get("fault_kind")
        if kind is not None:
            reviews = []
            for review in version["reviews"]:
                verdict = review.get("fault_kind_verdict") or {}
                reviews.append({"object_id": review["object_id"], "review_id": review.get("review_id"),
                                "status": review.get("status"), "verdict": verdict.get("verdict"),
                                "evidence_refs": verdict.get("evidence_refs", [])})
            kinds.setdefault(kind, []).append({**source, "source_type": "root_claim",
                                                "root_object_id": version.get("root_object_id"),
                                                "evidence_refs": root.get("evidence_refs", []),
                                                "reviews": reviews})
        diagnosis = version.get("fault_diagnosis") or {}
        for candidate in diagnosis.get("candidates", []):
            kind = candidate.get("fault_kind")
            if kind is not None:
                kinds.setdefault(kind, []).append({**source, "source_type": "diagnosis_candidate",
                    "diagnosis_object_id": version.get("fault_diagnosis_object_id"),
                    "candidate_status": candidate.get("status"),
                    "evidence_refs": candidate.get("evidence_refs", [])})
    candidate_comparison = [
        {"service": service, "candidates": [
            {"fault_kind": kind, "sources": sources} for kind, sources in kinds.items()
        ]}
        for service, kinds in comparisons.items() if kinds
    ]

    events = []
    for role, rows in trajectory["事件按角色分类"].items():
        for i, event in enumerate(rows):
            oid = add(f"EV-{event['seq']:08d}", f"/事件按角色分类/{role}/{i}", event)
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            entry = {"object_id": oid, "seq": event["seq"], "role": role,
                     "agent_id": event.get("agent_id"), "event": event.get("event"),
                     "evidence_refs": sorted(refs_in(event)),
                     **fields(data, ("chain_id", "revision", "request_id"))}
            if event.get("event") == "tool_result":
                entry.update(tool=data.get("tool"), status=result.get("status"),
                             truncated=result.get("truncated"),
                             row_count=len(result["rows"]) if isinstance(result.get("rows"), list) else None)
            events.append(entry)
    events.sort(key=lambda event: event["seq"])

    evidence = []
    for ref, item in trajectory["实际证据"].items():
        add(ref, f"/实际证据/{ref}", item)
        rows = item.get("rows")
        evidence.append({"object_id": ref, "kind": item.get("kind"),
                         "row_count": len(rows) if isinstance(rows, list) else None,
                         "truncated": item.get("truncated")})

    sections = {"初始及补充候选": [], "seed症状": [], "调查停止记录": [],
                "补查请求及实际返回": [], "META补查及实际返回": []}
    for i, value in enumerate(trajectory["初始及补充候选"]):
        source = f"/初始及补充候选/{i}"
        oid = add(f"ROUND-{i + 1:06d}", source, value)
        seeds = []
        for j, seed in enumerate(value.get("seeds", [])):
            sid = add(f"{oid}-SEED{j + 1:03d}", f"{source}/seeds/{j}", seed)
            seed_entry = {"object_id": sid, **fields(seed, ("seed_id", "service", "round", "evidence_refs", "responds_to"))}
            if seed.get("target_symptom") is not None:
                seed_entry["target_symptom_object_id"] = add(f"{sid}-TARGET", f"{source}/seeds/{j}/target_symptom",
                                                             seed["target_symptom"])
            seeds.append(seed_entry)
        symptom_ids = []
        for j, symptom in enumerate(value.get("symptoms", [])):
            sid = add(f"{oid}-SYM{j + 1:03d}", f"{source}/symptoms/{j}", symptom)
            symptom_ids.append(sid)
            sections["seed症状"].append({"object_id": sid, "round_object_id": oid,
                                        **fields(symptom, ("service", "description", "evidence_refs"))})
        sections["初始及补充候选"].append({"object_id": oid, **fields(value, ("round", "mode")),
                                        "seeds": seeds, "symptom_object_ids": symptom_ids})
    for prefix, name in (("STOP", "调查停止记录"), ("REQ", "补查请求及实际返回"),
                         ("FOLLOWUP", "META补查及实际返回")):
        for i, value in enumerate(trajectory[name]):
            oid = add(f"{prefix}-{i + 1:06d}", f"/{name}/{i}", value)
            entry = {"object_id": oid, **fields(value, ("chain_id", "revision", "request_id", "target_type",
                                                       "returned_status", "returned_revision", "new_seeds", "resolved")),
                     "evidence_refs": sorted(refs_in(value))}
            if isinstance(value.get("response"), dict):
                entry["response"] = fields(value["response"], ("status", "new_seed_ids"))
            sections[name].append(entry)
    conclusion = trajectory["META结案"]
    history = trajectory.get("综合回流历史", [])
    initial = trajectory.get("首次META结案")
    previous_conclusions = ([initial] if initial is not None else []) + [
        entry["conclusion"] for entry in history if isinstance(entry.get("conclusion"), dict)
    ]
    for n, previous_conclusion in enumerate(previous_conclusions, 1):
        previous_id = f"META-CONCLUSION-R{n:03d}"
        add(previous_id, f"/历史META结案/{n - 1}", previous_conclusion)
        for key in ("branch_assessments", "confidence_scores", "open_issues"):
            for j, value in enumerate(previous_conclusion.get(key, [])):
                add(f"{previous_id}-{key.upper()}-{j + 1:06d}", f"/历史META结案/{n - 1}/{key}/{j}", value)
    # Never overwrite the last historical conclusion with the current one.
    conclusion_id = f"META-CONCLUSION-R{len(previous_conclusions) + 1:03d}"
    add(conclusion_id, "/META结案", conclusion)
    meta = {"object_id": conclusion_id, **fields(conclusion, ("termination_reason",
            "priority_propagation_gaps", "propagation_gap_assessments", "family_coverage",
            "symptom_coverage", "symptom_assessments"))}
    for key in ("branch_assessments", "confidence_scores", "open_issues"):
        meta[key] = []
        for i, value in enumerate(conclusion.get(key, [])):
            oid = add(f"{conclusion_id}-{key.upper()}-{i + 1:06d}", f"/META结案/{key}/{i}", value)
            keys = ("chain_id", "revision", "conclusion_ref", "conclusion_confidence", "issue")
            meta[key].append({"object_id": oid, **fields(value, keys)})
    matched_reviews = {(r.get("chain_id", version.get("chain_id")), r.get("revision"), r.get("review_id"))
                       for version in versions for r in version["reviews"]}
    unmatched_reviews = []
    for category, entries in trajectory["审核按支持反对未确认和失败分类"].items():
        for i, entry in enumerate(entries):
            key = (entry.get("chain_id"), entry.get("revision"), entry.get("review_id"))
            if key not in matched_reviews:
                oid = add(f"UNMATCHED-REVIEW-{category}-{i + 1:06d}",
                          f"/审核按支持反对未确认和失败分类/{category}/{i}", entry)
                unmatched_reviews.append({"object_id": oid, "category": category,
                                          **fields(entry, ("chain_id", "revision", "review_id", "object"))})
    working_set = {"真实链版本和审核": versions, "candidate_comparison": candidate_comparison,
                   **sections, "META结案": meta,
                   "未匹配版本的审核": unmatched_reviews,
                   "事件索引": events, "证据索引": evidence, "轨迹说明": trajectory["轨迹说明"],
                   "说明": "所有事件及证据均列索引，未关联不代表无关；只记录明确关系，顺序不是因果。"}
    working_set["综合回流历史"] = []
    if trajectory.get("首次META结案") is not None:
        add("META-INITIAL-CONCLUSION", "/首次META结案", trajectory["首次META结案"])
        working_set["首次META结案"] = {"object_id": "META-INITIAL-CONCLUSION",
                                     **trajectory["首次META结案"]}
    for i, item in enumerate(trajectory.get("综合回流历史", [])):
        oid = add(f"RECOVERY-{i + 1:06d}", f"/综合回流历史/{i}", item)
        working_set["综合回流历史"].append({"object_id": oid, **item})
    working_set["trajectory_summary"] = _trajectory_summary(working_set, index)
    working_set["整案症状"] = trajectory.get("整案症状", [])
    working_set["edge_conflicts"] = _edge_conflicts(trajectory["真实链版本和审核"])
    return working_set, index


def _edge_conflicts(snapshots):
    """Linear review index. Unknown scope stays a potential conflict, never a global veto."""
    conflicts = []
    for snapshot in snapshots:
        chain = snapshot["chain"]
        for review_no, review in enumerate(snapshot["reviews"], 1):
            if review.get("status") != "reviewed":
                continue
            for i, edge in enumerate(review.get("edge_verdicts", [])):
                mismatch = edge.get("direction_mismatch") and edge.get("assessed_verdict") == "support"
                if edge.get("verdict") != "contradict" and not mismatch:
                    continue
                conflicts.append({"conflict_id": f"{review['review_id']}:E{i + 1:02d}",
                    "from": edge["from"], "to": edge["to"], "chain_id": chain["chain_id"],
                    "revision": chain["revision"], "review_id": review["review_id"],
                    "review_object_id": f"V-{chain['chain_id']}-{chain['revision']}-R{review_no:03d}",
                    "kind": "direction_mismatch" if mismatch else "contradict",
                    "assessed_direction": edge.get("assessed_direction"),
                    "scope": edge.get("scope", "scope_unknown"),
                    "scope_status": "requires_evidence_comparison",
                    "evidence_scopes": [scope for candidate in chain.get("edges", [])
                                        if (candidate["from"], candidate["to"]) == (edge["from"], edge["to"])
                                        for scope in candidate.get("evidence_scopes", [])],
                    "evidence_refs": edge.get("evidence_refs", []), "reason": edge.get("reason", "")})
    return conflicts


def _compact_working_set(working_set, index):
    """Lossless object interning for the model view; full snapshots remain the validator."""
    compact = copy.deepcopy(working_set)
    pooled, interned = {}, {}

    def intern(value):
        signature = dump(value)
        if signature not in interned:
            oid = f"COMPACT-{len(interned) + 1:06d}"
            interned[signature] = oid
            pooled[oid] = value
            index[oid] = {"source": "/compact_objects", "object": value}
        return {"object_id": interned[signature]}

    for version in compact["真实链版本和审核"]:
        version["allowed_evidence_refs"] = intern(version["allowed_evidence_refs"])
        for field in ("root", "fault_diagnosis"):
            if version.get(field) is not None:
                version[field] = intern(version[field])
        for edge in version["edges"]:
            if edge.get("evidence_scopes"):
                edge["evidence_scopes"] = intern(edge["evidence_scopes"])
        for review in version["reviews"]:
            for field in ("component_verdict", "fault_kind_verdict", "legacy_root_verdict", "edge_verdicts"):
                if field in review:
                    review[field] = intern(review[field])
    for service in compact.get("candidate_comparison", []):
        for candidate in service["candidates"]:
            for source in candidate["sources"]:
                # Same references/verdicts are already in the version/review directory.
                for field in ("reviews", "evidence_refs"):
                    source.pop(field, None)
    counts, key_events = {}, []
    for event in compact["事件索引"]:
        name = event["event"]
        counts[name] = counts.get(name, 0) + 1
        if name in {"decision_applied", "investigation_stopped", "followup_returned", "concluded",
                    "symptoms_registered", "family_dispatch_interrupted", "arbitration_failed"}:
            key_events.append({k: event[k] for k in ("object_id", "seq", "event", "chain_id", "revision") if k in event})
    compact["事件索引"] = {"counts": counts, "key_events": key_events,
                          "note": "完整事件保存在 original_index，EV-序号 可按需读取；未列出不代表未发生。"}
    initial = compact.get("首次META结案")
    current = index[compact["META结案"]["object_id"]]["object"]
    if initial and {k: v for k, v in initial.items() if k != "object_id"} == current:
        compact["首次META结案"] = {"object_id": compact["META结案"]["object_id"], "same_as_current": True}
    elif initial:
        compact["首次META结案"] = intern(initial)
    compact["综合回流历史"] = [intern(item) for item in compact.get("综合回流历史", [])]
    for conflict in compact.get("edge_conflicts", []):
        if conflict.get("evidence_scopes"):
            conflict["evidence_scopes"] = intern(conflict["evidence_scopes"])
    # Detailed steps duplicate the version table; keep counts, origin, and source IDs.
    for chain in compact.get("trajectory_summary", {}).get("chains", []):
        steps = chain.pop("key_steps", [])
        chain["step_sources"] = sorted({oid for step in steps for oid in step.get("source_object_ids", [])})
    compact["compact_objects"] = pooled
    compact["说明"] = "COMPACT 对象在 compact_objects 内展开一次；所有版本、审核及反证仍可见，完整原文可按 object_id 回查。权限与连续前段始终按完整历史校验。"
    return compact


def check_draft(case, draft, snapshots, *, case_evidence_refs):
    """核对预测的证据与来源，传播边另须符合调查链版本和前段。"""
    errors = []
    selection = {"selected_chain_ids": [], "root_causes": [], "propagation": [], "root_provenance": []}
    cache = {}
    conflicts_by_edge = {}
    for conflict in _edge_conflicts(snapshots):
        conflicts_by_edge.setdefault((conflict["from"], conflict["to"]), []).append(conflict)
    if not isinstance(draft, dict):
        return None, [_problem("$", "必须是单个对象")], []
    if not isinstance(draft.get("reason"), str) or not draft["reason"].strip():
        errors.append(_problem("reason", "需要综合说明争议、反证和未完成调查"))
    if set(draft) != {"root_causes", "propagation", "reason"}:
        errors.append(_problem("$", "顶层只允许 root_causes、propagation、reason"))
    by_version = {(s["chain"]["chain_id"], s["chain"]["revision"]): s for s in snapshots}
    case_evidence_refs = set(case_evidence_refs)
    selected_snapshots, edge_sources = {}, {}
    for group, keys in (("root_causes", ("service", "fault_kind")), ("propagation", ("from", "to"))):
        is_root = group == "root_causes"
        items = draft.get(group)
        if not isinstance(items, list):
            errors.append(_problem(group, "必须是列表"))
            continue
        used = set()
        for i, item in enumerate(items):
            loc = f"{group}[{i}]"
            if not isinstance(item, dict):
                errors.append(_problem(loc, "必须是对象")); continue
            expected = set(keys) | {"source_chain_id", "source_revision", "evidence_refs", "reason"}
            optional = {"conflict_resolution"} if not is_root else set()
            if set(item) - optional != expected or not all(isinstance(item.get(k), str) and item[k].strip() for k in keys):
                errors.append(_problem(loc, "字段不完整或含契约外字段")); continue
            key = tuple(item[k] for k in keys)
            if key in used:
                errors.append(_problem(loc, "相同根因或有向边重复")); continue
            used.add(key)
            if is_root and item["fault_kind"] not in FAULT_KINDS:
                errors.append(_problem(loc + ".fault_kind", "不是正式故障类型")); continue
            if any(name not in case.rt.names for name in (key[:1] if is_root else key)):
                errors.append(_problem(loc, "服务没有在本案例遥测中观察到")); continue
            cid, rev = item.get("source_chain_id"), item.get("source_revision")
            source = None
            if not (is_root and cid is None and rev is None):
                if not isinstance(cid, str) or type(rev) is not int or (cid, rev) not in by_version:
                    message = "来源链或版本不存在"
                    if is_root:
                        message += "；请填写真实链和版本，或将两个来源字段同时填为 null"
                    errors.append(_problem(loc, message))
                    continue
                source = by_version[(cid, rev)]
            chain = source["chain"] if source is not None else {}
            if not is_root:
                edges = [(edge["from"], edge["to"]) for edge in chain.get("edges", [])]
                if key not in edges:
                    message = f"{cid} 第 {rev} 版没有 {key[0]} → {key[1]}，请核对传播方向和来源版本"
                    errors.append(_problem(loc, message))
                    continue
            if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                errors.append(_problem(loc + ".reason", "需要说明证据、审核及未解决问题")); continue
            refs = item.get("evidence_refs")
            if not isinstance(refs, list) or not refs or not all(isinstance(r, str) for r in refs):
                errors.append(_problem(loc + ".evidence_refs", "需要非空真实证据编号列表")); continue
            available_refs = case_evidence_refs if is_root else set(source["allowed_evidence_refs"])
            try:
                refs = case.rt.refs(refs, available_refs)
                require(abnormal_refs(case, refs, cache=cache), "需要本次异常窗口证据，不能仅用正常窗口")
                if not is_root:
                    require(any(case.rt.tele.evidence(r)["kind"] in {"metric", "trace"} for r in refs),
                            "传播边至少需要指标或调用证据")
                    conflicts = conflicts_by_edge.get(key, [])
                    if conflicts:
                        resolution = item.get("conflict_resolution")
                        require(isinstance(resolution, dict), "该边存在历史反证/方向冲突，须给 conflict_resolution 或暂不选入")
                        ids = resolution.get("conflict_ids")
                        require(isinstance(ids, list) and all(isinstance(x, str) for x in ids)
                                and set(ids) == {c["conflict_id"] for c in conflicts},
                                "conflict_ids 必须覆盖该边相关冲突，不得忽略旧反证")
                        basis = resolution.get("basis")
                        require(basis in {"different_scope", "new_evidence"}, "冲突处置须依据 different_scope 或 new_evidence")
                        resolution_refs = case.rt.refs(resolution.get("evidence_refs"), available_refs)
                        rejected_refs = set().union(*(set(c["evidence_refs"]) for c in conflicts))
                        if basis == "new_evidence":
                            require(bool(set(resolution_refs) - rejected_refs), "new_evidence 不能仅重复冲突原证据")
                        if basis == "different_scope":
                            require(all(isinstance(resolution.get(k), str) and resolution[k].strip()
                                        for k in ("selected_scope", "conflicting_scope")), "范围不同需说明双方操作/请求范围")
                            require(resolution["selected_scope"] != resolution["conflicting_scope"], "不能把同一范围声明为不同范围")
                        refs = list(dict.fromkeys(refs + resolution_refs))
            except Invalid as exc:
                errors.append(_problem(loc + ".evidence_refs", exc)); continue
            selected_item = {**dict(zip(keys, key)), "evidence_refs": refs}
            selection[group].append(selected_item)
            if is_root:
                root = chain.get("root") or {}
                selection["root_provenance"].append({
                    **selected_item,
                    "source_chain_id": cid,
                    "source_revision": rev,
                    "independent_inference": (root.get("service"), root.get("fault_kind")) != key,
                })
            else:
                edge_sources[key] = (loc, cid, rev, edges[:edges.index(key)])
            if source is not None:
                selected_snapshots[(cid, rev)] = {
                    **chain, "last_review": source["reviews"][-1] if source["reviews"] else None,
                }
    chosen_edges = set(edge_sources)
    for loc, cid, rev, prefix in edge_sources.values():
        missing = [f"{start} → {end}" for start, end in prefix if (start, end) not in chosen_edges]
        if missing:
            errors.append(_problem(loc, f"{cid} 第 {rev} 版缺少连续前段 {'、'.join(missing)}，请核对整段传播路径"))
    selection["selected_chain_ids"] = sorted({cid for cid, _ in selected_snapshots})
    return selection, errors, list(selected_snapshots.values())


def synthesize(case, conclusion, *, round_no=1, final_mode=False, meta_returns_left=0, recovery_history=None, carryover=None):
    """每轮重建全案工作集；草稿、补查请求与正式导出严格分离。"""
    case.rt.budget.begin_synthesis_round(final_mode=final_mode)
    trajectory = collect_trajectory(case, conclusion)
    if recovery_history is not None:
        trajectory["综合回流历史"] = copy.deepcopy(recovery_history)
    snapshots = trajectory["真实链版本和审核"]
    working_set, index = _working_set(trajectory)
    model_working_set = _compact_working_set(working_set, index)
    prefix = f"synthesis/round_{round_no:02d}"
    case.rt.records.save(f"{prefix}/trajectory.json", trajectory)
    case.rt.records.save(f"{prefix}/working_set.json", working_set)
    case.rt.records.save(f"{prefix}/model_working_set.json", model_working_set)
    case.rt.records.save(f"{prefix}/original_index.json", index)
    lookup = Session(case.rt, "synthesis_lookup", "synthesis:lookup")
    final = Session(case.rt, "synthesis", "synthesis:final")
    lookup_max = case.cfg.synthesis_lookup_max
    formal_max = 1 + case.cfg.synthesis_final_max_retries if final_mode else case.cfg.synthesis_max_attempts
    lookup_calls = formal_attempts = 0
    carryover = carryover or {}
    previous = copy.deepcopy(carryover.get("previous_answer"))
    feedback = copy.deepcopy(carryover.get("validation_feedback", []))
    original_objects = copy.deepcopy(carryover.get("original_objects", []))
    if final_mode and feedback:
        feedback = [{"code": "FINAL_PREDICTION", "path": "root_causes",
                     "message": "不能再请求 Meta；请依据已有证据选择最可能根因，并合并已有合法链版本中有支持的传播路径与分支；无可用传播证据时允许为空并说明缺口。"}, *feedback]
        for item in feedback:
            if item.get("code") == "EMPTY_ROOT_CAUSES":
                item.update(message="请基于已有证据提交非空根因，说明不确定性。",
                            allowed_actions=["request_original_objects", "submit_answer"], meta_returns_left=0)
    last_empty, last_empty_id = None, None
    repairing = False
    input_sizes = []

    def result(status, **extra):
        if status == "final_required":
            extra["carryover"] = {"previous_answer": copy.deepcopy(previous),
                                  "validation_feedback": copy.deepcopy(feedback),
                                  "original_objects": copy.deepcopy(original_objects)}
        return None, None, {"status": status, "round_no": round_no, "lookup_calls": lookup_calls,
                            "formal_attempts": formal_attempts, **extra}

    grace_used = False
    while formal_attempts < formal_max or (not final_mode and last_empty is not None and not grace_used):
        if formal_attempts >= formal_max:
            grace_used = True
        if not final_mode and not case.rt.budget.can_synthesis_continue():
            return result("final_required")
        # 普通阶段、最终提交前均允许回查；首次最终答案之后只允许纠错。
        can_lookup = (not repairing and lookup_calls < lookup_max and case.rt.budget.can_synthesis_lookup())
        if can_lookup:
            phase, session = ("final" if final_mode else "explore"), lookup
        else:
            phase, session = ("repair" if repairing else "final"), final
            if final_mode and not repairing:
                case.rt.budget.enter_synthesis_final()
                repairing = True
                phase = "repair"
        allowed = ["submit_answer"]
        if can_lookup:
            allowed.insert(0, "request_original_objects")
        if not repairing and not final_mode and meta_returns_left > 0:
            allowed.insert(-1, "request_meta_review")
        session.history = []
        session.accepted()
        context = {"working_set": model_working_set, "phase": phase, "allowed_actions": allowed,
                   "lookup_calls_used": lookup_calls, "lookup_calls_max": lookup_max,
                   "formal_attempts_used": formal_attempts, "formal_attempts_max": formal_max,
                   "meta_returns_left": meta_returns_left if not final_mode else 0,
                   "previous_answer": previous, "validation_feedback": feedback,
                   "original_objects": original_objects}
        size = session.synthesis_size(context)
        input_sizes.append({"phase": phase, **size})
        case.rt.records.save(f"{prefix}/input_size.json", input_sizes)
        if not size["fits"]:
            raise StageFailure(f"综合工作集及当前完整输入超限：{dump(size)}；未发送模型")
        if session is lookup:
            lookup_calls += 1
        try:
            draft = session.step(context, [], synthesis=True)
        except Invalid as exc:
            if session is final or final_mode:
                formal_attempts += 1
            if final_mode:
                case.rt.budget.enter_synthesis_final()
                repairing = True
            feedback = [_problem("$", exc)]
            case.rt.records.save(f"{prefix}/invalid_{lookup_calls:02d}_{formal_attempts:02d}.json", {"errors": feedback})
            continue
        case.rt.records.save(f"{prefix}/response_{lookup_calls:02d}_{formal_attempts:02d}.json", {"answer": draft})

        if isinstance(draft, dict) and "request_original_objects" in draft:
            requested, reason = draft.get("request_original_objects"), draft.get("reason")
            errors, objects = [], []
            if "request_original_objects" not in allowed:
                errors.append(_problem("request_original_objects", "当前阶段不能回查，请提交完整答案"))
            elif (set(draft) != {"request_original_objects", "reason"} or not isinstance(requested, list)
                  or not requested or not all(isinstance(oid, str) for oid in requested)
                  or not isinstance(reason, str) or not reason.strip()):
                errors.append(_problem("request_original_objects", "需要非空对象编号列表和具体 reason"))
            else:
                unknown = [oid for oid in dict.fromkeys(requested) if oid not in index]
                if unknown:
                    errors.append(_problem("request_original_objects", f"未知对象 ID：{dump(unknown)}"))
                else:
                    objects = [{"object_id": oid, **index[oid]} for oid in dict.fromkeys(requested)]
                    next_context = {**context, "original_objects": objects, "validation_feedback": []}
                    if not session.synthesis_size(next_context)["fits"]:
                        errors.append(_problem("request_original_objects", "请求原文和工作集超限，请减少对象数量"))
                        objects = []
            feedback, original_objects = errors, objects
            if errors and session is final:
                formal_attempts += 1
            if errors and final_mode:
                if session is not final:
                    formal_attempts += 1
                case.rt.budget.enter_synthesis_final()
                repairing = True
            case.rt.records.save(f"{prefix}/original_request_{lookup_calls:02d}.json",
                                 {"request": draft, "objects": objects, "errors": errors})
            continue

        if isinstance(draft, dict) and "request_meta_review" in draft:
            req = draft.get("request_meta_review")
            errs = []
            if "request_meta_review" not in allowed:
                errs.append(_problem("request_meta_review", "当前不能再请求 Meta，请根据已有材料提交答案"))
            elif set(draft) != {"request_meta_review", "reason"} or not isinstance(req, dict):
                errs.append(_problem("request_meta_review", "需要 request_meta_review 对象和非空 reason"))
            else:
                fields = {"question", "checks_already_done", "expected_change", "chain_id", "evidence_refs"}
                if set(req) != fields or any(not isinstance(req.get(k), str) or not req[k].strip()
                                              for k in ("question", "checks_already_done", "expected_change")):
                    errs.append(_problem("request_meta_review", "需要完整的问题、已查内容、预期影响、链编号和证据编号"))
                cid, refs = req.get("chain_id"), req.get("evidence_refs")
                if cid is not None and (not isinstance(cid, str) or cid not in case.chains):
                    errs.append(_problem("request_meta_review.chain_id", "链编号不存在"))
                if not isinstance(refs, list) or any(not isinstance(r, str) or r not in trajectory["实际证据"] for r in refs):
                    errs.append(_problem("request_meta_review.evidence_refs", "证据编号须来自本案"))
                if not isinstance(draft.get("reason"), str) or not draft["reason"].strip():
                    errs.append(_problem("reason", "需要说明请求补查的原因"))
            if errs:
                feedback = errs
                if session is final or final_mode:
                    formal_attempts += 1
                if final_mode:
                    case.rt.budget.enter_synthesis_final()
                    repairing = True
                continue
            request = copy.deepcopy(req)
            request["reason"] = draft["reason"]
            case.rt.records.save(f"{prefix}/meta_request.json", request)
            return result("meta_review_requested", review_request=request,
                          original_answer=copy.deepcopy(last_empty), source_draft_id=last_empty_id)

        formal_attempts += 1
        selection, errors, chains = check_draft(case, draft, snapshots, case_evidence_refs=trajectory["实际证据"])
        if not errors and not selection["root_causes"]:
            last_empty = copy.deepcopy(draft)
            last_empty_id = f"synthesis-r{round_no:02d}-d{formal_attempts:02d}"
            case.rt.records.save(f"{prefix}/draft_{formal_attempts:02d}.json", last_empty)
            instructions = ("根因列表为空。请比较现有证据，能支持相对最可能的根因就提交；"
                            "缺已有原文时回查，缺关键调查时请求 Meta，说明已查内容与预期影响。"
                            if not final_mode else
                            "最终预测至少提交一个有证据支持的最可能根因，并合并已有合法链版本中有支持的传播路径与分支；无可用传播证据时允许为空并说明缺口。")
            errors = [{"code": "EMPTY_ROOT_CAUSES", "path": "root_causes", "message": instructions,
                       "allowed_actions": ["submit_answer"] if final_mode else allowed,
                       "meta_returns_left": meta_returns_left if not final_mode else 0}]
            state.report(case.rt, "synthesis_empty_root", stage="synthesis", round_no=round_no,
                         reason=str(draft.get("reason", "")))
        if not errors:
            try:
                answer, prediction = export_selected_answer(case.out, selection, case.rt.tele, chains, synthesized=True)
            except EvidenceSelectionError as exc:
                errors = [_problem(exc.path, exc)]
            except (ValueError, KeyError, TypeError) as exc:
                raise StageFailure(f"综合结果的证据复核或保存失败：{exc}") from exc
        case.rt.records.save(f"{prefix}/validation_{formal_attempts:02d}.json", {
            "valid": not errors, "errors": errors,
            "root_provenance": selection["root_provenance"] if selection is not None else []})
        if errors:
            previous, feedback = copy.deepcopy(draft), errors
            if final_mode:
                case.rt.budget.enter_synthesis_final()
                repairing = True
                state.report(case.rt, "synthesis_final_retry", stage="synthesis", attempt=formal_attempts,
                             max_attempts=formal_max, reason=dump(errors))
            continue
        case.rt.records.save(f"{prefix}/decision.json", draft)
        case.rt.records.save("synthesis/decision.json", draft)
        case.rt.records.event(session.id, "synthesis_completed", {"round_no": round_no, "attempt": formal_attempts,
                                                                   "selection": selection})
        return answer, prediction, {"status": "completed", "round_no": round_no,
                                    "formal_attempts": formal_attempts, "lookup_calls": lookup_calls,
                                    "object_count": len(index), "input_size": size}
    if not final_mode:
        return result("final_required")
    raise StageFailure(f"最终综合 {formal_max} 次仍未通过检查：{dump(feedback)}")
