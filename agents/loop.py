"""四类 agent 共用调查循环；角色权限、已收到的证据和每次真实工具结果在此核对。"""

import copy
import json
import re
import threading
import time
from functools import lru_cache
from pathlib import Path

from output_contract import FAULT_KINDS
from state import Budget, Limit, Records, dump, refs_in, report
from tools.telemetry import TOOL_HELP, Telemetry
from .l1_context import L1Context, QueryHistory
from .model_input import render_context

NOTE_HELP = "记录简短进展：finding、next_step 是非空文字；evidence_refs 引用已有结果，可为空。不产生新证据。"
PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"


def model_dump(value):
    """仅紧凑序列化模型输入；保留所有值、顺序和原有 JSON 校验语义。"""
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False, separators=(",", ":"))


@lru_cache(maxsize=2)
def synthesis_tokenizer(path):
    """仅在明确配置本地文件时加载；缺失依赖或文件时明确报错。"""
    try:
        from tokenizers import Tokenizer
        return Tokenizer.from_file(str(Path(path)))
    except Exception as exc:
        raise StageFailure(f"无法加载配置的综合 token 计数文件：{exc}") from exc


class Invalid(ValueError):
    """A malformed action the agent can correct without losing the case."""


class StageFailure(Exception):
    """The agent could not produce a valid action after specific feedback."""


class NoProgress(Exception):
    """当前调查在停止查询后仍未作出决定；不表示遥测已经证明没有故障。"""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


def require(condition, message):
    if not condition:
        raise Invalid(message)


def reason(obj):
    text = obj.get("reason")
    require(isinstance(text, str) and text.strip(), "必须给出本次动作的具体 reason")
    return text.strip()


@lru_cache(maxsize=1)
def fault_kind_guide():
    """只读项目内的通用说明；核对类型覆盖，不读取案例或评分结果。"""
    guide = (PROMPT_DIR / "fault_kinds.md").read_text(encoding="utf-8-sig")
    headings = re.findall(r"^### `([a-z_]+)`", guide, re.MULTILINE)
    if len(headings) != len(FAULT_KINDS) or set(headings) != set(FAULT_KINDS):
        raise RuntimeError("fault_kinds.md 必须为 FAULT_KINDS 中的每个类型保留一个 ### `类型名` 标题")
    return guide


@lru_cache(maxsize=1)
def fault_kind_sections():
    parts = re.split(r"(?=^### `[a-z_]+`)", fault_kind_guide(), flags=re.MULTILINE)
    return {re.match(r"### `([a-z_]+)`", part).group(1): part.split("\n## ")[0].strip()
            for part in parts[1:]}


def fault_kind_summary():
    """每轮保留全部类型及其含义，详细区分方法由调查或审核按需读取。"""
    lines = []
    for name in FAULT_KINDS:
        meaning = re.search(r"\*\*中文含义\*\*：([^\n]+)", fault_kind_sections()[name])
        if not meaning:
            raise RuntimeError(f"故障类型 {name} 缺少中文含义")
        lines.append(f"- `{name}`：{meaning.group(1)}")
    return "\n".join(lines)


@lru_cache(maxsize=6)
def prompt(role):
    """加载精简运行提示词；完整 Pro 说明保留供阅读，六个调用角色分别缓存。"""
    prompt_role = "synthesis" if role == "synthesis_lookup" else role
    filename = prompt_role + "_runtime.md"
    raw = (PROMPT_DIR / filename).read_text(encoding="utf-8-sig")
    parts = raw.split("\n---USER---\n")
    if len(parts) != 2 or "{{context}}" not in parts[1]:
        raise RuntimeError(f"{filename} 缺少唯一的 ---USER--- 分隔行或 {{context}} 占位")
    options = "\n".join(f"- `{kind}`" for kind in FAULT_KINDS)
    parts[0] = parts[0].replace("{{fault_kind_options}}", options)
    if "{{fault_kind_guide}}" in parts[0]:
        parts[0] = parts[0].replace("{{fault_kind_guide}}", fault_kind_summary())
    return parts


