"""候选种子：发现症状、发布起点、处理补充候选。"""

import copy

from state import refs_in, report
from .loop import Invalid, reason, require, object_list, checked_symptom


def seed_round(case, requests):
    """首次及补充生成分别保存；补充轮接收实际退回原因，不能凭旧证据重复开工。"""
    n = len(case.rounds) + 1
    case.seed_agent.seed_round = n
    remaining = case.cfg.max_candidates - len(case.seeds)
    cap = min(remaining, case.cfg.initial_candidates) if n == 1 else remaining
    # Evidence returned with a reseed request is actually available to its consumer.
    case.seed_agent.seen.update(refs_in(requests))
    for request in requests:
        case.seed_agent.seen.update(request.get("counter_evidence_refs", []))
    ctx = {
        "mode": "initial" if n == 1 else "supplement",
        "round": n,
        "max_new_seeds": cap,
        "existing_seeds": case.seeds,
        "known_symptoms": case.symptoms,
        "requests": requests,
    }
    while True:
        a = case.seed_agent.step(ctx, ["propose_seeds"])
        try:
            why = reason(a)
            seeds = object_list(a, "seeds")
            if not seeds and n == 1:
                nonempty = any(t.get("row_count", 0) for t in case.rt.catalog.get("tables", {}).values())
                require(
                    not nonempty or case.seed_agent.data_queries,
                    "存在遥测记录，尚未成功调查数据；不能将未查询或工具失败写成空候选。",
                )
            require(len(seeds) <= cap, f"本轮最多允许 {cap} 个起点")
            fresh, used, graph_handoffs = [], set(), {}
            for item in seeds:
                name = case.rt.service(item.get("service"))
                refs = case.rt.refs(item.get("evidence_refs"), case.seed_agent.seen)
                older = [s for s in case.seeds if s["service"] == name]
                active = getattr(case, "active_seed_ids", set())
                require(
                    name not in used
                    and not any(s["seed_id"] in active for s in older)
                    and (not older or set(refs) - set().union(*(set(s["evidence_refs"]) for s in older))),
                    "该起点正在调查，或相同起点没有新增证据，不能重复启动",
                )
                used.add(name)
                fresh.append(
                    {
                        "seed_id": f"S{len(case.seeds)+len(fresh)+1:02d}",
                        "service": name,
                        "reason": reason(item),
                        "evidence_refs": refs,
                        "round": n,
                    }
                )
            symptoms = [checked_symptom(case, s, case.seed_agent) for s in object_list(a, "symptoms")]
            for raw, seed in zip(seeds, fresh):
                if requests:
                    available = {q["request_id"] for q in requests}
                    replied = raw.get("responds_to", list(available) if len(available) == 1 else [])
                    require(
                        isinstance(replied, list)
                        and replied
                        and all(isinstance(v, str) and v in available for v in replied),
                        "多个补查请求时，每个新种子需要 responds_to:[request_id] 说明回应哪一项",
                    )
                    seed["responds_to"] = list(dict.fromkeys(replied))
                    seed["source_requests"] = [
                        {
                            "request_id": q["request_id"],
                            "chain_id": q.get("chain_id"),
                            "revision": q.get("revision"),
                            "suggested_service": q.get("suggested_service"),
                            "reason": q.get("reason"),
                            "evidence_refs": q.get("evidence_refs", []),
                            "observed_calls": copy.deepcopy(q.get("observed_calls", [])),
                            "proposed_impact": copy.deepcopy(q.get("proposed_impact")),
                            "counter_evidence_refs": list(q.get("counter_evidence_refs", [])),
                            "next_check": q.get("next_check", ""),
                        }
                        for q in requests if q["request_id"] in seed["responds_to"]
                    ]
                idx = raw.get("target_symptom_index")
                if idx is not None:
                    require(
                        type(idx) is int and 0 <= idx < len(symptoms),
                        "target_symptom_index 不在本轮症状列表内",
                    )
                    seed["target_symptom"] = symptoms[idx]
                elif len(symptoms) == 1:
                    seed["target_symptom"] = symptoms[0]
                # 从 Seed 实际图查询中交接该起点的观察。保留原范围和来源；
                # 非本起点为中心的查询只传有关边，不将其称为本起点的完整查询。
                observations = []
                for record in case.seed_agent.query_history.queries.values():
                    result = record.get("result", {})
                    args = record.get("args", {})
                    if record.get("tool") != "query_graph" or result.get("status") not in {"ok", "no_observations"}:
                        continue
                    edges = [
                        {key: copy.deepcopy(edge.get(key)) for key in
                         ("caller", "callee", "window", "evidence_refs", "window_status")}
                        for edge in result.get("edges", [])
                        if isinstance(edge, dict) and seed["service"] in (edge.get("caller"), edge.get("callee"))
                        and edge.get("window") in {"normal", "abnormal"} and edge.get("evidence_refs")
                    ]
                    centered = args.get("component") == seed["service"]
                    if not centered and not edges:
                        continue
                    observations.append({
                        "source_agent": record.get("source_agent"),
                        "source_round": record.get("seed_round"),
                        "query_no": record.get("query_no"),
                        "scope": copy.deepcopy(args),
                        "complete_result": centered,
                        "result": copy.deepcopy(result) if centered else {
                            "status": result.get("status"),
                            "truncated": bool(result.get("truncated", False)),
                            "windows": copy.deepcopy(result.get("windows", {})),
                            "edges": edges,
                        },
                    })
                graph_handoffs[seed["seed_id"]] = observations
            break
        except Invalid as exc:
            case.seed_agent.reject(exc)
    if not hasattr(case, "seed_graph_handoffs"):
        case.seed_graph_handoffs = {}
    case.seed_graph_handoffs.update(graph_handoffs)
    case.seeds.extend(fresh)
    for req in requests:
        ids = [s["seed_id"] for s in fresh if req["request_id"] in s.get("responds_to", [])]
        case.resolve_request(req, "new_candidates" if ids else "no_new_candidate", why, ids)
    case.register_symptoms(symptoms, case.seed_agent, round_no=n)
    handoff_path = f"seed/graph_handoffs_{n:02d}.json"
    case.rt.records.save(handoff_path, {"round": n, "graph_handoffs": graph_handoffs})
    handoff_summary = [
        {
            "seed_id": seed_id,
            "query_count": len(observations),
            "edge_observation_count": sum(len(item["result"].get("edges", [])) for item in observations),
            "truncated_query_count": sum(bool(item["result"].get("truncated")) for item in observations),
        }
        for seed_id, observations in graph_handoffs.items()
    ]
    packet = {
        "round": n,
        "mode": ctx["mode"],
        "requests": requests,
        "seeds": fresh,
        "graph_handoff_index": {"path": handoff_path, "seeds": handoff_summary},
        "symptoms": symptoms,
        "reason": why,
    }
    case.rounds.append(packet)
    case.rt.records.save(f"seed/round_{n:02d}.json", packet)
    # 区分实际要求决策的条件与模型给出的候选理由；只用于进度显示，不改变调查流程。
    control = case.seed_agent.query_control()
    budget = case.rt.budget.snapshot()
    if control["decision_required"]:
        handoff_trigger = "repeat_limit"
    elif budget["tool_calls"] >= budget["max_tool_calls"]:
        handoff_trigger = "tool_budget_exhausted"
    else:
        handoff_trigger = "model_decision"
    report(
        case.rt,
        "seeds_ready",
        stage="seed",
        agent_id=case.seed_agent.id,
        round=n,
        mode=ctx["mode"],
        count=len(fresh),
        total_seeds=len(case.seeds),
        services=[s["service"] for s in fresh],
        handoff_trigger=handoff_trigger,
        candidates=[
            {
                "seed_id": s["seed_id"],
                "service": s["service"],
                "target_service": (s.get("target_symptom") or {}).get("service"),
            }
            for s in fresh
        ],
        symptoms=[{"service": v["service"], "description": v["description"]} for v in symptoms],
        reason=why,
    )
    case.seed_agent.accepted()
    return fresh
