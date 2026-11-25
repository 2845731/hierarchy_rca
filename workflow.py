"""案例调度：分派起点、传递审核和补查请求，汇总预算、产物和终止原因。"""

import copy
from pathlib import Path
from filesystem import filesystem_path
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from time import monotonic
from queue import Queue, Empty

from progress import emit

from agents import seed as seed_agent, l1, l2, meta, synthesis
from agents.l1_frontier import same_target
from agents.loop import Runtime, Session, Invalid, require, reason, NoProgress, StageFailure, status_for, text_field, checked_symptom, abnormal_refs
from state import Limit, refs_in
from tools.telemetry import EvidenceValidationError
import state


@contextmanager
def stage(rt, name, *, chain=None, **fields):
    """阶段和案例共用异常分类；没有新证据时停止调查，不报告技术失败。"""
    started = monotonic()
    state.report(rt, "stage_start", chain=chain, stage=name, **fields)
    outcome = {"status": "completed"}
    try:
        yield outcome
    except BaseException as exc:
        outcome.update(
            status="interrupted" if isinstance(exc, KeyboardInterrupt) else status_for(exc),
            error_type=type(exc).__name__,
        )
        raise
    finally:
        state.report(
            rt,
            "stage_end",
            chain=chain,
            stage=name,
            elapsed_seconds=monotonic() - started,
            **fields,
            **outcome,
        )


