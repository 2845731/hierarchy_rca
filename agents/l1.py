"""路径探索：建分支、验证下一跳、应用审核反馈并修改路径。"""

import copy
import json
import state
from state import Limit, refs_in
from .loop import Session, Invalid, reason, require, checked_symptom, evidence_scopes
from .l1_frontier import same_target
from output_contract import FAULT_KINDS


def make_chain(case, seed, number, parent=None, nodes=None, edges=None, target_symptom=None, fork_at=None):
    """每条链拥有独立 L2；同一种子的各链交由同一 L1 串行修改。"""
    owner = "l1:" + seed["seed_id"]
    cid = f"{seed['seed_id']}-C{number:02d}"
    c = {
        "chain_id": cid,
        "l1_id": owner,
        "l2_id": "l2:" + cid,
        "seed": seed,
        "parent_chain_id": parent["chain_id"] if parent else None,
        "parent_revision": parent["revision"] if parent else None,
        "revision": 0,
        "nodes": copy.deepcopy(nodes) if nodes else [seed["service"]],
        "edges": copy.deepcopy(edges) if edges else [],
        "root": None,
        "fault_diagnosis": None,
        "target_symptom": (
            target_symptom if target_symptom is not None else parent["target_symptom"]
            if parent
            else seed.get("target_symptom") or (case.symptoms[0] if len(case.symptoms) == 1 else None)
        ),
        "symptom": None,
        "status": "exploring",
        "reviews": [],
        "last_review": None,
        "reviewed_checkpoint": None,
        "submitted_checkpoint": None,
        "decisions": [],
        "reason": "",
        "feedback": None,
    }
    previous = case.walkers.get(c["parent_chain_id"])
    available = refs_in(c)
    handoff = getattr(case, "seed_graph_handoffs", {}).get(seed["seed_id"], [])
    available.update(refs_in(handoff))
    for request in seed.get("source_requests", []):
        available.update(request.get("counter_evidence_refs", []))
    if previous:
        available.update(previous.seen)
    case.chains[cid] = c
    case.families.setdefault(seed["seed_id"], []).append(cid)
    case.walkers[cid] = Session(
        case.rt,
        "l1",
        owner + ":" + cid,
        chain=cid,
        inherited=available,
        neighbors=previous.neighbors if previous else None,
        investigation=previous.investigation if previous else None,
    )
    walker = case.walkers[cid]
    if not previous:
        # 第一条链继承真实 Seed 观察；后续子链仍由父链复制会话状态。
        for observation in handoff:
            result = observation["result"]
            for edge in result.get("edges", []):
                key = tuple(sorted((edge["caller"], edge["callee"])))
                facts = walker.neighbors.setdefault(key, [])
                fact = {k: copy.deepcopy(edge.get(k)) for k in ("caller", "callee", "window", "evidence_refs")}
                if fact not in facts:
                    facts.append(fact)
            # 仅对以当前起点为中心的完整原结果使用与工具相同的缓存键。
            # 从其他中心筛选出的局部观察不进入查询缓存。
            if observation.get("complete_result"):
                key = ("query_graph", json.dumps(observation["scope"], sort_keys=True,
                                                ensure_ascii=False, allow_nan=False))
                walker.query_history.remember(key, "query_graph", observation["scope"], result,
                                              observation.get("source_agent") or "seed")
    if getattr(case.cfg, "branch_frontier_enabled", False) and hasattr(case, "branch_frontiers"):
        from .l1_frontier import BranchFrontier
        frontier = case.branch_frontiers.setdefault(seed["seed_id"], BranchFrontier())
        walker = case.walkers[cid]
        walker.branch_frontier = frontier
        walker.branch_chain = c
        if not previous:
            for observation in handoff:
                frontier.observe(None, None, observation["scope"], observation["result"],
                                 source_agent=observation.get("source_agent") or "seed",
                                 source_round=observation.get("source_round"),
                                 complete_result=observation.get("complete_result", False))
        walker.branch_frontier_view = lambda: frontier.summary(
            c, [case.chains[k] for k in case.families[seed["seed_id"]]], case.symptoms,
            walker.seen, getattr(case.cfg, "branch_frontier_summary_limit", 12))
    # Independent session per chain, NOT one reviewer shared by the whole L1.
    case.reviewers[cid] = Session(case.rt, "l2", c["l2_id"], chain=cid)
    state.save_version(case, c)
    case.rt.records.register_chain(cid, number)
    state.report(
        case.rt,
        "chain_created",
        chain=c,
        stage="l1",
        parent_chain_id=c["parent_chain_id"],
        parent_revision=c["parent_revision"],
        fork_at=(fork_at if fork_at is not None else
                 (c["nodes"][-2] if len(c["nodes"]) > 1 else c["nodes"][-1])) if parent else None,
        total_chains=len(case.rt.records.chain_numbers),
        target_service=(c["target_symptom"] or {}).get("service"),
    )
    return c