class Runtime:
    def __init__(self, case_dir, out, model, cfg, handle):
        self.cfg, self.model, self.handle = cfg, model, handle
        self.provider_failure = None
        self.provider_stop = threading.Event()
        self.evidence_choices = EvidenceChoices(handle)
        self.records, self.budget = Records(out), Budget(cfg)
        self.tele = Telemetry(case_dir, out)
        self.names = self.tele.service_names()
        self.catalog = self.tele.catalog
        common = {
            "time",
            "service_name",
            "trace_id",
            "span_id",
            "parent_span_id",
            "duration",
            "metric",
            "metric_name",
            "value",
            "unit",
            "body",
            "span_name",
        }
        self.compact_catalog = {
            "windows": self.catalog.get("windows", {}),
            "tables": {
                k: {
                    "row_count": v.get("row_count"),
                    "common_columns": sorted(common & set(v.get("columns", {}))),
                }
                for k, v in self.catalog.get("tables", {}).items()
            },
            "note": "完整字段名与类型按需用 inspect_telemetry(action=columns,table=...) 查看。",
        }

    def service(self, name):
        require(isinstance(name, str) and name in self.names, f"遥测中没有这个服务名：{name!r}")
        return name

    def refs(self, values, available, *, empty=False):
        """核对编号确实来自当前会话，并且对应本案例的有效、非空工具结果。"""
        require(
            isinstance(values, list) and all(isinstance(i, str) for i in values),
            "evidence_refs 必须是引用编号列表",
        )
        values = list(dict.fromkeys(values))
        require(empty or values, "此项必须引用实际读过的证据")
        for ref in values:
            require(ref in available, f"当前会话未收到证据 {ref}")
            try:
                item = self.tele.evidence(ref)  # 读已登记的查询；正式导出才重新执行 SQL。
            except (KeyError, ValueError) as exc:
                raise Invalid(f"找不到证据 {ref}") from exc
            require(
                isinstance(item, dict) and item.get("sql") and item.get("rows"),
                f"{ref} 没有可复查的非空数据，不能作为支持证据",
            )
        return values

    def preview(self, ids):
        """层间传递小份真实样本；完整结果由编号重查，不把模型文字当作原始证据。"""
        preview = {}
        for ref in list(dict.fromkeys(ids))[:8]:
            item = self.tele.evidence(ref)
            rows = [
                {key: (v[:400] + "…" if isinstance(v, str) and len(v) > 400 else v) for key, v in row.items()}
                for row in item["rows"][:2]
            ]
            preview[ref] = {
                "kind": item["kind"],
                "rows": rows,
                "saved_rows": len(item["rows"]),
                "result_truncated": item.get("truncated", False),
            }
        return preview


class EvidenceChoices:
    """案例内固定编号；只负责对应关系，证据权限仍由每个会话的 seen 决定。"""

    def __init__(self, handle):
        self.handle = handle
        self.lock = threading.RLock()
        self.by_ref = {}
        self.by_choice = {}

    def available(self, seen):
        with self.lock:
            for ref in sorted(seen):
                if ref not in self.by_ref:
                    choice = f"{self.handle}-e{len(self.by_ref) + 1}"
                    self.by_ref[ref] = choice
                    self.by_choice[choice] = ref
            return {self.by_ref[ref]: ref for ref in sorted(seen)}