class Incident:

    def __init__(self, case_dir, out, model, cfg, handle):
        self.rt = Runtime(case_dir, out, model, cfg, handle)
        self.cfg, self.out = cfg, Path(out)
        self.seeds, self.symptoms, self.requests, self.rounds = [], [], [], []
        self.chains, self.walkers, self.reviewers = {}, {}, {}
        self.families = {}
        self.branch_frontiers = {}
        self.frontier_dispatches = {}
        self.active_seed_ids = set()
        self.reseed_events = None
        self.seed_attempts = 0
        self.seed_agent = Session(self.rt, "seed", "seed")
        self.meta = Session(self.rt, "meta", "meta:arbitrate")
        self.followups = []
        self.recovery_history = []
        self.investigation_stops = []
        self.finalize_only = False
        self.meta_selection_report = {}
        self.meta_conclusion = {}
        self.initial_meta_conclusion = {}
        self.synthesis_report = {}

    def register_symptoms(self, rows, session, *, chain=None, round_no=None):
        """One evidence-backed registry; malformed annotations never retry the main action."""
        accepted, errors = [], []
        if not isinstance(rows, list):
            rows, errors = [], [{"error": "new_symptoms 必须是列表"}]
        for position, raw in enumerate(rows):
            try:
                require(not isinstance(raw, dict) or not raw.get("annotation_error"),
                        str(raw.get("annotation_error")) if isinstance(raw, dict) else "无效症状")
                item = checked_symptom(self, raw, session)
                require(abnormal_refs(self, item["evidence_refs"]), "登记异常需要异常窗口证据")
            except (Invalid, EvidenceValidationError) as exc:
                errors.append({"position": position, "error": str(exc)})
                continue
            source = {"agent_id": session.id, "chain_id": chain.get("chain_id") if chain else None,
                      "revision": chain.get("revision") if chain else None, "seed_round": round_no}
            # Without a known request scope, only identical observations are merged.
            identity_fields = ("service", "operation", "anomaly_kind", "scope")
            if not item.get("operation") or not item.get("anomaly_kind"):
                identity_fields += ("description",)
            signature = tuple(item.get(k) for k in identity_fields)
            with self.rt.records.lock:
                existing = next((s for s in self.symptoms
                    if tuple(s.get(k) for k in identity_fields) == signature
                    and set(s["evidence_refs"]) & set(item["evidence_refs"])), None)
                if existing is None:
                    existing = {**item, "symptom_index": len(self.symptoms), "sources": []}
                    self.symptoms.append(existing)
                existing["evidence_refs"] = sorted(set(existing["evidence_refs"]) | set(item["evidence_refs"]))
                scopes = {s["evidence_ref"]: s for s in existing.get("evidence_scopes", [])}
                scopes.update({s["evidence_ref"]: s for s in item["evidence_scopes"]})
                existing["evidence_scopes"] = list(scopes.values())
                existing.setdefault("sources", [])
                existing.setdefault("symptom_index", self.symptoms.index(existing))
                if source not in existing["sources"]:
                    existing["sources"].append(source)
                accepted.append(existing["symptom_index"])
        if accepted:
            with self.rt.records.lock:
                self.rt.records.save("symptoms.json", self.symptoms)
        result = {"symptom_indices": accepted, "annotation_errors": errors}
        if accepted or errors:
            self.rt.records.event(session.id, "symptoms_registered", result)
        return result

    def symptom_snapshot(self, seen=None):
        with self.rt.records.lock:
            rows = copy.deepcopy(self.symptoms)
        if seen is not None:
            for row in rows:
                row["evidence_available"] = bool(set(row["evidence_refs"]) & seen)
                row["evidence_refs"] = [r for r in row["evidence_refs"] if r in seen]
                row.pop("sources", None)
                row.pop("evidence_scopes", None)
        return rows

    def record_stop(self, session, exc, *, chain=None, requests=(), family_decision=None):
        """记录局部未完成的调查；不把执行停止解释成没有故障，也不改变其他链。"""
        with self.rt.records.lock:
            stop = {
                "stop_id": f"T{len(self.investigation_stops) + 1:03d}",
                "stage": session.role,
                "agent_id": session.id,
                "status": status_for(exc),
                "reason": str(exc),
                "request_ids": [r["request_id"] for r in requests],
                "chain_id": chain["chain_id"] if chain else None,
                "revision": chain["revision"] if chain else None,
                "evidence_refs": sorted(session.seen),
                "query_control": session.query_control(),
                "resolved": False,
            }
            if isinstance(exc, EvidenceValidationError):
                stop.update(exc.details())
            if family_decision is not None:
                stop["family_decision"] = family_decision
            self.investigation_stops.append(stop)
            self.rt.records.save("investigation_stops.json", self.investigation_stops)
        self.rt.records.event(session.id, "investigation_stopped", stop)
        state.report(
            self.rt, "investigation_stopped", chain=chain, stage=session.role,
            agent_id=session.id, status=stop["status"], reason=stop["reason"],
            stop_id=stop["stop_id"], request_ids=stop["request_ids"],
        )

    def resolve_stops(self, session):
        """后续有效审核/提交解决了同一会话的停止时，保留历史但不再算未完成。"""
        with self.rt.records.lock:
            changed = False
            for stop in self.investigation_stops:
                if stop["agent_id"] == session.id and not stop["resolved"]:
                    stop["resolved"] = True
                    changed = True
            if changed:
                self.rt.records.save("investigation_stops.json", self.investigation_stops)

    def handoff_to_meta(self):
        """调查已停止时只能根据现有成果选择，不能重新派发同一项补查。"""
        state.report(
            self.rt, "meta_handoff", stage="meta", status="partial", finalize_only=True,
            submitted_count=sum(c["status"] == "submitted" for c in self.chains.values()),
            stopped_count=sum(not s["resolved"] for s in self.investigation_stops),
            reason="保留已提交且经过当前版本审核的链；未完成的问题一并交给 Meta，本次不再派发补查。",
        )

    def queue_requests(self, requests):
        """原请求有独立编号；联合补种不掩盖某一条请求实际上无人回应。"""
        for req in requests:
            if "request_id" not in req:
                req.update(request_id=f"Q{len(self.requests)+1:03d}", response={"status": "pending"})
                self.requests.append(req)
                state.report(
                    self.rt,
                    "seed_request_queued",
                    stage="seed",
                    chain=self.chains.get(req.get("chain_id")),
                    request_id=req["request_id"],
                    reason=req.get("reason"),
                    status="pending",
                )
        return requests

    def resolve_request(self, req, status, why, ids=()):
        req["response"] = {"status": status, "reason": why, "new_seed_ids": list(ids)}
        self.rt.records.save(f"seed/requests/{req['request_id']}.json", req)
        self.rt.records.event(
            req.get("chain_id") or "meta:arbitrate",
            "seed_response",
            {"request_id": req["request_id"], **req["response"]},
        )
        state.report(
            self.rt,
            "seed_request_result",
            stage="seed",
            chain=self.chains.get(req.get("chain_id")),
            request_id=req["request_id"],
            new_seed_ids=list(ids),
            status=status,
            reason=why,
        )

    def review_dispatch(self, c, action, agent):
        """核定审核次数、目标与真正新增的证据；缺数据与执行失败均原样回传。"""
        cid = c["chain_id"]
        request = l1.prepare_review(self, c, action, agent)
        self.rt.records.event(agent.id, "review_requested", request)
        number = len(c["reviews"]) + 1
        rid = f"{cid}-R{number:02d}"
        state.report(
            self.rt,
            "review_sent",
            chain=c,
            stage="l2",
            agent_id=c["l2_id"],
            review_id=rid,
            review_no=number,
            question=request["question"],
            max_reviews=self.cfg.reviews_per_chain,
        )
        audit_started = monotonic()
        try:
            l2.audit(self, c, request)
            self.resolve_stops(self.reviewers[cid])
        except (Limit, StageFailure, NoProgress, EvidenceValidationError) as exc:
            # Return a real failure to the caller, not an empty or low-scoring review.
            failed = {
                "chain_id": cid,
                "revision": c["revision"],
                "review_id": f"{cid}-R{len(c['reviews'])+1:02d}",
                "l2_id": c["l2_id"],
                "status": status_for(exc),
                "reason": str(exc),
                "score": None,
            }
            if isinstance(exc, EvidenceValidationError):
                failed.update(exc.details())
            self.rt.budget.release(cid, "audit")
            self.walkers[cid].seen.update(self.reviewers[cid].seen)
            c["last_review"] = failed
            c["reviews"].append(failed)
            self.rt.records.save(f"l2/{cid}/{failed['review_id']}.json", failed)
            self.rt.records.event(c["l1_id"], "review_received", failed)
            self.record_stop(self.reviewers[cid], exc, chain=c)
        r = c["last_review"]
        state.report(
            self.rt,
            "review_returned",
            chain=c,
            stage="l2",
            agent_id=c["l2_id"],
            review_id=r["review_id"],
            review_no=number,
            elapsed_seconds=monotonic() - audit_started,
            status=r["status"],
            score=r.get("score"),
            recommendation=r.get("recommendation"),
            supported_prefix=r.get("supported_prefix", []),
            component_verdict=(r.get("component_verdict") or {}).get("verdict"),
            fault_kind_verdict=(r.get("fault_kind_verdict") or {}).get("verdict"),
            edge_verdicts=[{k: e[k] for k in ("from", "to", "verdict")} for e in r.get("edge_verdicts", [])],
            reason=r["reason"],
        )

    def walk_family(self, first):
        with stage(self.rt, "l1", chain=first) as result:
            self._walk_family(first)
            result["branch_statuses"] = {
                k: self.chains[k]["status"] for k in self.families[first["seed"]["seed_id"]]
            }
            statuses = set(result["branch_statuses"].values())
            if len(statuses) == 1:
                result["status"] = next(iter(statuses))
            elif statuses & {"budget_exhausted", "technical_failure", "investigation_stopped"}:
                result["status"] = "partial"

    def _walk_family(self, first):
        # Different seeds execute concurrently. A single L1 serially owns all its siblings.
        pending = [first["chain_id"]]
        while True:
            if not pending:
                child = self.choose_family_task(first["seed"]["seed_id"])
                if child is None:
                    break
                pending.append(child["chain_id"])
            cid = pending.pop(0)
            c = self.chains[cid]
            try:
                child = l1.walk_step(self, c)
            except (Limit, StageFailure, NoProgress) as exc:
                state.finish(self, c, status_for(exc), str(exc))
                self.record_stop(self.walkers[cid], exc, chain=c)
                self.finish_frontier_task(c)
                self.save_branch_frontier(c)
                continue
            self.save_branch_frontier(c)
            if c["status"] == "submitted":
                self.resolve_stops(self.walkers[cid])
            elif c["status"] == "reseed_requested" and self.reseed_events is not None:
                self.reseed_events.put(self.reseed_request(c))
            if c["status"] == "exploring":
                pending.append(cid)
            else:
                self.finish_frontier_task(c)
                if c.get("frontier_task_id"):
                    self.save_branch_frontier(c)
            if child:
                pending.append(child)

    def family_frontier(self, seed_id, limit=None):
        frontier = self.branch_frontiers.get(seed_id)
        if frontier is None:
            return {"items": [], "total": 0, "truncated": False}
        family = [self.chains[cid] for cid in self.families.get(seed_id, [])]
        seen = {c["chain_id"]: self.walkers[c["chain_id"]].seen for c in family}
        return frontier.family_summary(family, self.symptoms, seen, limit=limit)

    @staticmethod
    def frontier_dispatch_key(seed_id, task):
        return (seed_id, task["task_id"])

    def start_frontier_task(self, seed_id, task, question, why, target_symptom=None):
        frontier = self.branch_frontiers[seed_id]
        key = self.frontier_dispatch_key(seed_id, task)
        evidence = refs_in({"observations": task.get("observations", []),
                            "symptom_clues": task.get("symptom_clues", [])})
        previous = self.frontier_dispatches.get(key)
        require(previous is None or bool(evidence - previous), "该待办没有新增相关证据，不重复派发")
        child = l1.family_task_child(self, task, question, why, target_symptom=target_symptom)
        self.frontier_dispatches[key] = (previous or set()) | evidence
        frontier.mark(task["task_id"], "in_progress", why)
        self.rt.records.event(child["l1_id"], "family_task_dispatched", {
            "seed_id": seed_id, "task_id": task["task_id"], "chain_id": child["chain_id"],
            "source_chain_id": task["source_chain_id"], "source_revision": task["source_revision"],
            "question": question, "reason": why,
        })
        return child

    def finish_frontier_task(self, chain):
        task_id = chain.get("frontier_task_id")
        frontier = self.branch_frontiers.get(chain["seed"]["seed_id"])
        if not task_id or frontier is None:
            return
        view = meta.submitted_view(chain)
        review = view.get("last_review") or {}
        edge = chain.get("frontier_task_edge") or {}
        matches = [v for v in review.get("edge_verdicts", [])
                   if (v.get("from"), v.get("to")) == (edge.get("from"), edge.get("to"))]
        if (review.get("status") == "reviewed" and review.get("revision") == view.get("revision")
                and matches and matches[-1].get("verdict") in {"support", "contradict"}):
            # The frontier derives resolved and its verdict only from L2's saved review.
            frontier.mark(task_id, "unresolved", "待办已审核，具体结论见对应 L2 记录")
        elif chain.get("status") in {"budget_exhausted", "technical_failure", "investigation_stopped"}:
            frontier.mark(task_id, "deferred", chain.get("reason") or "调查因预算或技术原因未完成")
        else:
            frontier.mark(task_id, "unresolved", chain.get("reason") or "调查结束但待办传播关系未获明确审核")

    def choose_family_task(self, seed_id):
        frontier = self.branch_frontiers.get(seed_id)
        if not frontier:
            return None
        summary = self.family_frontier(seed_id)
        pending = [x for x in summary["items"] if x["status"] == "pending"]
        if not pending:
            return None
        family = self.families[seed_id]
        if len(family) >= self.cfg.chains_per_l1:
            for item in pending:
                frontier.mark(item["task_id"], "deferred", "本候选起点累计链数已达上限")
            self.save_branch_frontier(self.chains[family[-1]])
            return None
        candidates = [x for x in pending if x.get("source_status") == "submitted"]
        if not candidates:
            for item in pending:
                frontier.mark(item["task_id"], "deferred", "缺少已提交来源版本或可优先调查的症状线索")
            self.save_branch_frontier(self.chains[family[-1]])
            return None
        # The source's own session is reused only for this decision; the submitted chain remains frozen.
        anchor = self.chains[candidates[0]["source_chain_id"]]
        session = self.walkers[anchor["chain_id"]]
        page_limit = max(1, getattr(self.cfg, "branch_frontier_summary_limit", 12))
        visible = [item for item in candidates if item["source_chain_id"] == anchor["chain_id"]][:page_limit]
        def visible_to_session(value):
            if isinstance(value, list):
                return [visible_to_session(v) for v in value]
            if not isinstance(value, dict):
                return value
            result = {k: ([r for r in v if r in session.seen] if k == "evidence_refs" and isinstance(v, list)
                          else visible_to_session(v)) for k, v in value.items()}
            if "refs_available_in_session" in result:
                result["refs_available_in_session"] = bool(result.get("evidence_refs"))
            return result
        context = {"mode": "family_review", "chain": state.view(meta.submitted_view(anchor)),
                   "family_coverage": {"items": visible_to_session(visible), "total": len(candidates),
                                        "truncated": len(visible) < len(candidates),
                                        "frontier_revision": summary.get("frontier_revision"),
                                        "page_instruction": "finish_family 仅处理本页；还有未展示任务时会继续展示下一页。"},
                   "limits": {"chains_per_l1": self.cfg.chains_per_l1,
                              "chains_used": len(family), "max_hops": self.cfg.max_hops}}
        old_tools = session.allowed_tools
        session.allowed_tools = {}
        def next_page():
            nonlocal anchor, session, old_tools, visible
            session.allowed_tools = old_tools
            anchor = self.chains[candidates[0]["source_chain_id"]]
            session = self.walkers[anchor["chain_id"]]
            old_tools = session.allowed_tools
            session.allowed_tools = {}
            visible = [item for item in candidates if item["source_chain_id"] == anchor["chain_id"]][:page_limit]
            context["chain"] = state.view(meta.submitted_view(anchor))
            context["family_coverage"].update(
                items=visible_to_session(visible), total=len(candidates),
                truncated=len(visible) < len(candidates), frontier_revision=frontier.frontier_revision,
            )
        try:
            while True:
                action = session.step(context, ["continue_family", "finish_family"])
                try:
                    why = reason(action)
                    if action["action"] == "finish_family":
                        reasons = action.get("deferred_tasks", [])
                        require(isinstance(reasons, list) and all(
                            isinstance(x, dict) and isinstance(x.get("task_id"), str)
                            and isinstance(x.get("reason"), str) and x["reason"].strip() for x in reasons),
                            "deferred_tasks 需包含任务编号和原因")
                        by_id = {x["task_id"]: x["reason"] for x in reasons}
                        require(len(by_id) == len(reasons) and set(by_id) <= {x["task_id"] for x in visible},
                                "暂缓任务必须来自本轮展示且不可重复")
                        require({x["task_id"] for x in visible} <= set(by_id), "请逐项说明已展示待办的结束原因")
                        # Only the displayed tasks were reviewed by the model. Keep
                        # undisplayed tasks pending and present the next page.
                        for item in visible:
                            frontier.mark(item["task_id"], "deferred", by_id[item["task_id"]])
                        reviewed_ids = set(by_id)
                        pending = [item for item in pending if item["task_id"] not in reviewed_ids]
                        candidates = [item for item in candidates if item["task_id"] not in reviewed_ids]
                        self.rt.records.event(session.id, "family_page_reviewed", {
                            "seed_id": seed_id, "reason": why,
                            "reviewed_task_ids": sorted(reviewed_ids),
                            "remaining_candidate_ids": [item["task_id"] for item in candidates],
                        })
                        self.save_branch_frontier(anchor)
                        session.accepted()
                        if not candidates:
                            # These tasks had no submitted source, so the model did
                            # not inspect them. Record the actual dispatch limitation.
                            for item in pending:
                                frontier.mark(item["task_id"], "deferred", "缺少已提交来源版本，未能逐项审核")
                            self.rt.records.event(session.id, "family_finished", {
                                "seed_id": seed_id, "reason": why,
                                "unreviewed_task_ids": [item["task_id"] for item in pending],
                            })
                            self.save_branch_frontier(anchor)
                            return None
                        next_page()
                        continue
                    task_id = action.get("task_id")
                    task = next((x for x in visible if x["task_id"] == task_id), None)
                    require(task is not None, "请选择本轮展示的待办，合法 task_id："
                            + ", ".join(item["task_id"] for item in visible))
                    question = text_field(action, "next_check")
                    try:
                        child = self.start_frontier_task(seed_id, task, question, why,
                                                         target_symptom=action.get("target_symptom"))
                    except Invalid as exc:
                        frontier.mark(task_id, "deferred", "当前无法安全创建续查链：" + str(exc))
                        pending = [x for x in pending if x["task_id"] != task_id]
                        candidates = [x for x in candidates if x["task_id"] != task_id]
                        self.save_branch_frontier(anchor)
                        if not candidates:
                            for item in pending:
                                frontier.mark(item["task_id"], "deferred", "没有可安全继续的已提交来源链")
                            self.save_branch_frontier(anchor)
                            session.accepted()
                            return None
                        session.reject(exc, action=action.get("action"))
                        next_page()
                        continue
                    self.save_branch_frontier(anchor)
                    session.accepted()
                    return child
                except Invalid as exc:
                    session.reject(exc, action=action.get("action"))
        except (Limit, StageFailure, NoProgress, EvidenceValidationError) as exc:
            # A failed decision is not a disposition for the remaining tasks.
            # Leave them pending so Meta can inspect and dispatch them.
            interruption = {
                "seed_id": seed_id, "error_type": type(exc).__name__, "reason": str(exc),
                "visible_task_ids": [item["task_id"] for item in visible],
                "remaining_task_ids": [item["task_id"] for item in pending],
            }
            self.rt.records.event(session.id, "family_decision_interrupted", interruption)
            self.record_stop(session, exc, chain=anchor, family_decision=interruption)
            self.save_branch_frontier(anchor)
            return None
        finally:
            session.allowed_tools = old_tools

    def save_branch_frontier(self, c):
        """保存该种子下各路径的完整待查清单；状态和审核引用保留来源。"""
        seed_id = c["seed"]["seed_id"]
        frontier = self.branch_frontiers.get(seed_id)
        if frontier is None:
            return
        family = [self.chains[k] for k in self.families[seed_id]]
        views = []
        for chain in family:
            session = self.walkers[chain["chain_id"]]
            views.append({"chain_id": chain["chain_id"], "revision": chain["revision"],
                          "chain_status": chain["status"], "closure_reason": chain.get("reason", ""),
                          "frontier": frontier.summary(chain, family, self.symptoms, session.seen)})
        self.rt.records.save(f"l1/{seed_id}_branch_frontier.json", {
            "seed_id": seed_id, "observations": frontier.observations,
            "query_records": copy.deepcopy(frontier.query_records),
            "frontier_revision": frontier.frontier_revision,
            "family_coverage": self.family_frontier(seed_id), "chains": views,
        })

    def reseed_request(self, c):
        """固定来源链和 L1 明确选择的证据，不把整段调查顺序当作传播方向。"""
        return {
            "chain_id": c["chain_id"],
            "revision": c["revision"],
            "seed": c["seed"],
            "nodes": list(c["nodes"]),
            "last_review": c["last_review"],
            **c["reseed_request"],
        }

    def investigate(self, requests=None):
        """主线程串行处理 Seed 与请求；各 L1 家族并行并及时报告补种。"""
        pending = self.queue_requests(requests or [])
        events = Queue()
        self.reseed_events = events

        def seed_once(batch):
            n = len(self.rounds)
            self.seed_attempts += 1
            self.seed_agent.reseed_budget = n > 0
            try:
                with stage(
                    self.rt, "seed", round=n + 1,
                    mode="initial" if n == 0 else "supplement",
                    request_ids=[q["request_id"] for q in batch],
                ) as result:
                    fresh = seed_agent.seed_round(self, batch)
                    result["candidate_count"] = len(fresh)
                    if not fresh:
                        result["status"] = "no_new_candidate" if batch else "insufficient_evidence"
                    return fresh
            except (Limit, StageFailure, NoProgress, EvidenceValidationError) as exc:
                self.record_stop(self.seed_agent, exc, requests=batch)
                for req in batch:
                    self.resolve_request(req, status_for(exc), "本轮候选调查未完成：" + str(exc))
                if n == 0:
                    self.finalize_only = True
                return []
            finally:
                self.seed_agent.reseed_budget = False

        def start(pool, jobs, seeds):
            for seed in seeds:
                if seed["round"] > 1:
                    self.rt.budget.register_reseed(seed["seed_id"])
                c = l1.make_chain(self, seed, 1)
                if self.rt.budget.reserve(c["chain_id"]):
                    self.active_seed_ids.add(seed["seed_id"])
                    jobs[pool.submit(self.walk_family, c)] = seed["seed_id"]
                else:
                    state.finish(self, c, "budget_exhausted", "无法预留调查、审核和反馈所需额度")

        waiting_handoffs = {}
        handoff_jobs = {}

        def resume_handoff(seed_id, pool, jobs):
            queued = waiting_handoffs.pop(seed_id, [])
            if not queued:
                return
            family = [self.chains[cid] for cid in self.families.get(seed_id, [])]
            chain = next((item for item in reversed(family) if item["status"] == "submitted"), None)
            if chain is None or self.finalize_only:
                for request in queued:
                    self.resolve_request(request, "not_dispatched",
                                         "同名调查已结束，缺少可安全续查的已提交链或已进入最终选择")
                return
            walker = self.walkers[chain["chain_id"]]
            actionable = []
            known = set(walker.seen)
            for request in queued:
                refs = set(request.get("evidence_refs", []))
                refs.update(request.get("counter_evidence_refs", []))
                refs.update(refs_in(request.get("observed_calls", [])))
                if refs - known:
                    actionable.append(request)
                    known.update(refs)
                else:
                    self.resolve_request(request, "not_dispatched",
                                         "同名调查已拥有这些证据，没有新增证据可交接")
            if actionable and len(family) >= self.cfg.chains_per_l1:
                for request in actionable:
                    self.resolve_request(request, "not_dispatched", "同名候选累计链数已达上限，无法新建续查链")
                return
            new_id = f"{seed_id}-C{len(family) + 1:02d}"
            if actionable and not self.rt.budget.reserve(new_id):
                for request in actionable:
                    self.resolve_request(request, "not_dispatched", "预算不足，无法安全续查同名候选")
            elif actionable:
                source = meta.submitted_view(chain)
                try:
                    child = l1.make_chain(
                        self, source["seed"], len(family) + 1, parent=source,
                        nodes=[source["seed"]["service"]], edges=[],
                        target_symptom=copy.deepcopy(source.get("target_symptom")),
                    )
                except Exception:
                    self.rt.budget.release(new_id)
                    raise
                child_session = self.walkers[child["chain_id"]]
                child_session.seen.update(known)
                for request in actionable:
                    calls = request.get("observed_calls", [])
                    if request.get("chain_id") not in self.chains or not calls:
                        continue
                    for call in calls:
                        pair = tuple(sorted((call["caller"], call["callee"])))
                        facts = child_session.neighbors.setdefault(pair, [])
                        if call not in facts:
                            facts.append(copy.deepcopy(call))
                    frontier = self.branch_frontiers.get(seed_id)
                    if frontier is not None:
                        frontier.observe(
                            request["chain_id"], request.get("revision"),
                            {"component": source["seed"]["service"], "origin": "reseed_request",
                             "request_id": request["request_id"], "coverage": "unknown"},
                            {"status": "ok", "truncated": True, "edges": copy.deepcopy(calls)},
                            source_agent=self.walkers[request["chain_id"]].id, complete_result=False,
                        )
                child["feedback"] = {
                    "target_type": "l1",
                    "question": "核对其他调查提供的新增证据，判断该候选及影响方向是否需要修正。",
                    "reason": "同名候选的补种请求转交已有调查。",
                    "requests": copy.deepcopy(actionable),
                }
                for request in actionable:
                    self.resolve_request(request, "pending", "同名候选已结束原调查，新增证据已交给独立续查链")
                self.active_seed_ids.add(seed_id)
                new_future = pool.submit(self.walk_family, child)
                jobs[new_future] = seed_id
                handoff_jobs[new_future] = (child, actionable)

        try:
            with ThreadPoolExecutor(max_workers=self.cfg.l1_workers, thread_name_prefix="l1") as pool:
                jobs = {}
                if not self.rounds and not pending:
                    start(pool, jobs, seed_once([]))
                while jobs or pending or not events.empty():
                    for future in list(jobs):
                        if future.done():
                            seed_id = jobs.pop(future)
                            self.active_seed_ids.discard(seed_id)
                            future.result()
                            handoff = handoff_jobs.pop(future, None)
                            if handoff:
                                chain, handed_requests = handoff
                                for request in handed_requests:
                                    if chain["status"] == "submitted":
                                        self.resolve_request(request, "handoff_completed",
                                                             "同名候选已接收证据并完成后续调查")
                                    else:
                                        self.resolve_request(request, "failed",
                                                             "同名候选已接收证据，但后续调查未提交：" + chain.get("reason", ""))
                            resume_handoff(seed_id, pool, jobs)
                    while True:
                        try:
                            pending.extend(self.queue_requests([events.get_nowait()]))
                        except Empty:
                            break
                    if pending:
                        batch, pending = pending, []
                        dispatch = []
                        for req in batch:
                            existing = [
                                s["seed_id"] for s in self.seeds
                                if s["service"] == req.get("suggested_service")
                                and s["seed_id"] in self.active_seed_ids
                            ]
                            if existing:
                                req["existing_seed_ids"] = existing
                                waiting_handoffs.setdefault(existing[0], []).append(req)
                                self.resolve_request(req, "pending",
                                                     "候选正在调查，等待家族结束后安全交接新增证据")
                            elif req.get("chain_id") and any(
                                s["service"] == req.get("suggested_service")
                                and any(self.chains[cid]["status"] == "submitted"
                                        for cid in self.families.get(s["seed_id"], []))
                                for s in self.seeds
                            ):
                                matched = next(s["seed_id"] for s in reversed(self.seeds)
                                               if s["service"] == req.get("suggested_service")
                                               and any(self.chains[cid]["status"] == "submitted"
                                                       for cid in self.families.get(s["seed_id"], [])))
                                req["existing_seed_ids"] = [matched]
                                waiting_handoffs.setdefault(matched, []).append(req)
                                resume_handoff(matched, pool, jobs)
                            elif self.finalize_only or self.seed_attempts >= 1 + self.cfg.seed_rounds or len(self.seeds) >= self.cfg.max_candidates:
                                self.resolve_request(req, "not_dispatched", "补种轮次、候选上限或全局停止条件限制了本次补查")
                            else:
                                dispatch.append(req)
                        if dispatch:
                            start(pool, jobs, seed_once(dispatch))
                        continue
                    if jobs:
                        try:
                            pending.extend(self.queue_requests([events.get(timeout=0.1)]))
                        except Empty:
                            pass
        finally:
            self.reseed_events = None
            self.rt.budget.release_reseed()

    def ordered(self):
        return [self.chains[k] for k in sorted(self.chains)]

    def followup(self, a, *, meta_calls_to_keep=None, recovery_no=None):
        """只把跨链疑问发给实际负责调查的 L1 或候选层，并记录对方返回的结果。"""
        require(not self.finalize_only, "已进入最终选择，不再派发补查；请根据已有审核结果选择")
        kind = a.get("target_type", "l1")
        require(kind in {"seed", "l1"}, "followup.target_type 只能是 seed 或 l1")
        cid = a.get("chain_id")
        question = text_field(a, "question")
        ticket = {"target_type": kind, "chain_id": cid, "question": question, "reason": reason(a)}
        target = None
        if a.get("target_symptom_index") is not None:
            index = a["target_symptom_index"]
            require(kind == "l1" and type(index) is int and 0 <= index < len(self.symptoms),
                    "target_symptom_index 须填写展示对象的整案编号，不是页内位置或 Seed 本轮下标；"
                    f"合法编号与服务：{[(i, row['service']) for i, row in enumerate(self.symptoms)]}")
            target = checked_symptom(self, self.symptom_snapshot()[index], self.meta)
            ticket["target_symptom_index"] = index
        if recovery_no is not None:
            require(sum(t.get("recovery_no") == recovery_no for t in self.followups)
                    < self.cfg.synthesis_recovery_followups_per_round, "本次回流补查次数已用完")
            ticket["recovery_no"] = recovery_no
        else:
            require(sum(not t.get("recovery_no") for t in self.followups) < self.cfg.meta_followups,
                    "Meta 补查次数已用完")
        if kind == "l1":
            require(isinstance(cid, str) and cid in self.chains, "请指定实际链编号")
        else:
            require(
                self.seed_attempts < 1 + self.cfg.seed_rounds and len(self.seeds) < self.cfg.max_candidates,
                "候选补充轮次或总数已用完",
            )
        if a.get("task_id") is None:
            previous = [t for t in self.followups
                        if (t["target_type"], t["chain_id"], " ".join(t["question"].split()),
                            t.get("target_symptom_index"))
                        == (kind, cid, " ".join(question.split()), ticket.get("target_symptom_index"))]
            if kind == "l1":
                ticket["target_revision"] = meta.submitted_view(self.chains[cid])["revision"]
                ticket["evidence_refs"] = self.rt.refs(a.get("evidence_refs", []), self.meta.seen, empty=True)
                if target is not None:
                    ticket["evidence_refs"] = sorted(set(ticket["evidence_refs"]) | set(target["evidence_refs"]))
                source = meta.submitted_view(self.chains[cid])
                planned_nodes = [source["nodes"][0]] if target is not None else source["nodes"]
                planned_target = target if target is not None else source.get("target_symptom")
                # 只拦截同一具体检查的并行重复；不同问题保留，不做自然语言相似度判别。
                for other_id in self.families[source["seed"]["seed_id"]]:
                    other = self.chains[other_id]
                    feedback = other.get("feedback") or {}
                    require(not (other_id != cid and other["status"] == "exploring"
                                 and other["nodes"][:len(planned_nodes)] == planned_nodes
                                 and same_target(other.get("target_symptom"), planned_target)
                                 and " ".join(feedback.get("question", "").split()) == " ".join(question.split())
                                 and not (set(ticket["evidence_refs"]) - self.walkers[other_id].seen)),
                            f"相同路径、目标和具体检查已由 {other_id} 调查中，请复用任务或选择未覆盖问题")
                same_revision = [t for t in previous if t.get("target_revision") == ticket["target_revision"]]
                used_refs = set().union(*(set(t.get("evidence_refs", [])) for t in same_revision))
                require(not same_revision or bool(set(ticket["evidence_refs"]) - used_refs),
                        "同一链版本的相同问题已交办；需有新的相关证据才可再次派发")
            else:
                require(not previous, "同一补查问题已交办，不重复派发")
        require(self.rt.budget.allow_followup(meta_calls_to_keep), "没有足够额度补查并保留仲裁结案和最终综合，请结束调查")
        task_id = a.get("task_id")
        task_child = None
        if task_id is not None:
            require(kind == "l1" and isinstance(task_id, str) and task_id.strip(),
                    "传播待办补查必须指定 L1 来源链和有效 task_id")
            seed_id = self.chains[cid]["seed"]["seed_id"]
            task = next((x for x in self.family_frontier(seed_id)["items"]
                         if x["task_id"] == task_id), None)
            require(task is not None and task.get("source_chain_id") == cid, "待办与来源链不匹配或不存在")
            require(task.get("status") in {"pending", "unresolved", "deferred"}, "待办已解决或正在调查")
            try:
                if target is not None:
                    self.walkers[cid].seen.update(target["evidence_refs"])
                    self.rt.records.event(self.meta.id, "target_evidence_handoff", {
                        "chain_id": cid, "symptom_index": ticket["target_symptom_index"],
                        "evidence_refs": target["evidence_refs"]})
                task_child = self.start_frontier_task(seed_id, task, question, ticket["reason"], target_symptom=target)
            except Limit as exc:
                raise Invalid(str(exc)) from exc
            ticket.update(task_id=task_id, source_revision=task["source_revision"],
                          dispatched_chain_id=task_child["chain_id"])
        self.followups.append(ticket)
        self.rt.records.event(self.meta.id, "followup_dispatched", ticket)
        state.report(
            self.rt,
            "meta_followup",
            stage="meta",
            agent_id=self.meta.id,
            target_type=kind,
            chain_id=cid,
            question=question,
            reason=ticket["reason"],
            followup_no=len(self.followups),
        )
        if kind == "seed":
            target_chain = self.chains.get(cid) if isinstance(cid, str) else None
            ticket["evidence_refs"] = sorted(
                refs_in(state.view(target_chain)) if target_chain else self.meta.seen
            )
            self.investigate([ticket])
            ticket["new_seeds"] = ticket["response"]["new_seed_ids"]
        elif task_child is not None:
            followup_events = Queue()
            self.reseed_events = followup_events
            try:
                self.walk_family(task_child)
            finally:
                self.reseed_events = None
            ticket["returned_status"] = task_child["status"]
            ticket["returned_revision"] = task_child["revision"]
            followup_requests = []
            while not followup_events.empty():
                followup_requests.append(followup_events.get_nowait())
            if followup_requests:
                self.investigate(followup_requests)
        else:
            source_chain = self.chains[cid]
            c = source_chain
            followup_events = Queue()
            reserved = False
            if source_chain["status"] == "submitted" or target is not None:
                source = meta.submitted_view(source_chain)
                family = self.families[source["seed"]["seed_id"]]
                new_id = f"{source['seed']['seed_id']}-C{len(family) + 1:02d}"
                if len(family) >= self.cfg.chains_per_l1:
                    ticket["returned_status"] = "not_dispatched"
                    ticket["returned_reason"] = "候选起点累计链数已达上限"
                elif not self.rt.budget.reserve(new_id):
                    ticket["returned_status"] = "budget_exhausted"
                    ticket["returned_reason"] = "预算不足以新建续查链并审核"
                else:
                    try:
                        c = l1.make_chain(
                            self, source["seed"], len(family) + 1, parent=source,
                            nodes=[source["nodes"][0]] if target is not None else source["nodes"],
                            edges=[] if target is not None else source["edges"],
                            target_symptom=target if target is not None else copy.deepcopy(source.get("target_symptom")),
                        )
                    except Exception:
                        self.rt.budget.release(new_id)
                        raise
                    self.walkers[c["chain_id"]].seen.update(ticket.get("evidence_refs", []))
                    c["feedback"] = ticket.copy()
                    ticket["dispatched_chain_id"] = c["chain_id"]
                    reserved = True
            elif self.rt.budget.reserve(cid):
                self.walkers[cid].seen.update(ticket.get("evidence_refs", []))
                c.update(status="exploring", feedback=ticket.copy(), submitted_checkpoint=None)
                reserved = True
            else:
                ticket["returned_status"] = "budget_exhausted"
                ticket["returned_reason"] = "预算不足以继续调查并审核"
            if reserved:
                self.reseed_events = followup_events
                try:
                    self.walk_family(c)
                finally:
                    self.reseed_events = None
                ticket["returned_status"] = c["status"]
                ticket["returned_revision"] = c["revision"]
            followup_requests = []
            while not followup_events.empty():
                followup_requests.append(followup_events.get_nowait())
            if followup_requests:
                self.investigate(followup_requests)
        self.rt.records.save(f"meta/followup_{len(self.followups):02d}.json", ticket)
        self.rt.records.event(self.meta.id, "followup_returned", ticket)
        state.report(
            self.rt,
            "meta_followup_result",
            stage="meta",
            target_type=kind,
            chain_id=cid,
            followup_no=len(self.followups),
            new_seed_ids=ticket.get("new_seeds", []),
            revision=ticket.get("returned_revision"),
            status=ticket.get("returned_status") or ticket.get("response", {}).get("status"),
        )

    def counts(self):
        cs = self.ordered()
        return {
            "initial_candidates": len(self.rounds[0]["seeds"]) if self.rounds else 0,
            "supplemental_candidates": sum(len(r["seeds"]) for r in self.rounds[1:]),
            "seed_rounds": len(self.rounds),
            "seed_attempts": self.seed_attempts,
            "supplement_requests": len(self.requests),
            "chains": len(cs),
            "submitted": sum(c["status"] == "submitted" for c in cs),
            "rejected": sum(c["status"] == "rejected" for c in cs),
            "withdrawn": sum(c["status"] == "withdrawn" for c in cs),
            "reseed_requested": sum(c["status"] == "reseed_requested" for c in cs),
            "insufficient_evidence": sum(c["status"] == "insufficient_evidence" for c in cs),
            "budget_exhausted": sum(c["status"] == "budget_exhausted" for c in cs),
            "technical_failures": sum(c["status"] == "technical_failure" for c in cs),
            "investigation_stopped": sum(not s["resolved"] for s in self.investigation_stops),
            "l2_reviews": sum(len(c["reviews"]) for c in cs),
            "meta_followups": len(self.followups),
        }

    def backtracking_totals(self):
        """仅作进度摘要。建议按成功返回的审核计，实际回退按已经应用的 L1 决定计。"""
        chains = self.chains.values()
        return {
            "backtracks": sum(
                decision["action"] == "backtrack" for chain in chains for decision in chain["decisions"]
            ),
            "backtrack_advice": sum(
                review["status"] == "reviewed" and review.get("recommendation") == "backtrack"
                for chain in chains
                for review in chain["reviews"]
            ),
        }

    def run(self):
        answer = prediction = None
        failure = {}
        meta_failure = {}
        all_failed = False
        active_stage = "investigation"
        try:
            self.investigate()
            active_stage = "meta"
            try:
                with stage(self.rt, "meta", agent_id=self.meta.id):
                    conclusion = meta.arbitrate(self)
            except (Limit, StageFailure, NoProgress, EvidenceValidationError) as exc:
                meta_failure = {"status": status_for(exc), "reason": str(exc), "error_type": type(exc).__name__}
                if isinstance(exc, EvidenceValidationError):
                    meta_failure.update(exc.details())
                conclusion = {"status": "未完成", "failure": meta_failure, "termination_reason": str(exc)}
                self.meta_conclusion = conclusion
                self.rt.records.event(self.meta.id, "arbitration_failed", conclusion)
            self.initial_meta_conclusion = copy.deepcopy(conclusion)
            # 每次回流在同一个 Incident 内追加调查，随后重建全案综合工作集。
            returns = 0
            round_no = 1
            carryover = None
            final_mode = bool(self.finalize_only or not self.rt.budget.can_recovery())
            while True:
                active_stage = "synthesis"
                if final_mode:
                    state.report(self.rt, "synthesis_final_mode", stage="synthesis",
                                 reason="补查额度或可用预算限制，使用全部已有材料作最终预测")
                with stage(self.rt, "synthesis", agent_id="synthesis:final"):
                    answer, prediction, self.synthesis_report = synthesis.synthesize(
                        self, conclusion, round_no=round_no,
                        meta_returns_left=0 if final_mode else self.cfg.synthesis_max_meta_returns - returns,
                        recovery_history=self.recovery_history, final_mode=final_mode, carryover=carryover)
                if self.synthesis_report.get("status") == "final_required":
                    if final_mode:
                        raise StageFailure("最终综合无法继续：模型预算不足或阶段状态无效")
                    carryover = self.synthesis_report.get("carryover")
                    round_no += 1
                    final_mode = True
                    continue
                if self.synthesis_report.get("status") != "meta_review_requested":
                    break
                if final_mode or returns >= self.cfg.synthesis_max_meta_returns:
                    raise StageFailure("最终综合不允许继续请求 Meta 补查")
                returns += 1
                original = copy.deepcopy(self.synthesis_report.get("original_answer"))
                request = copy.deepcopy(self.synthesis_report.get("review_request"))
                feedback = {
                    "trigger": "empty_root_causes" if original is not None else "direct_request",
                    "synthesis_round": round_no,
                    "source_draft_id": self.synthesis_report.get("source_draft_id"),
                    "original_answer": original, "review_request": request,
                    "meta_return_no": returns,
                    "meta_returns_left": self.cfg.synthesis_max_meta_returns - returns,
                }
                self.rt.records.save(f"meta/recovery_{returns:02d}/synthesis_feedback.json", feedback)
                self.rt.records.event("synthesis:final", "meta_review_requested", feedback)
                state.report(self.rt, "synthesis_recovery_request", stage="synthesis", recovery_no=returns,
                             max_recoveries=self.cfg.synthesis_max_meta_returns,
                             reason=str((request or {}).get("question", "")))
                count_before = len(self.followups)
                recovery_error = False
                active_stage = "meta"
                try:
                    with stage(self.rt, "meta", agent_id=self.meta.id):
                        conclusion = meta.arbitrate(self, synthesis_feedback=feedback,
                                                   recovery_history=self.recovery_history)
                except EvidenceValidationError:
                    # 坏证据不作为普通调查缺口反复交模型处理。
                    raise
                except (Limit, StageFailure, NoProgress) as exc:
                    recovery_error = True
                    meta_failure = {"status": status_for(exc), "reason": str(exc), "error_type": type(exc).__name__}
                    conclusion = {"status": "未完成", "failure": meta_failure, "termination_reason": str(exc)}
                    self.meta_conclusion = conclusion
                    self.rt.records.event(self.meta.id, "arbitration_failed", conclusion)
                record = {"synthesis_feedback": feedback, "conclusion": copy.deepcopy(conclusion),
                          "followups": copy.deepcopy(self.followups[count_before:])}
                self.recovery_history.append(record)
                self.rt.records.save(f"meta/recovery_{returns:02d}/result.json", record)
                state.report(self.rt, "synthesis_recovery_result", stage="synthesis", recovery_no=returns,
                             max_recoveries=self.cfg.synthesis_max_meta_returns,
                             status="completed" if record["followups"] else "concluded",
                             reason=conclusion.get("termination_reason", ""))
                round_no += 1
                carryover = None
                final_mode = (recovery_error or returns >= self.cfg.synthesis_max_meta_returns or not record["followups"]
                              or self.finalize_only or not self.rt.budget.can_recovery())
            # 回流可能改变链状态，最终统计以补查后的状态为准。
            statuses = [c["status"] for c in self.ordered()]
            failed_statuses = {"technical_failure", "budget_exhausted"}
            all_failed = (bool(statuses) and all(z in failed_statuses for z in statuses)) or (
                not statuses and any(not s["resolved"] and s["status"] in failed_statuses
                                     for s in self.investigation_stops)
            )
            stops = [s for s in self.investigation_stops if not s["resolved"]]
            if all_failed:
                failed_stops = [s["status"] for s in self.investigation_stops if not s["resolved"]]
                status = "technical_failure" if "technical_failure" in statuses + failed_stops else "budget_exhausted"
                why = "所有调查分支均技术失败或预算不足；综合已读取轨迹，不能将调查失败视作正常完成"
            elif not answer["root_causes"] and not answer["propagation"]:
                status = meta_failure["status"] if meta_failure else stops[-1]["status"] if stops else "insufficient_evidence"
                why = "综合未确认可输出的根因或传播关系；空结果不表示已证明没有故障"
            elif meta_failure or any(c.get("submitted_checkpoint") or c.get("review_limit_blocked") for c in self.ordered()) or stops or any(
                c["status"] in {"budget_exhausted", "technical_failure", "investigation_stopped"}
                for c in self.ordered()
            ):
                status, why = "partial", "部分调查或审核未完成；综合结果及未解决问题见综合记录"
            else:
                status, why = "completed", "调查结束，最终综合已完成"
        except (Limit, StageFailure, NoProgress, EvidenceValidationError) as exc:
            status, why = status_for(exc), str(exc)
            if isinstance(exc, EvidenceValidationError):
                # Meta/正式输出不能绕过坏证据。保存具体原因，不把程序错误交给模型反复改 JSON。
                failure = {"failure_stage": active_stage, **exc.details()}
                self.rt.records.event(
                    "workflow", "evidence_validation_failed", {**failure, "status": status, "reason": why}
                )
            elif active_stage in {"meta", "synthesis"}:
                failure = {"failure_stage": active_stage, "error_type": type(exc).__name__}
        for req in self.requests:
            if req["response"]["status"] == "pending":
                self.resolve_request(req, status, "案例停止时未取得补种结果：" + why)
        report = {
            "case_handle": self.rt.handle,
            "output_dir": str(self.out),
            "status": status,
            "completion_reason": why,
            "budget": self.rt.budget.snapshot(),
            "counts": self.counts(),
            "investigation_stops": self.investigation_stops,
            "finalize_only": self.finalize_only,
            "meta_selection_report": self.meta_selection_report,
            "meta_conclusion": self.meta_conclusion,
            "meta_failure": meta_failure,
            "all_investigation_failed": all_failed,
            "synthesis_report": self.synthesis_report,
            "recovery_history": self.recovery_history,
            "answer": answer,
            "prediction": prediction,
            **failure,
        }
        self.rt.records.save(
            "case_summary.json",
            {
                **{k: v for k, v in report.items() if k not in {"answer", "prediction"}},
                "has_answer": answer is not None,
                "chains": meta.meta_chains(self),
                "seed_rounds": self.rounds,
                "symptoms": self.symptom_snapshot(),
            },
        )
        return report