def family_task_child(case, task, question, reason_text, target_symptom=None):
    """从已提交快照的可审核前缀建立续查链；待查边由 L1 自行取证和送审。"""
    from .meta import submitted_view

    cid = task.get("source_chain_id")
    require(isinstance(cid, str) and cid in case.chains, "待办来源链不存在")
    original = case.chains[cid]
    require(original.get("status") == "submitted", "待办来源没有已提交版本，暂不能创建续查链")
    source = submitted_view(original)
    require(source.get("revision") == task.get("source_revision"), "待办来源版本已变化，需重新检查")
    seed = source["seed"]
    family = case.families[seed["seed_id"]]
    require(len(family) < case.cfg.chains_per_l1, "本候选起点的累计链数已达上限")
    path = task.get("path")
    require(isinstance(path, list) and len(path) >= 2, "待办缺少有效路径")
    at, neighbor = task.get("at"), task.get("neighbor")
    require(path[-2:] == [at, neighbor] and at in source["nodes"], "待办分支点或邻居无效")
    pos = source["nodes"].index(at)
    nodes = copy.deepcopy(source["nodes"][:pos + 1])
    require(nodes == path[:-1] and neighbor not in nodes, "待办路径与来源版本不一致")
    require(len(nodes) <= case.cfg.max_hops, "待办路径已达到最大跳数")
    review = source.get("last_review") or {}
    require(review.get("status") == "reviewed" and review.get("revision") == source["revision"],
            "来源版本没有有效审核")
    edges = copy.deepcopy(source["edges"][:pos])
    verdicts = review.get("edge_verdicts", [])
    require(len(verdicts) >= len(edges) and all(
        (verdicts[i].get("from"), verdicts[i].get("to"), verdicts[i].get("verdict"))
        == (edge.get("from"), edge.get("to"), "support") for i, edge in enumerate(edges)
    ), "待办前缀尚未全部获得 L2 支持")
    previous = case.walkers[cid]
    target = (checked_symptom(case, target_symptom, previous) if target_symptom is not None
              else copy.deepcopy(source.get("target_symptom")))
    require(target, "待办未提供可验证的目标症状，来源链也没有可继承目标")
    require(refs_in({"edges": edges, "target": target}) <= previous.seen,
            "来源前缀或目标证据不在该会话可访问范围")
    new_id = f"{seed['seed_id']}-C{len(family) + 1:02d}"
    if not case.rt.budget.reserve(new_id):
        raise Limit("预算不足以预留续查链及审核")
    try:
        child = make_chain(case, seed, len(family) + 1, parent=source,
                           nodes=nodes, edges=edges,
                           target_symptom=target, fork_at=at)
    except Exception:
        case.rt.budget.release(new_id)
        raise
    child["frontier_task_id"] = task["task_id"]
    child["frontier_task_edge"] = {"from": at, "to": neighbor}
    child["feedback"] = {"question": question, "reason": reason_text,
                         "task_id": task["task_id"],
                         "at": at, "neighbor": neighbor,
                         "observations": copy.deepcopy(task.get("observations", [])),
                         "symptom_clues": copy.deepcopy(task.get("symptom_clues", [])),
                         "instruction": "这是待查邻居，不是已确认传播；先查询邻接与异常线索，再决定是否扩链送审。"}
    return child