class Session:
    def __init__(self, rt, role, agent_id, *, chain=None, inherited=(), neighbors=None, investigation=None):
        self.rt, self.role, self.id, self.chain = rt, role, agent_id, chain
        self.reseed_budget = False
        self.seen = set(inherited)
        self.neighbors = copy.deepcopy(neighbors or {})
        # 每条 L1 独立持有；分叉复制已有事实，不共享可变的查询计数或调查笔记。
        self.investigation = None
        if role == "l1":
            self.investigation = copy.deepcopy(investigation) if investigation is not None else L1Context()
        self.query_history = self.investigation if self.investigation is not None else QueryHistory()
        self.history = []
        self.consecutive_repeats = 0
        self.last_repeated_query = None
        self.failures, self.bad_actions = [], 0
        self.l1_review_failures = 0
        self.correction = ""
        self.read_refs = set()
        self.data_queries = 0
        self.progress_chain = None
        self.progress_stage = self.role
        # 仅供 L1 判断本次控制决定是否已经使用了最后决定机会；不改变预算。
        self.last_final_action_only = False
        self.allowed_tools = {
            **({"replay_evidence": TOOL_HELP["replay_evidence"]} if role == "meta" else TOOL_HELP),
            "record_progress": NOTE_HELP,
        }
        if role in {"l1", "l2"}:
            self.allowed_tools["read_fault_guide"] = (
                "读取通用故障类型说明：fault_kinds 为 1 至 5 个正式类型名；"
                "返回支持线索和区分方法，不是案例观测，不产生证据，消耗一次工具额度。"
            )
        if role == "l1":
            # 兼容只构造了最小 Runtime 测试替身的已有调用；真实 Runtime 按案例共享。
            self.evidence_choices = getattr(rt, "evidence_choices", None) or EvidenceChoices(rt.handle)
            self.allowed_tools["record_progress"] += " L1 可用 evidence_choice_ids 选择当前可用证据。"
            self.allowed_tools["replay_evidence"] += " L1 可用 evidence_choice_id 选择当前可用证据。"

    def resolve_l1_choices(self, action, offered):
        """只转换 L1 协议明确允许的位置；原始动作与其他角色不受影响。"""
        converted = copy.deepcopy(action)
        used = {}

        def resolve(choice):
            require(isinstance(choice, str) and choice in offered,
                    f"当前会话未提供证据选择编号 {choice!r}，请从 available_evidence_choices 中选择")
            ref = offered[choice]
            require(ref in self.seen, f"当前会话未收到证据 {ref}")
            used[choice] = ref
            return ref

        def list_field(obj):
            if not isinstance(obj, dict) or "evidence_choice_ids" not in obj:
                return
            choices = obj.pop("evidence_choice_ids")
            require(isinstance(choices, list), "evidence_choice_ids 必须是短编号列表")
            refs = [resolve(choice) for choice in choices]
            if "evidence_refs" in obj:
                old = obj["evidence_refs"]
                require(isinstance(old, list) and all(isinstance(ref, str) for ref in old),
                        "evidence_refs 必须是引用编号列表")
                require(set(old) == set(refs), "短编号与完整 evidence_refs 不一致，请只选一组证据")
            obj["evidence_refs"] = refs

        op = converted.get("action")
        if op == "tool":
            args = converted.get("args")
            if isinstance(args, dict) and converted.get("tool") == "record_progress":
                list_field(args)
            elif isinstance(args, dict) and converted.get("tool") == "replay_evidence" and "evidence_choice_id" in args:
                choice = args.pop("evidence_choice_id")
                ref = resolve(choice)
                require("evidence_ref" not in args or args["evidence_ref"] == ref,
                        "短编号与完整 evidence_ref 不一致，请只选一份证据")
                args["evidence_ref"] = ref
        else:
            list_field(converted)
            for key in ("root", "symptom", "edge", "target_symptom"):
                list_field(converted.get(key))
            # An annotation cannot invalidate an otherwise valid control action.
            annotations = converted.get("new_symptoms")
            if isinstance(annotations, list):
                for annotation in annotations:
                    try:
                        list_field(annotation)
                    except Invalid as exc:
                        if isinstance(annotation, dict):
                            annotation["annotation_error"] = str(exc)
            diagnosis = converted.get("fault_diagnosis")
            if isinstance(diagnosis, dict) and isinstance(diagnosis.get("candidates"), list):
                for candidate in diagnosis["candidates"]:
                    list_field(candidate)

        if used:
            # 只补充 L1 自己撰写的说明，不改 SQL、遥测原文，也不从文字授予证据权限。
            text_objects = [(converted, ("reason",))]
            if isinstance(converted.get("symptom"), dict):
                text_objects.append((converted["symptom"], ("description",)))
            if op == "tool" and converted.get("tool") == "record_progress" and isinstance(converted.get("args"), dict):
                text_objects.append((converted["args"], ("finding", "next_step")))
            for obj, fields in text_objects:
                for field in fields:
                    value = obj.get(field)
                    if not isinstance(value, str):
                        continue
                    for choice, ref in offered.items():
                        if ref not in self.seen:
                            continue
                        pattern = r"(?<![A-Za-z0-9_-])" + re.escape(choice) + r"(?![A-Za-z0-9_-])"
                        value = re.sub(pattern, lambda match: f"{match.group()}（{ref}）", value)
                    obj[field] = value
        return converted, used

    def note(self, kind, data):
        self.history.append({"event": kind, "data": data})
        self.rt.records.event(self.id, kind, data)

    def progress(self, kind, **fields):
        report(
            self.rt,
            kind,
            chain=self.progress_chain,
            stage=self.progress_stage,
            role=self.role,
            agent_id=self.id,
            **fields,
        )

    def _record_rejection(self, problem, consecutive_errors):
        self.correction = " ".join(str(problem).split())[:300]
        self.note("action_rejected", str(problem))
        self.progress(
            "action_rejected", status="rejected", consecutive_errors=consecutive_errors, reason=self.correction
        )

    def reject(self, problem, *, action=None):
        if self.role == "l1" and action == "review":
            self.l1_review_failures += 1
            self._record_rejection(problem, self.l1_review_failures)
            max_retries = self.rt.cfg.max_l1_review_retries
            if self.l1_review_failures > max_retries:
                raise StageFailure(
                    f"L1 送审动作连续 {self.l1_review_failures} 次未通过校验"
                    f"（首次失败后最多额外纠错 {max_retries} 次），最后一项：{self.correction}"
                )
            return
        self.bad_actions += 1
        self._record_rejection(problem, self.bad_actions)
        if self.bad_actions >= 3:
            raise StageFailure(f"连续 3 个无效动作，最后一项：{self.correction}")

    def accepted(self):
        """只有查询取得新信息，或控制动作通过所属阶段的完整检查，才结束这段连续错误。"""
        self.bad_actions = 0
        self.l1_review_failures = 0
        self.correction = ""
        self.consecutive_repeats = 0
        self.last_repeated_query = None

    def query_control(self):
        limit = self.rt.cfg.max_consecutive_repeated_queries
        return {
            "consecutive_repeats": self.consecutive_repeats,
            "max_consecutive_repeated_queries": limit,
            "decision_required": self.consecutive_repeats >= limit,
            "last_repeated_query": copy.deepcopy(self.last_repeated_query),
            "reason": (
                "连续重复读取已达上限；本轮停止查询，请用已有证据完成本阶段决定，不得编造结果。"
                if self.consecutive_repeats >= limit else ""
            ),
        }

    def repeated(self, name, args, record, *, executed=False):
        """只数连续重复读取；JSON 纠错和笔记不会清零，成功的新查询/控制动作才清零。"""
        self.consecutive_repeats += 1
        self.last_repeated_query = {
            "tool": name,
            "args": copy.deepcopy(args),
            "query_no": record["query_no"],
            "evidence_refs": sorted(refs_in(record["result"])),
        }
        control = self.query_control()
        self.progress(
            "query_repeated" if executed else "tool_reused",
            tool=name,
            status=record["result"].get("status"),
            query_no=record["query_no"],
            reuse_count=record["reuse_count"],
            target={
                k: args[k]
                for k in ("operation", "action", "component", "table", "metric", "window", "direction")
                if isinstance(args.get(k), str)
            },
            consecutive_repeats=self.consecutive_repeats,
            max_consecutive_repeated_queries=self.rt.cfg.max_consecutive_repeated_queries,
        )
        if control["decision_required"]:
            self.note("query_decision_required", control)
            self.progress(
                "query_decision_required",
                reason=control["reason"],
                consecutive_repeats=self.consecutive_repeats,
                max_consecutive_repeated_queries=self.rt.cfg.max_consecutive_repeated_queries,
            )

    def recent(self):
        """完整记录在 events.jsonl。模型看原始数值样本、引用与截断标记，不反复接收宽表目录。"""
        view = []
        for event in self.history[-8:]:
            kind, value = event["event"], event["data"]
            if kind == "tool_result":
                result = dict(value["result"])
                if result.get("details", {}).get("window") == "both" and "windows" in result:
                    result.pop("rows", None)
                    result["windows"] = {
                        window: {**item, "rows": item.get("rows", [])[:4], "preview_only": True}
                        for window, item in result["windows"].items()
                    }
                if "catalog" in result:
                    result["catalog"] = self.rt.compact_catalog
                for field in ("rows", "edges"):
                    rows = result.get(field)
                    if isinstance(rows, list) and len(rows) > 8:
                        result[field] = rows[:8]
                        result[field + "_preview"] = f"只展示前8/{len(rows)}项；缩小条件可查看其余记录"
                        if field == "edges":
                            result["other_neighbors"] = [
                                {
                                    k: v
                                    for k, v in r.items()
                                    if k in {"caller", "callee", "window", "evidence_refs"}
                                }
                                for r in rows[8:]
                            ]
                # 主动重放时可看真实 SQL；普通结果保留原始单位、值、查询条件及证据编号。
                value = {**value, "result": result}
            view.append({"event": kind, "data": value})
        return view

    def _tool(self, data):
        name, args = data.get("tool"), data.get("args")
        require(
            isinstance(name, str) and name in self.allowed_tools and isinstance(args, dict),
            "该角色没有这个工具，或 args 不符合说明",
        )
        if name == "read_fault_guide":
            names = args.get("fault_kinds")
            require(isinstance(names, list) and 1 <= len(names) <= 5
                    and all(isinstance(n, str) and n in FAULT_KINDS for n in names),
                    "fault_kinds 必须包含 1 至 5 个正式类型名")
        if name == "record_progress":
            require(
                all(isinstance(args.get(k), str) and args[k].strip() for k in ("finding", "next_step")),
                "进展笔记需要 finding 与 next_step，不记录内部推理",
            )
            ids = args.get("evidence_refs", [])
            require(
                isinstance(ids, list) and all(isinstance(r, str) and r in self.seen for r in ids),
                "笔记只引用本会话收到的证据",
            )
            call_no = self.rt.budget.tool(self.chain, supplement=self.reseed_budget)
            started = time.monotonic()
            self.progress("tool_start", tool=name, call_no=call_no)
            note_status = "failed"
            try:
                self.note("progress_note", args)
                if self.investigation is not None:
                    self.investigation.notes.append(copy.deepcopy(args))
                note_status = "recorded"
            finally:
                self.progress(
                    "tool_end",
                    tool=name,
                    call_no=call_no,
                    elapsed_seconds=time.monotonic() - started,
                    status=note_status,
                    evidence_count=0,
                )
            return False  # 笔记本身没有取得新观测。
        if name == "replay_evidence":
            require(args.get("evidence_ref") in self.seen, "只能重放已交给本会话的证据")
        signature = (name, json.dumps(args, sort_keys=True, ensure_ascii=False, allow_nan=False))
        previous = self.query_history.reusable(signature)
        if name != "replay_evidence":
            cached = previous
            if cached:
                # 只复用本案例未变化的数据；主动 replay_evidence 仍按工具契约实际重跑 SQL。
                self.rt.tele.check_current()
                if name == "query_graph" and getattr(self, "branch_frontier", None) is not None:
                    self.branch_frontier.observe(self.chain, self.branch_chain["revision"], args, cached["result"])
                cached["reuse_count"] += 1
                self.note(
                    "tool_result",
                    {
                        "tool": name,
                        "args": args,
                        "result": copy.deepcopy(cached["result"]),
                        "reused": True,
                        "new_observation": False,
                        "query_no": cached["query_no"],
                        "reuse_count": cached["reuse_count"],
                        "message": (
                            "返回这项查询已有的结果，未重新执行工具、未新增证据或工具开销。"
                        ),
                    },
                )
                self.repeated(name, args, cached)
                return False
        call_no = self.rt.budget.tool(self.chain, supplement=self.reseed_budget)
        started = time.monotonic()
        # SQL、原始日志和模型笔记不作进度字段，只显示所查对象和时间窗。
        target = {
            k: args[k]
            for k in ("operation", "action", "component", "table", "metric", "window", "direction")
            if isinstance(args.get(k), str)
        }
        self.progress("tool_start", tool=name, call_no=call_no, target=target)
        try:
            if name == "read_fault_guide":
                names = args.get("fault_kinds")
                result = {"status": "ok", "guide": {n: fault_kind_sections()[n] for n in names},
                          "evidence_refs": [], "note": "通用类型说明，不是案例观测。"}
            else:
                result = self.rt.tele.call(name, args)
        except Exception as exc:  # local telemetry operation only, not a model transport call
            result = {"status": "technical_failure", "error": f"{type(exc).__name__}: {exc}"}
        except BaseException as exc:
            self.progress(
                "tool_end",
                tool=name,
                call_no=call_no,
                target=target,
                status="interrupted",
                elapsed_seconds=time.monotonic() - started,
                error_type=type(exc).__name__,
            )
            raise
        summary = result if isinstance(result, dict) else {}
        self.progress(
            "tool_end",
            tool=name,
            call_no=call_no,
            target=target,
            status=summary.get("status", "invalid_result"),
            elapsed_seconds=time.monotonic() - started,
            row_count=len(summary["rows"]) if isinstance(summary.get("rows"), list) else None,
            edge_count=len(summary["edges"]) if isinstance(summary.get("edges"), list) else None,
            evidence_count=len(refs_in(summary)),
            truncated=bool(summary.get("truncated", False)),
        )
        require(isinstance(result, dict), "工具没有返回对象")
        if name == "query_graph" and getattr(self, "branch_frontier", None) is not None:
            self.branch_frontier.observe(self.chain, self.branch_chain["revision"], args, result)
        self.query_history.remember(signature, name, args, result, self.id, data.get("reason", ""))
        if self.role == "seed":
            self.query_history.queries[signature]["seed_round"] = getattr(self, "seed_round", None)
        success = result.get("status") in {"ok", "no_observations"}
        returned = refs_in(result)
        # 一个新范围的合法空查询也回答了调查问题。重复读取同一份结果不刷新计数。
        repeated_replay = (
            name == "replay_evidence" and previous is not None
            and (previous["result"].get("status") in {"ok", "no_observations"} or not success)
        )
        progressed = success and not repeated_replay
        if success:
            if name != "read_fault_guide" and (name != "inspect_telemetry" or args.get("action") == "rows"):
                self.data_queries += 1
            self.seen.update(returned)
            self.read_refs.update(returned)
            if name == "query_graph":
                for e in result.get("edges", []):
                    if e.get("window") in {"normal", "abnormal"} and e.get("caller") and e.get("callee"):
                        key = tuple(sorted((e["caller"], e["callee"])))
                        existing = self.neighbors.setdefault(key, [])
                        fact = {k: e.get(k) for k in ("caller", "callee", "window", "evidence_refs")}
                        if fact not in existing:
                            existing.append(fact)
        else:
            self.failures.append({"tool": name, "args": args, "result": result})
        self.note("tool_result", {"tool": name, "args": args, "result": result})
        if repeated_replay:
            record = self.query_history.queries[signature]
            record["reuse_count"] += 1
            self.repeated(name, args, record, executed=True)
        return progressed

    def synthesis_request(self, context):
        """构造与实际综合请求一致的提示词，供请求前估算。"""
        system, template = prompt(self.role)
        system += "\n本轮只输出一个 JSON 对象，不调用工具。"
        payload = {"case_handle": self.rt.handle, **context,
                   "format_feedback": [v for v in self.history if v["event"] == "action_rejected"][-2:],
                   "incident_description": getattr(self.rt.cfg, "incident_description", "")}
        if self.correction:
            payload["本轮纠错指令"] = f"请纠正这一处：{self.correction}。只返回符合本阶段协议的一个完整 JSON 对象。"
        serialized, instruction = render_context(payload, self.rt.cfg)
        return system + instruction, template.replace("{{context}}", serialized)

    def synthesis_size(self, context, *, request=None):
        """已配置本地分词器则计数，否则使用 UTF-8 字节保守估算。"""
        system, user = self.synthesis_request(context) if request is None else request
        cfg = self.rt.cfg
        path = getattr(cfg, "synthesis_tokenizer_file", None)
        if path:
            tokenizer = synthesis_tokenizer(path)
            estimated = len(tokenizer.encode(system, add_special_tokens=False).ids) + len(
                tokenizer.encode(user, add_special_tokens=False).ids)
            method = "configured_tokenizer_text_count_excluding_message_wrapper"
        else:
            estimated = len(system.encode("utf-8")) + len(user.encode("utf-8"))
            method = "utf8_bytes_conservative_estimate_not_exact"
        limit = cfg.synthesis_context_limit - cfg.synthesis_output_reserve - cfg.synthesis_context_margin
        return {"estimated_input_tokens": estimated, "method": method,
                "input_budget": limit, "fits": estimated <= limit}

    def step(self, context, actions, *, slot=None, synthesis=False):
        """工具动作实际执行后再送回模型；有控制决定时返回所属上层，不转交链的写权限。"""
        self.progress_chain = context.get("chain")
        self.progress_stage = "synthesis" if synthesis else self.role
        system, template = prompt(self.role)
        system += (
            "\n本轮只输出正式答案对象，不调用工具。"
            if synthesis
            else "\n可用工具（程序实际提供的参数）：\n"
            + model_dump(self.allowed_tools)
            + "\n允许的控制动作："
            + ", ".join(actions)
        )
        json_failures = 0
        max_json_retries = self.rt.cfg.max_json_retries
        while True:
            if self.rt.provider_stop.is_set():
                raise self.rt.provider_failure
            if synthesis:
                synthesis_request = self.synthesis_request(context)
                size = self.synthesis_size(context, request=synthesis_request)
                if not size["fits"]:
                    raise StageFailure(f"综合请求计数或估算 {size['estimated_input_tokens']}（{size['method']}），超过输入预算 {size['input_budget']}；未发送模型")
            # 调用号和额度在同一锁内读取；另一条 L1 不会抢走显示用的编号。
            try:
                with self.rt.budget.lock:
                    budget_role = ("synthesis_lookup" if self.role == "synthesis_lookup" else "synthesis") if synthesis else self.role
                    final_only = self.rt.budget.model(
                        budget_role, self.chain, slot, supplement=self.reseed_budget
                    )
                    call_no = self.rt.budget.models
            except Limit:
                if json_failures:
                    self.progress(
                        "json_retry_stopped",
                        status="budget_exhausted",
                        detail=f"已重试 {json_failures - 1}/{max_json_retries} 次；本阶段可用模型额度不足",
                    )
                raise
            control = self.query_control()
            final_only = final_only or control["decision_required"]
            allowed_actions = ([a for a in actions if a in {"select", "conclude"}]
                               if self.role == "meta" and final_only and not synthesis else actions)
            if synthesis:
                call_system, user = synthesis_request
            else:
                payload = {
                    "case_handle": self.rt.handle,
                    "catalog": self.rt.compact_catalog,
                    "fault_kinds": list(FAULT_KINDS) if self.role != "seed" else [],
                    **context,
                    "budget": self.rt.budget.snapshot(),
                    "final_action_only": final_only,
                    "query_control": control,
                    "allowed_control_actions": allowed_actions,
                    "available_refs": sorted(self.seen),
                    "history": self.recent(),
                }
                if self.investigation is not None:
                    payload["investigation_summary"] = self.investigation.summary(
                        context["chain"], self.neighbors
                    )
                    frontier_view = getattr(self, "branch_frontier_view", None)
                    if frontier_view is not None:
                        payload["branch_frontier"] = frontier_view()
                elif self.role == "seed":
                    payload["investigation_summary"] = self.query_history.query_summary()
                if self.role == "l1":
                    offered = self.evidence_choices.available(self.seen)
                    payload["available_evidence_choices"] = [
                        {"choice_id": choice, "evidence_ref": ref} for choice, ref in offered.items()
                    ]
                payload["incident_description"] = getattr(self.rt.cfg, "incident_description", "")
                if self.correction:
                    # 放在本轮上下文的末项，下一次调用可直接看到应改哪一处，而不必在历史中寻找。
                    payload["本轮纠错指令"] = (
                        f"请纠正这一处：{self.correction}。只返回符合本阶段协议的一个完整 JSON 对象。"
                    )
                serialized, instruction = render_context(payload, self.rt.cfg)
                user = template.replace("{{context}}", serialized)
                call_system = system + instruction
            if final_only and not synthesis:
                call_system += (
                    "\n本轮停止所有工具调用，包括证据重放和进展记录。只允许控制动作："
                    + ", ".join(allowed_actions)
                    + "。请依据已有证据决定；未解决的问题如实说明。"
                )
            # 首次失败后才开始编号；直到取得有效 JSON，这组重试才结束。
            retry = {"json_retry_no": json_failures, "max_json_retries": max_json_retries} if json_failures else {}
            started = time.monotonic()
            self.progress("model_start", call_no=call_no, status="waiting", **retry)
            outcome = {"status": "interrupted"}
            format_error = None
            try:
                action = self.rt.model.call(call_system, user, agent_id=self.id)
                outcome = {"status": "replied"}
            except ValueError as exc:  # invalid JSON only; provider exhaustion must propagate
                outcome = {"status": "invalid_response", "error_type": type(exc).__name__}
                format_error = f"输出不是有效 JSON：{exc}"
            except Exception as exc:
                # 原客户端已完成它自己的主备容灾。通知并行兄弟停止发起下一次模型请求。
                self.rt.provider_failure = exc
                self.rt.provider_stop.set()
                outcome = {"status": "provider_failure", "error_type": type(exc).__name__}
                raise
            finally:
                self.progress(
                    "model_end", call_no=call_no, elapsed_seconds=time.monotonic() - started, **outcome, **retry
                )
            if format_error is not None:
                json_failures += 1
                self._record_rejection(format_error, json_failures)
                if synthesis:
                    # 综合的外层统一计数；一次格式错误就是一次生成，不再嵌套通用 JSON 重试。
                    raise Invalid(format_error)
                if json_failures > max_json_retries:
                    detail = f"最后一项：{self.correction}"
                    self.progress(
                        "json_retry_stopped",
                        status="technical_failure",
                        call_no=call_no,
                        max_json_retries=max_json_retries,
                        detail=detail,
                    )
                    raise StageFailure(f"JSON 纠错重试 {max_json_retries} 次后仍失败，{detail}")
                continue
            if json_failures:
                json_failures = 0
                self.correction = ""
            self.note("model_action", action)
            try:
                if synthesis:
                    return action
                require(isinstance(action, dict), "必须返回 JSON 对象")
                if self.role == "l1":
                    action, used = self.resolve_l1_choices(action, offered)
                    if used:
                        # 不加入 self.history，避免改变最近历史和连续重复查询的判断。
                        self.rt.records.event(self.id, "evidence_choice_resolved", {
                            "choices": [{"choice_id": choice, "evidence_ref": ref}
                                        for choice, ref in used.items()]
                        })
                if action.get("action") != "tool":
                    require(action.get("action") in allowed_actions, f"本轮允许动作：{', '.join(allowed_actions)}")
                    if self.role == "l1":
                        self.last_final_action_only = final_only
                    return action
                if control["decision_required"]:
                    raise NoProgress(
                        "连续重复读取已达上限，要求用已有证据作决定后仍请求查询；当前调查未完成。",
                        control,
                    )
                require(not final_only, "这是预留的最终决定轮，不能再发起工具查询")
                try:
                    if self._tool(action):
                        self.accepted()
                except Limit:
                    self.note("tool_budget_exhausted", "当前调查可用的工具额度已用完（部分额度可能预留给补种）；用已有证据决定或明确停止。")
                    if self.history[-3:-2] and self.history[-3]["event"] == "tool_budget_exhausted":
                        raise
            except Invalid as exc:
                self.reject(exc, action=action.get("action") if isinstance(action, dict) else None)