def run_case(case_dir: Path, output_dir: Path, model, settings, case_handle: str) -> dict:
    """Run one case; model-provider failures propagate to the batch's stop policy."""
    started = monotonic()
    emit(
        "case_start",
        case_handle=case_handle,
        stage="workflow",
        budget={
            "model_calls": 0,
            "max_model_calls": settings.max_model_calls,
            "tool_calls": 0,
            "max_tool_calls": settings.max_tool_calls,
        },
    )
    incident = None
    try:
        src, out = Path(case_dir).resolve(), Path(output_dir).resolve()
        if out == src or src in out.parents:
            raise ValueError("结果目录必须与原案例目录分离")
        if any(filesystem_path(out / f).exists() for f in ("stages/events.jsonl", "agent_answer.json", "prediction.json")):
            raise ValueError("此目录已有本例调查/预测，请使用新的案例输出目录，避免混用版本")
        incident = Incident(src, out, model, settings, case_handle)
        try:
            result = incident.run()
        finally:
            incident.rt.tele.close()
    except BaseException as exc:
        emit(
            "case_end",
            case_handle=case_handle,
            stage="workflow",
            elapsed_seconds=monotonic() - started,
            status="interrupted" if isinstance(exc, KeyboardInterrupt) else "technical_failure",
            error_type=type(exc).__name__,
            budget=incident.rt.budget.snapshot() if incident else None,
            **({"counts": incident.counts(), **incident.backtracking_totals()} if incident else {}),
        )
        raise
    emit(
        "case_end",
        case_handle=case_handle,
        stage="workflow",
        elapsed_seconds=monotonic() - started,
        status=result["status"],
        reason=result["completion_reason"],
        counts=result["counts"],
        **incident.backtracking_totals(),
        budget=result["budget"],
        has_answer=result["answer"] is not None,
    )
    return result