def checked_edge(case, raw, nodes, agent):
    """调用事实只限制邻接；故障的影响方向由 L1 提出，由 L2 单独审核。"""
    require(isinstance(raw, dict), "edge 必须是对象")
    a, b = raw.get("from"), case.rt.service(raw.get("to"))
    require(a == nodes[-1] and b not in nodes, "新增边必须从链尾到未访问过的真实服务")
    require(len(nodes) <= case.cfg.max_hops, "本链已达到最大跳数")
    observations = agent.neighbors.get(tuple(sorted((a, b))), [])
    require(observations, "先用 query_graph 查这两个服务；正常或异常窗口有真实邻接才能走这一步")
    refs = case.rt.refs(raw.get("evidence_refs"), agent.seen)
    require(set(refs) & refs_in(observations), "请引用该调用边自己的 evidence_refs，而非另一邻居或目录")
    return {
        "from": a,
        "to": b,
        "evidence_refs": refs,
        "observed_calls": copy.deepcopy(observations),
        "observed_windows": sorted({e["window"] for e in observations}),
        "evidence_scopes": evidence_scopes(case, refs),
    }


def root_claim(case, obj, session, start):
    require(isinstance(obj, dict) and obj.get("service") == start, "候选根必须是本链起点")
    require("fault_kind" in obj and (obj["fault_kind"] is None or obj["fault_kind"] in FAULT_KINDS),
            "fault_kind 必须为 null 或本轮正式枚举")
    return {
        "service": start,
        "fault_kind": obj["fault_kind"],
        "evidence_refs": case.rt.refs(obj.get("evidence_refs"), session.seen),
    }


def checked_fault_diagnosis(case, obj, session):
    """保存有限的竞争解释；候选不等于已确认故障。"""
    require(isinstance(obj, dict), "review 必须提交 fault_diagnosis 对象")
    for key in ("observed_phenomenon", "missing_evidence", "next_check"):
        require(isinstance(obj.get(key), str) and obj[key].strip(), f"fault_diagnosis.{key} 需填写文字")
    candidates = obj.get("candidates")
    require(isinstance(candidates, list) and len(candidates) <= 3, "candidates 必须是最多 3 项的列表")
    checked, seen = [], set()
    for candidate in candidates:
        require(isinstance(candidate, dict), "故障类型候选必须是对象")
        kind = candidate.get("fault_kind")
        require(isinstance(kind, str) and kind in FAULT_KINDS and kind not in seen, "候选类型须合法且不重复")
        seen.add(kind)
        require(isinstance(candidate.get("status"), str)
                and candidate["status"] in {"plausible", "needs_check"}, "候选状态须为 plausible 或 needs_check")
        require(isinstance(candidate.get("reason"), str) and candidate["reason"].strip(), "候选需要说明理由")
        refs = case.rt.refs(candidate.get("evidence_refs", []), session.seen, empty=True)
        require(candidate["status"] != "plausible" or bool(refs), "plausible 候选须引用支持线索；否则使用 needs_check")
        checked.append({"fault_kind": kind, "status": candidate["status"],
                        "reason": candidate["reason"].strip(), "evidence_refs": refs})
    return {"observed_phenomenon": obj["observed_phenomenon"].strip(), "candidates": checked,
            "missing_evidence": obj["missing_evidence"].strip(), "next_check": obj["next_check"].strip()}


