"""独立验证：检查指定版本的根因、每条边与连续前段。"""

import copy
import math
import state
from state import refs_in
from .loop import Invalid, reason, require, object_list, abnormal_refs


def verdict(case, obj, session):
    require(isinstance(obj, dict), "每项审核判断必须是对象")
    value = obj.get("verdict")
    require(isinstance(value, str) and value in {"support", "contradict", "unknown"},
            "verdict 只能是 support/contradict/unknown")
    return {
        "verdict": value,
        "reason": reason(obj),
        "evidence_refs": case.rt.refs(obj.get("evidence_refs"), session.seen, empty=value == "unknown"),
    }


def audit(case, c, request):
    """审核输入是本版本快照和 L1 提出的具体疑问；结果只发回此链的 L1。"""
    cid, rev = c["chain_id"], c["revision"]
    s = case.reviewers[cid]
    # A revised snapshot gets a fresh inspection, with earlier records still saved.
    s.history = []
    s.accepted()
    s.seen.update(refs_in(state.view(c)))
    s.seen.update(request["evidence_refs"])
    previous = request.get("previous_review_context")
    # 旧版引用来自本链已审核记录，不把摘要当作新的遥测证据。
    previous_refs = refs_in(previous) | set((previous or {}).get("new_evidence_refs_since_last_review", []))
    s.seen.update(previous_refs)
    facts = case.rt.preview(request["evidence_refs"] + sorted(refs_in(state.view(c))) + sorted(previous_refs))
    while True:
        a = s.step(
            {
                "mode": "audit",
                "chain": state.view(c),
                "edge_catalog": [{"edge_id": f"E{i + 1:02d}", **edge}
                                 for i, edge in enumerate(c["edges"])],
                "symptoms": case.symptom_snapshot(s.seen),
                "review_request": {k: v for k, v in request.items() if k != "previous_review_context"},
                "previous_review_context": previous,
                "evidence_samples": facts,
            },
            ["audit"],
            slot="audit",
        )
        try:
            cache = {}
            result = {
                "chain_id": cid,
                "revision": rev,
                "review_id": f"{cid}-R{len(c['reviews'])+1:02d}",
                "status": "reviewed",
                "l2_id": s.id,
                "reason": reason(a),
            }
            rows = object_list(a, "edge_verdicts")
            require(len(rows) == len(c["edges"]), "审核必须逐项覆盖本版本全部有向边")
            checked = []
            for i, (edge, row) in enumerate(zip(c["edges"], rows)):
                require(row.get("edge_id") == f"E{i + 1:02d}", "审核须按 edge_catalog 的 edge_id 顺序覆盖")
                claim = verdict(case, row, s)
                direction = row.get("assessed_direction")
                pair = [edge["from"], edge["to"]]
                require(direction == pair or direction == pair[::-1]
                        or (direction is None and claim["verdict"] == "unknown"),
                        "assessed_direction 须为实际判断的 [影响源,受影响端]；unknown 可为 null")
                mechanism = row.get("mechanism", "")
                require(isinstance(mechanism, str) and
                        (bool(mechanism.strip()) or claim["verdict"] == "unknown"), "明确判断需要简短传播机制")
                scope = row.get("scope", "unknown")
                require(isinstance(scope, str) and bool(scope.strip()), "scope 须说明操作/请求范围，未知写 unknown")
                if claim["verdict"] == "support":
                    require(
                        abnormal_refs(case, claim["evidence_refs"], cache=cache), "仅正常窗口证据不能支持本次的异常传播"
                    )
                assessed_verdict = claim["verdict"]
                mismatch = direction is not None and direction != pair
                if mismatch:
                    # A claim about the reverse direction is not support (nor automatically
                    # contradiction) for this chain edge. Never rewrite the chain here.
                    claim["verdict"] = "unknown"
                checked.append({"from": edge["from"], "to": edge["to"], **claim,
                                "edge_id": row["edge_id"], "assessed_direction": direction,
                                "assessed_verdict": assessed_verdict, "direction_mismatch": mismatch,
                                "mechanism": mechanism.strip(), "scope": scope.strip()})
            result["edge_verdicts"] = checked
            component = a.get("component_verdict")
            require(isinstance(component, dict) and component.get("service") == c["nodes"][0],
                    "component_verdict.service 必须是本链起点")
            component_v = verdict(case, component, s)
            if component_v["verdict"] == "support":
                require(abnormal_refs(case, component_v["evidence_refs"], cache=cache),
                        "支持故障来源组件需引用异常窗口证据")
            result["component_verdict"] = {"service": c["nodes"][0], **component_v}
            fault = a.get("fault_kind_verdict")
            kind = (c.get("root") or {}).get("fault_kind")
            require(isinstance(fault, dict) and "fault_kind" in fault and fault["fault_kind"] == kind,
                    "fault_kind_verdict.fault_kind 必须与本次根因类型一致；未提出时为 null")
            fault_v = verdict(case, fault, s)
            require(kind is not None or fault_v["verdict"] == "unknown",
                    "未提交具体故障类型时只能审核为 unknown")
            if fault_v["verdict"] == "support":
                require(abnormal_refs(case, fault_v["evidence_refs"], cache=cache),
                        "支持故障类型需引用异常窗口证据")
            result["fault_kind_verdict"] = {"fault_kind": kind, **fault_v}
            prefix = c["nodes"][:1]
            for edge in checked:
                if edge["verdict"] != "support":
                    break
                prefix.append(edge["to"])
            score = a.get("score")
            require(
                type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1,
                "score 必须是 0 至 1 之间的有限数值",
            )
            require(
                isinstance(a.get("doubts"), list) and all(isinstance(d, str) for d in a["doubts"]),
                "doubts 必须是文字列表",
            )
            require(a.get("recommendation") in {"keep", "query", "backtrack", "new_seed"}, "未知审核建议")
            result.update(
                supported_prefix=prefix,
                score=score,
                doubts=a["doubts"],
                recommendation=a["recommendation"],
            )
            break
        except Invalid as exc:
            s.reject(exc)
    s.accepted()
    case.rt.budget.release(cid, "audit")
    case.walkers[cid].seen.update(s.seen)
    result["available_refs_at_review"] = sorted(case.walkers[cid].seen)
    c["last_review"] = result
    c["reviews"].append(result)
    c["reviewed_checkpoint"] = {"chain": state.view(c), "review": copy.deepcopy(result)}
    case.rt.records.save(f"l1/{cid}/reviewed_checkpoint.json", c["reviewed_checkpoint"])
    case.rt.records.save(f"l2/{cid}/{result['review_id']}.json", result)
    case.rt.records.event(c["l1_id"], "review_received", {"chain_id": cid, "revision": rev, "review": result})