def text_field(obj, key):
    v = obj.get(key)
    require(isinstance(v, str) and v.strip(), f"{key} 需要具体文字")
    return v.strip()


def object_list(obj, key):
    items = obj.get(key)
    require(isinstance(items, list) and all(isinstance(x, dict) for x in items), f"{key} 必须为对象列表")
    return items


def status_for(exc):
    return (
        "budget_exhausted"
        if isinstance(exc, Limit)
        else "investigation_stopped"
        if isinstance(exc, NoProgress)
        else "technical_failure"
    )


def evidence_scopes(case, refs):
    """Read stored metadata only; free SQL is not parsed into invented request scopes."""
    rows = []
    for ref in refs:
        evidence = case.rt.tele.evidence(ref)
        details = evidence.get("details") or {}
        rows.append({"evidence_ref": ref, "data_version": evidence.get("data_version"),
                     "truncated": bool(evidence.get("truncated")),
                     "query": {k: copy.deepcopy(details[k]) for k in
                               ("tool", "window", "operation", "component_filter", "trace_id", "start", "end")
                               if k in details},
                     "request_scope_status": "scope_unknown"})
    return rows


def checked_symptom(case, obj, session):
    require(isinstance(obj, dict), "symptom 必须是对象或 null")
    result = {
        "service": case.rt.service(obj.get("service")),
        "description": text_field(obj, "description"),
        "evidence_refs": case.rt.refs(obj.get("evidence_refs"), session.seen),
    }
    for key in ("operation", "anomaly_kind", "scope"):
        if key in obj and obj[key] is not None:
            require(isinstance(obj[key], str) and bool(obj[key].strip()), f"{key} 必须是非空文字")
            result[key] = obj[key].strip()
    result["evidence_scopes"] = evidence_scopes(case, result["evidence_refs"])
    return result


def abnormal_refs(case, refs, cache=None):
    # 用已授权的查询来源检查，不把 SQL 文字、别名或引号中的“abnormal_”当表。
    # 已保存证据无法校验时交给调度器处理，不能当成“没有异常窗口证据”返回 False。
    for ref in refs:
        _, _, sources = (case.rt.tele.validated_evidence(ref) if cache is None
                         else case.rt.tele.validated_evidence(ref, cache=cache))
        if any(name.startswith("abnormal_") for name in sources):
            return True
    return False