def walk_step(case, c):
    """消费当前快照的直接反馈，原子地做一步取舍；不会修改其他 L1 的链。"""
    cid, family = c["chain_id"], case.families[c["seed"]["seed_id"]]
    s = case.walkers[cid]
    last, feedback = c["last_review"], c["feedback"]
    checkpoint = c.get("reviewed_checkpoint")
    current_review = bool(last and last.get("status") == "reviewed" and last.get("revision") == c["revision"])
    checkpoint_ready = bool(
        checkpoint and not current_review and len(c["reviews"]) >= case.cfg.reviews_per_chain
        and c["reviews"] and c["reviews"][-1].get("status") == "reviewed"
        and c["reviews"][-1].get("review_id") == checkpoint["review"].get("review_id")
    )
    new_refs = sorted(s.seen - set(checkpoint["review"].get("available_refs_at_review", []))) if checkpoint_ready else []
    related_services = set(c["nodes"]) | {(c.get("target_symptom") or {}).get("service")}
    related_services.update(service for pair in s.neighbors for service in pair)
    related_symptoms = [row for row in case.symptom_snapshot(s.seen) if row["service"] in related_services]
    symptom_limit = max(1, getattr(case.cfg, "branch_frontier_summary_limit", 12))
    # 同样大小的摘要先保留固定目标、链尾异常，再展示其他邻居；保留整案编号。
    related_symptoms = sorted(reversed(related_symptoms), key=lambda row: (
        not same_target(row, c.get("target_symptom")), row["service"] != c["nodes"][-1]))
    ctx = {
        "mode": "investigate",
        "chain": state.view(c),
        "symptoms": related_symptoms[:symptom_limit],
        "symptom_summary": {"related_total": len(related_symptoms),
                            "truncated": len(related_symptoms) > symptom_limit,
                            "note": "仅显示路径及已知邻居相关症状；全案清单交 Meta 处理。"},
        "last_review": last,
        "feedback": feedback,
        "sibling_chain_ids": list(family),
        "seed_and_target_samples": case.rt.preview(
            c["seed"]["evidence_refs"]
            + sorted(refs_in(c["seed"].get("source_requests", [])))
            + (c["target_symptom"] or {}).get("evidence_refs", [])
        ),
        "limits": {
            "max_hops": case.cfg.max_hops,
            "chains_per_l1": case.cfg.chains_per_l1,
            "reviews_left": case.cfg.reviews_per_chain - len(c["reviews"]),
        },
    }
    if checkpoint_ready:
        ctx["reviewed_checkpoint"] = {
            "chain": checkpoint["chain"], "review": checkpoint["review"],
            "new_evidence_refs": new_refs,
            "new_evidence_samples": case.rt.preview(new_refs),
            "instruction": "审核次数已用完。仅在新证据和补查反馈没有推翻旧版判断时，才可明确提交旧版；不能提交新版本未审核的边。",
        }
    def defer_change(action, message):
        c["review_limit_blocked"] = True
        entry = {"revision": c["revision"], "action": copy.deepcopy(action), "reason": message}
        c.setdefault("unreviewed_proposals", []).append(entry)
        case.rt.records.event(s.id, "unreviewed_change_deferred", entry)
        ctx["review_limit_notice"] = message + " 如要提交当前已审核版本，须核对新证据并在 submit 明确填写 confirm_no_contradiction:true；有反证或关键疑问则停止或补种。"
    actions = ["choose_symptom", "extend", "fork", "backtrack", "review", "submit", "reseed", "stop"]
    root_stop_confirmation_used = False
    pending_stop = None
    pending_branch_action = None
    branch_reminder_used = False
    while True:
        try:
            a = s.step(ctx, actions, slot="response" if last else None)
        except Limit as exc:
            if pending_branch_action is not None and str(exc) == "model_budget_exhausted":
                a = pending_branch_action
                pending_branch_action = None
            elif pending_stop is None or str(exc) != "model_budget_exhausted":
                raise
            else:
                # 另一个分支可能在提醒之后用掉共享额度：保留原停止，不自动提交根因。
                a = pending_stop
                a["reason"] += "；模型额度不足，未能完成根因与传播关系的再次确认"
                case.rt.records.event(s.id, "root_stop_confirmation_unfinished", {
                    "chain_id": cid, "revision": c["revision"], "reason": "model_budget_exhausted"
                })
                pending_stop = None
        try:
            why, op = reason(a), a["action"]
            new_child = None
            if (op in {"submit", "stop"} and not branch_reminder_used
                    and not s.last_final_action_only and len(family) < case.cfg.chains_per_l1
                    and getattr(s, "branch_frontier_view", None)):
                frontier_view = s.branch_frontier_view()
                pending_branches = [item for item in frontier_view["items"]
                                    if item["status"] == "pending" and item["symptom_clues"]]
                with case.rt.budget.lock:
                    can_remind = case.rt.budget._free() > 0
                if pending_branches and can_remind:
                    branch_reminder_used = True
                    pending_branch_action = copy.deepcopy(a)
                    ctx["branch_reminder"] = (
                        "仍有带症状线索的待查分支。请判断是否值得补查；若选择结束，"
                        "在 reason 说明原因。邻接和症状不等于传播成立。"
                    )
                    continue
            pending_branch_action = None
            if op == "choose_symptom":
                require(not c["target_symptom"], "本分支已有目标症状，不能为获得更好解释随意换目标")
                if len(c["reviews"]) >= case.cfg.reviews_per_chain:
                    defer_change(a, "本链审核次数已用完，不能修改目标症状后重新送审。")
                    continue
                state.change(case, c, target_symptom=checked_symptom(case, a.get("symptom"), s))
            elif op in {"extend", "fork"}:
                require(c["target_symptom"], "先 choose_symptom 指定本分支要解释的实际观测，缺证据可补查")
                nodes, edges = c["nodes"], c["edges"]
                if op == "fork":
                    require(len(family) < case.cfg.chains_per_l1, "当前 L1 的分支数量已达上限")
                    require(a.get("at") in nodes, "fork.at 必须是已走过的节点")
                    pos = nodes.index(a["at"])
                    nodes, edges = nodes[: pos + 1], edges[:pos]
                edge = checked_edge(case, a.get("edge"), nodes, s)
                # 原样交给 L2 的是 L1 的假设理由，不替换 observed_calls 中的调用事实。
                edge["hypothesis_reason"] = why
                new_nodes, new_edges = nodes + [edge["to"]], edges + [edge]
                if op == "fork":
                    target = (checked_symptom(case, a["target_symptom"], s)
                              if a.get("target_symptom") is not None else c["target_symptom"])
                    proposed_refs = refs_in({"edges": new_edges, "target_symptom": target})
                    for other_id in family:
                        other = case.chains[other_id]
                        if (other["nodes"][:len(new_nodes)] != new_nodes
                                or not same_target(other.get("target_symptom"), target)
                                or other["status"] not in {"exploring", "submitted"}):
                            continue
                        existing_refs = refs_in({"edges": other["edges"][:len(new_edges)],
                                                 "target_symptom": other.get("target_symptom")})
                        require(bool(proposed_refs - existing_refs),
                                f"{other_id} 已有相同范围、目标和路径，且无新增证据；复用已有链或选择未覆盖分支")
                    new_id = f"{c['seed']['seed_id']}-C{len(family) + 1:02d}"
                    require(case.rt.budget.reserve(new_id), "预算不足以再开分支并审核，继续原分支或结束")
                    new_child = make_chain(case, c["seed"], len(family) + 1, c, new_nodes, new_edges,
                                           target_symptom=target)
                else:
                    if len(c["reviews"]) >= case.cfg.reviews_per_chain:
                        defer_change(a, "本链审核次数已用完，新增传播边未审核且未加入当前链。")
                        continue
                    if not case.rt.budget.reserve(cid):
                        raise Limit("预算不足以修改后重新审核")
                    state.change(case, c, nodes=new_nodes, edges=new_edges, symptom=None)
            elif op == "backtrack":
                require(a.get("at") in c["nodes"][:-1], "回退目的必须在当前链尾之前")
                if len(c["reviews"]) >= case.cfg.reviews_per_chain:
                    defer_change(a, "本链审核次数已用完，回退提议未执行且未审核。")
                    continue
                if not case.rt.budget.reserve(cid):
                    raise Limit("预算不足以回退后重新审核")
                pos = c["nodes"].index(a["at"])
                state.change(case, c, nodes=c["nodes"][: pos + 1], edges=c["edges"][:pos], symptom=None)
            elif op == "review":
                case.review_dispatch(c, a, s)
            elif op == "reseed":
                service = case.rt.service(a.get("suggested_service"))
                refs = case.rt.refs(a.get("evidence_refs"), s.seen)
                calls = a.get("observed_calls", [])
                require(isinstance(calls, list), "observed_calls 必须是列表")
                checked_calls = []
                for call in calls:
                    require(isinstance(call, dict), "observed_calls 中的调用必须是对象")
                    caller = case.rt.service(call.get("caller"))
                    callee = case.rt.service(call.get("callee"))
                    window = call.get("window")
                    call_refs = case.rt.refs(call.get("evidence_refs"), s.seen)
                    known = s.neighbors.get(tuple(sorted((caller, callee))), [])
                    require(any(f.get("caller") == caller and f.get("callee") == callee
                                and f.get("window") == window
                                and set(call_refs) & set(f.get("evidence_refs", [])) for f in known),
                            "observed_calls 必须与本会话已有的调用方向、窗口及证据一致")
                    checked_calls.append({"caller": caller, "callee": callee, "window": window,
                                          "evidence_refs": call_refs})
                impact = a.get("proposed_impact")
                if impact is not None:
                    require(isinstance(impact, dict), "proposed_impact 必须是对象")
                    impact = {"from": case.rt.service(impact.get("from")),
                              "to": case.rt.service(impact.get("to"))}
                    require(impact["from"] != impact["to"], "拟议影响方向的两端不能相同")
                counter_refs = case.rt.refs(a.get("counter_evidence_refs", []), s.seen, empty=True)
                next_check = a.get("next_check", "")
                require(isinstance(next_check, str), "next_check 必须是文字")
                c["reseed_request"] = {
                    "suggested_service": service,
                    "reason": why,
                    "evidence_refs": refs,
                    "observed_calls": checked_calls,
                    "proposed_impact": impact,
                    "counter_evidence_refs": counter_refs,
                    "next_check": next_check.strip(),
                }
            elif op == "submit":
                if a.get("use_reviewed_version") is True:
                    require(checkpoint_ready, "仅当前版本未审核且本链审核次数用完时可提交已保存的旧版")
                    current_new_refs = sorted(s.seen - set(checkpoint["review"].get("available_refs_at_review", [])))
                    if current_new_refs != new_refs:
                        new_refs = current_new_refs
                        ctx["reviewed_checkpoint"]["new_evidence_refs"] = new_refs
                        ctx["reviewed_checkpoint"]["new_evidence_samples"] = case.rt.preview(new_refs)
                        ctx["review_limit_notice"] = "刚获得了新证据，请先对照旧审核重新判断，再决定是否提交旧版。"
                        continue
                    require(a.get("confirm_no_contradiction") is True,
                            "必须核对新增证据和补查问题，明确确认没有推翻所保留的旧版判断")
                    require(checkpoint["review"]["revision"] == checkpoint["chain"]["revision"], "保存的版本和审核不一致")
                    c["submitted_checkpoint"] = copy.deepcopy(checkpoint)
                else:
                    require(
                        last and last["revision"] == c["revision"] and last["status"] == "reviewed",
                        "需先取得本版本的有效 L2 审核，旧版或失败审核不能提交",
                    )
                    if c.get("review_limit_blocked"):
                        require(a.get("confirm_no_contradiction") is True,
                                "请核对被暂存的修改提议及新证据，明确确认它们没有推翻当前已审核判断")
                    c["submitted_checkpoint"] = None
            elif op == "stop":
                disposition = a.get("disposition", "insufficient_evidence")
                require(disposition in {"insufficient_evidence", "rejected", "withdrawn"}, "未知停止原因")
                if disposition == "rejected":
                    case.rt.refs(a.get("evidence_refs"), s.seen)
                if (disposition == "insufficient_evidence" and not root_stop_confirmation_used
                    and not s.last_final_action_only and c["root"] and not c["edges"]
                    and last and last.get("status") == "reviewed" and last.get("revision") == c["revision"]
                    and (last.get("component_verdict") or {}).get("verdict") == "support"):
                    root_stop_confirmation_used = True
                    pending_stop = copy.deepcopy(a)
                    reviewed_refs = set(last.get("available_refs_at_review", []))
                    ctx["root_stop_confirmation"] = {
                        "review_id": last["review_id"], "revision": c["revision"],
                        "new_evidence_refs": sorted(s.seen - reviewed_refs),
                        "new_evidence_samples": case.rt.preview(sorted(s.seen - reviewed_refs)),
                        "original_stop_reason": why,
                        "instruction": (
                            "当前无传播边，但本版本根因组件已有 L2 支持。请核对故障类型及已有异常线索："
                            "有可验证的影响关系且额度允许时，优先延伸或分支，补充影响范围本身有价值；"
                            "无可行线索或额度受限时，可 submit 有依据的单节点并说明类型及传播缺口；"
                            "根因已被动摇或无法保留时，可 stop 或 reseed。"
                            "不能仅因传播边为空而认定根因证据不足，也不能仅因已有支持就忽略反证。"
                        ),
                    }
                    case.rt.records.event(s.id, "root_stop_confirmation_requested", {
                        "chain_id": cid, "revision": c["revision"], "review_id": last["review_id"],
                        "original_stop_reason": why,
                        "new_evidence": {"evidence_refs": ctx["root_stop_confirmation"]["new_evidence_refs"]},
                    })
                    continue
            break
        except Invalid as exc:
            s.reject(exc, action=a.get("action"))
    s.accepted()
    annotations = a.get("new_symptoms", [])
    annotations = (list(annotations) if isinstance(annotations, list)
                   else [{"annotation_error": "new_symptoms 必须是列表"}])
    if op == "choose_symptom":
        annotations.append(c["target_symptom"])
    if new_child and new_child.get("target_symptom"):
        annotations.append(new_child["target_symptom"])
    registration = case.register_symptoms(annotations, s, chain=c)
    decision = {
        "chain_id": cid,
        "input_revision": ctx["chain"]["revision"],
        "output_revision": c["revision"],
        "action": op,
        "reason": why,
        "review_received": last["review_id"] if last else None,
        "meta_question": feedback["question"] if feedback else None,
        "symptom_registration": registration,
    }
    if op == "submit" and c.get("submitted_checkpoint"):
        decision["submitted_revision"] = c["submitted_checkpoint"]["chain"]["revision"]
        decision["unreviewed_revision"] = c["revision"]
        decision["new_evidence"] = {"evidence_refs": new_refs}
    if new_child:
        decision["child_chain_id"] = new_child["chain_id"]
    c["decisions"].append(decision)
    c["feedback"] = None
    case.rt.records.event(s.id, "decision_applied", decision)
    state.report(
        case.rt,
        "chain_step",
        stage="l1",
        chain=c,
        agent_id=s.id,
        action=op,
        before_nodes=ctx["chain"]["nodes"],
        after_nodes=list(c["nodes"]),
        before_revision=ctx["chain"]["revision"],
        step_no=len(c["decisions"]),
        reason=why,
        child_chain_id=new_child["chain_id"] if new_child else None,
        child_nodes=list(new_child["nodes"]) if new_child else None,
        review_id=last["review_id"] if last else None,
        recommendation=last.get("recommendation") if last else None,
    )
    if op in {"submit", "reseed", "stop"}:
        status = {
            "submit": "submitted",
            "reseed": "reseed_requested",
            "stop": a.get("disposition", "insufficient_evidence"),
        }[op]
        if op == "stop" and last and last["status"] in {
            "technical_failure", "budget_exhausted", "investigation_stopped"
        }:
            status = last["status"]
        state.finish(case, c, status, why)
    return new_child["chain_id"] if new_child else None


