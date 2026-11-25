"""同一候选起点的传播待办；观察、调查状态和 L2 结论分别保存。"""

import copy
import json


def _submitted_view(chain):
    checkpoint = chain.get("submitted_checkpoint") if chain.get("status") == "submitted" else None
    if checkpoint:
        return {**chain, **checkpoint["chain"], "status": "submitted",
                "last_review": checkpoint["review"]}
    return chain


def _target_key(target):
    target = target or {}
    fields = ("service", "operation", "anomaly_kind", "scope")
    if not target.get("operation") or not target.get("anomaly_kind"):
        fields += ("description",)
    return tuple(target.get(k) for k in fields)


def same_target(left, right):
    """共享目标比较；未知请求范围需共享观测，避免合并仅服务相同的异常。"""
    if not left or not right:
        return left == right
    if _target_key(left) != _target_key(right):
        return False
    scope = left.get("scope")
    if not scope or str(scope).strip().lower() in {"unknown", "scope_unknown"}:
        return bool(set(left.get("evidence_refs", [])) & set(right.get("evidence_refs", [])))
    return True


class BranchFrontier:
    """证据引用仅为索引；可用性始终由各个会话的 seen 决定。"""

    def __init__(self):
        self.observations = []
        self.query_records = []
        self.frontier_revision = 0
        self._ids = {}
        self._tasks = {}
        self._state_signatures = {}

    def observe(self, chain, revision, args, result, *, source_agent=None, source_round=None,
                complete_result=True):
        """保存每个查询范围的结果，空、失败与截断均不等同于反证。"""
        status = result.get("status", "invalid_result")
        query = {
            "source_chain": chain, "source_revision": revision,
            "scope": copy.deepcopy(args), "status": status,
            "truncated": bool(result.get("truncated")),
            "edge_count": len(result.get("edges", [])) if isinstance(result.get("edges"), list) else None,
        }
        if source_agent is not None:
            query.update(source_agent=source_agent, source_round=source_round,
                         complete_result=complete_result)
        if "windows" in result:
            query["windows"] = copy.deepcopy(result["windows"])
        if result.get("error"):
            query["error"] = str(result["error"])
        changed = False
        if query not in self.query_records:
            self.query_records.append(query)
            changed = True
        if status in {"ok", "no_observations"}:
            edges = result.get("edges")
            for edge in edges if isinstance(edges, list) else []:
                if not isinstance(edge, dict):
                    continue
                if not edge.get("caller") or not edge.get("callee"):
                    continue
                item = {
                    "source_chain": chain, "source_revision": revision,
                    "scope": copy.deepcopy(args), "truncated": bool(result.get("truncated")),
                    "caller": edge["caller"], "callee": edge["callee"],
                    "window": edge.get("window"),
                    "evidence_refs": list(edge.get("evidence_refs", [])),
                }
                if source_agent is not None:
                    item.update(source_agent=source_agent, source_round=source_round,
                                complete_result=complete_result)
                if item not in self.observations:
                    self.observations.append(item)
                    changed = True
        if changed:
            self.frontier_revision += 1

    def _task(self, path, target):
        key = json.dumps([path, _target_key(target)], ensure_ascii=False, separators=(",", ":"))
        if key not in self._ids:
            task_id = f"F{len(self._ids) + 1:03d}"
            self._ids[key] = task_id
            self._tasks[task_id] = {"status": None, "reason": ""}
            self.frontier_revision += 1
        return self._ids[key]

    def mark(self, task_id, status, reason=""):
        """仅更新调查进度；不能通过此入口伪造 L2 审核结论。"""
        if task_id not in self._tasks:
            raise ValueError("未知传播待办编号")
        if status not in {"pending", "in_progress", "unresolved", "deferred"}:
            raise ValueError("未知或不可手动设置的传播待办状态")
        if status in {"unresolved", "deferred"} and not str(reason).strip():
            raise ValueError("未完成的传播待办必须说明原因")
        entry = {"status": status, "reason": str(reason)}
        if self._tasks[task_id] != entry:
            self._tasks[task_id] = entry
            self.frontier_revision += 1

    def summary(self, chain, family, symptoms, seen, limit=None):
        chain = _submitted_view(chain)
        views = [_submitted_view(c) for c in family]
        nodes = chain["nodes"]
        target = chain.get("target_symptom")
        candidates = {}
        for item in self.observations:
            for at, neighbor in ((item["caller"], item["callee"]), (item["callee"], item["caller"])):
                if at not in nodes:
                    continue
                pos = nodes.index(at)
                if neighbor in nodes[:pos + 1]:
                    continue
                path = nodes[:pos + 1] + [neighbor]
                key = tuple(path)
                row = candidates.setdefault(key, {
                    "task_id": self._task(path, target),
                    "at": at, "neighbor": neighbor, "path": path,
                    "target_symptom": copy.deepcopy(target),
                    "source_chain_id": chain["chain_id"], "source_revision": chain["revision"],
                    "source_status": chain.get("status"),
                    "observations": [], "symptom_clues": [], "investigations": [],
                })
                fact = copy.deepcopy(item)
                fact["evidence_refs"] = [r for r in fact["evidence_refs"] if r in seen]
                fact["refs_available_in_session"] = bool(fact["evidence_refs"])
                if fact not in row["observations"]:
                    row["observations"].append(fact)
        for row in candidates.values():
            if row["target_symptom"]:
                row["target_symptom"]["evidence_refs"] = [
                    ref for ref in row["target_symptom"].get("evidence_refs", []) if ref in seen
                ]
            known_symptoms = list(symptoms) + [c.get("target_symptom") for c in views if c.get("target_symptom")]
            for symptom in known_symptoms:
                if symptom.get("service") == row["neighbor"]:
                    clue = copy.deepcopy(symptom)
                    clue["registered_evidence_exists"] = bool(clue.get("evidence_refs"))
                    clue["evidence_refs"] = [r for r in clue.get("evidence_refs", []) if r in seen]
                    clue.pop("evidence_scopes", None)
                    clue.pop("sources", None)
                    if clue not in row["symptom_clues"]:
                        row["symptom_clues"].append(clue)
            for other in views:
                if (other["nodes"][:len(row["path"])] != row["path"]
                        or (not same_target(other.get("target_symptom"), target)
                            and other.get("frontier_task_id") != row["task_id"])):
                    continue
                review = other.get("last_review") or {}
                reviews = []
                if review.get("status") == "reviewed" and review.get("revision") == other.get("revision"):
                    for verdict in review.get("edge_verdicts", []):
                        if (verdict.get("from"), verdict.get("to")) == (row["at"], row["neighbor"]):
                            reviews.append({
                                "review_id": review.get("review_id"), "revision": review.get("revision"),
                                "verdict": verdict.get("verdict"), "reason": verdict.get("reason"),
                                "evidence_refs": [r for r in verdict.get("evidence_refs", []) if r in seen],
                            })
                other_target = copy.deepcopy(other.get("target_symptom"))
                if other_target:
                    other_target["evidence_refs"] = [
                        ref for ref in other_target.get("evidence_refs", []) if ref in seen
                    ]
                row["investigations"].append({
                    "chain_id": other["chain_id"], "revision": other["revision"],
                    "status": other["status"], "reason": other.get("reason", ""),
                    "target_symptom": other_target, "reviews": reviews,
                })
            verdicts = [v["verdict"] for inv in row["investigations"] for v in inv["reviews"]]
            decisive = set(verdicts) & {"support", "contradict"}
            row["review_verdict"] = (next(iter(decisive)) if len(decisive) == 1
                                     else "unknown" if verdicts else None)
            stored = self._tasks[row["task_id"]]
            if decisive and len(decisive) == 1:
                row["status"] = "resolved"
            elif any(i["status"] == "exploring" for i in row["investigations"]):
                row["status"] = "in_progress"
            elif stored["status"] == "deferred":
                row["status"] = "deferred"
            elif stored["status"] in {"pending", "in_progress"}:
                # 明确重新排队不能被旧的 unknown/终止调查记录覆盖。
                row["status"] = stored["status"]
            elif row["investigations"]:
                row["status"] = "unresolved"
            else:
                row["status"] = stored["status"] or "pending"
            if stored["status"] in {"deferred", "unresolved"} and row["status"] == "pending":
                row["status"] = stored["status"]
            row["reason"] = stored["reason"]
            signature = (
                row["status"], row["review_verdict"],
                tuple(sorted((str(v.get("review_id")), str(v.get("revision")), str(v.get("verdict")))
                             for investigation in row["investigations"] for v in investigation["reviews"])),
            )
            if self._state_signatures.get(row["task_id"]) != signature:
                self._state_signatures[row["task_id"]] = signature
                self.frontier_revision += 1
        rows = sorted(candidates.values(), key=lambda x: (x["status"] != "pending", not x["symptom_clues"], x["path"]))
        shown = rows if limit is None else rows[:limit]
        queries = [q for q in self.query_records
                   if q.get("source_chain") == chain["chain_id"] or q.get("source_agent")]
        return {
            "items": shown, "total": len(rows), "truncated": len(shown) < len(rows),
            "frontier_revision": self.frontier_revision,
            "query_records": copy.deepcopy(queries[-12:]),
            "query_records_total": len(queries), "query_records_truncated": len(queries) > 12,
            "note": "已发现邻接和症状线索不是传播证据；未查、审核未知和技术失败不等于反证。",
        }

    def family_summary(self, family, symptoms, seen, limit=None):
        """汇总全部家族候选；seen 可为每条链独立的权限映射。"""
        rows = {}
        for chain in family:
            chain_seen = seen.get(chain["chain_id"], set()) if isinstance(seen, dict) else seen
            view = self.summary(chain, family, symptoms, chain_seen, None)
            for row in view["items"]:
                old = rows.get(row["task_id"])
                # 等价路径优先选择已提交且实际可引用邻接、目标证据的来源。
                def priority(item):
                    target = item.get("target_symptom")
                    return (
                        item["source_status"] == "submitted",
                        any(o.get("refs_available_in_session") for o in item["observations"]),
                        target is None or bool(target.get("evidence_refs")),
                    )
                if old is None or priority(row) > priority(old):
                    rows[row["task_id"]] = row
        ordered = sorted(rows.values(), key=lambda x: (x["status"] != "pending", not x["symptom_clues"], x["task_id"]))
        shown = ordered if limit is None else ordered[:limit]
        return {
            "frontier_revision": self.frontier_revision, "items": shown, "total": len(ordered),
            "truncated": len(shown) < len(ordered),
            "query_records": copy.deepcopy(self.query_records[-12:]),
            "query_records_total": len(self.query_records), "query_records_truncated": len(self.query_records) > 12,
            "note": "只涵盖已经发现的待办；候选邻接不是已确认传播。",
        }