def prepare_review(case, c, action, agent):
    """检查 L1 提交的症状和证据，固定待审版本后将请求交给调度器。"""
    cid = c["chain_id"]
    root = root_claim(case, action["root"], agent, c["nodes"][0]) if action.get("root") is not None else None
    diagnosis = checked_fault_diagnosis(case, action.get("fault_diagnosis"), agent)
    symptom = checked_symptom(case, action["symptom"], agent) if action.get("symptom") is not None else None
    extra = case.rt.refs(action.get("evidence_refs", []), agent.seen, empty=True)
    target = c.get("target_symptom") or {}
    target_hint = (
        f"当前固定目标={target.get('service')}，链尾={c['nodes'][-1]}，"
        f"可引用目标证据={target.get('evidence_refs', [])}。"
        "symptom 须同时对应原目标和链尾并引用目标证据；否则填 null，已有传播边仍送审。"
        "其他异常用 new_symptoms 登记；独立调查其他目标时用 fork.target_symptom，不改原目标。"
    )
    require(not symptom or symptom["service"] == c["nodes"][-1], target_hint)
    require(
        not symptom
        or (
            c["target_symptom"]
            and symptom["service"] == c["target_symptom"]["service"]
            and set(symptom["evidence_refs"]) & set(c["target_symptom"]["evidence_refs"])
        ),
        target_hint,
    )
    require(len(c["reviews"]) < case.cfg.reviews_per_chain, "这条链的审核次数已经用完")
    changed = root != c["root"] or symptom != c["symptom"] or diagnosis != c.get("fault_diagnosis")
    last = c["last_review"]
    if last and not changed and last["status"] == "reviewed":
        require(
            agent.seen - set(last["available_refs_at_review"]),
            "本版本、根因和证据均未改变，不重复审核；采纳反馈或改查其他证据",
        )
    if not case.rt.budget.reserve(cid, before_walk=False):
        raise Limit("没有剩余模型额度完成本次审核及反馈处理")
    # checkpoint 只指向最近一次成功审核的版本；修改当前版本前保留对照。
    checkpoint = c.get("reviewed_checkpoint")
    previous = None
    if checkpoint and checkpoint.get("review", {}).get("status") == "reviewed":
        old_chain = checkpoint.get("chain", {})
        old_review = checkpoint["review"]
        if old_chain.get("revision") == old_review.get("revision"):
            previous = {
                "previous_revision": old_chain["revision"],
                "previous_root": copy.deepcopy(old_chain.get("root")),
                "previous_review_id": old_review.get("review_id"),
                "previous_component_verdict": copy.deepcopy(old_review.get("component_verdict")),
                "previous_fault_kind_verdict": copy.deepcopy(old_review.get("fault_kind_verdict")),
                "previous_doubts": copy.deepcopy(old_review.get("doubts", [])),
                "current_root": copy.deepcopy(root),
                "root_changed": old_chain.get("root") != root,
                "new_evidence_refs_since_last_review": sorted(
                    agent.seen - set(old_review.get("available_refs_at_review", []))
                ),
            }
    if changed:
        state.change(case, c, root=root, symptom=symptom, fault_diagnosis=diagnosis)
    request = {
        "chain_id": cid,
        "revision": c["revision"],
        "question": reason(action),
        "evidence_refs": extra,
        "previous_review_context": previous,
    }
    return request
