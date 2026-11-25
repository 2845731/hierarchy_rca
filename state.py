"""在一个案例内保存过程、分配共有预算；本模块不读取遥测，也不调用模型。"""

import copy
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from progress import emit
from filesystem import filesystem_path, atomic_write_text


class Limit(Exception):
    """A configured resource limit, not a finding about the incident."""


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


class Records:
    """完整事件按发生顺序追加；每个阶段另有可以直接阅读的版本与结果。"""

    def __init__(self, output_dir):
        self.root = Path(output_dir) / "stages"
        filesystem_path(self.root).mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.seq = 0
        self.chain_numbers = {}  # 仅用于显示，不改变诊断使用的链编号或模型输入。

    def register_chain(self, cid, within_l1):
        with self.lock:
            self.chain_numbers[cid] = {"case_chain_no": len(self.chain_numbers) + 1, "l1_chain_no": within_l1}

    def save(self, path, obj):
        with self.lock:
            path = self.root / path
            atomic_write_text(path, dump(obj))

    def event(self, agent, event, value):
        with self.lock:
            self.seq += 1
            row = {
                "seq": self.seq,
                "time": datetime.now(timezone.utc).isoformat(),
                "agent_id": agent,
                "event": event,
                "data": value,
            }
            with filesystem_path(self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(dump(row) + "\n")


class Budget:
    """全案例共用一份账。新开分支前，给它留出 L2 审核与 L1 接收反馈的调用。"""

    def __init__(self, settings):
        self.cfg = settings
        self.models = self.tools = 0
        self.synthesis_lookups = 0
        self.synthesis_round_lookups = 0
        self.synthesis_final_active = False
        self.meta_left = settings.meta_reserve
        self.holds = {}  # chain -> still reserved final audit / L1 response turns
        enabled = settings.seed_rounds > 0 and settings.max_candidates > 0
        self.reseed_models_left = settings.reseed_model_reserve if enabled else 0
        self.reseed_tools_left = settings.reseed_tool_reserve if enabled else 0
        self.reseed_seeds = set()
        self.lock = threading.RLock()

    def _supplement(self, chain):
        return bool(chain and chain.split("-")[0] in self.reseed_seeds)

    def begin_synthesis_round(self, final_mode=False):
        """每轮独立计回查次数；案例总预算和累计次数不重置。"""
        with self.lock:
            self.synthesis_round_lookups = 0
            self.synthesis_final_active = False

    def enter_synthesis_final(self):
        """只有开始最终答案提交后，才可使用为首次提交与纠错预留的调用。"""
        with self.lock:
            self.synthesis_final_active = True

    def _final_reserve(self):
        return 1 + self.cfg.synthesis_final_max_retries

    def can_synthesis_continue(self):
        with self.lock:
            holds = sum(len(slots) for slots in self.holds.values())
            return self.cfg.max_model_calls - self.models > self._final_reserve() + holds

    def can_synthesis_lookup(self):
        """查询不占用最终首次提交和全部纠错所需额度。"""
        with self.lock:
            holds = sum(len(slots) for slots in self.holds.values())
            return (self.synthesis_round_lookups < self.cfg.synthesis_lookup_max
                    and self.cfg.max_model_calls - self.models > self._final_reserve() + holds)

    def can_recovery(self):
        """确保有 Meta 结案和补查所需额度，同时保护最终综合。"""
        with self.lock:
            remaining = self.cfg.max_model_calls - self.models
            holds = sum(len(slots) for slots in self.holds.values())
            # 先消耗一次 Meta 决策，再为补查留至少 3 次及 Meta 结案留 1 次。
            return (remaining >= max(self.cfg.meta_synthesis_reserve, self._final_reserve()) + holds + 5
                    and self.tools < self.cfg.max_tool_calls)

    def register_reseed(self, seed_id):
        with self.lock:
            self.reseed_seeds.add(seed_id)

    def release_reseed(self):
        with self.lock:
            self.reseed_models_left = self.reseed_tools_left = 0

    def _free(self, supplement=False):
        return (
            self.cfg.max_model_calls
            - self.models
            - max(self.meta_left, self._final_reserve())
            - sum(len(slots) for slots in self.holds.values())
            - (0 if supplement else self.reseed_models_left)
        )

    def reserve(self, chain, *, before_walk=True):
        with self.lock:
            need = {"audit", "response"} - self.holds.get(chain, set())
            supplement = self._supplement(chain)
            if need and self._free(supplement) < len(need) + int(before_walk):
                return False
            if supplement and need:
                self.reseed_models_left -= min(
                    self.reseed_models_left, max(0, len(need) - max(0, self._free()))
                )
            self.holds.setdefault(chain, set()).update(need)
            return True

    def release(self, chain, slot=None):
        with self.lock:
            if slot is None:
                self.holds.pop(chain, None)
            elif chain in self.holds:
                self.holds[chain].discard(slot)

    def model(self, role, chain=None, slot=None, *, supplement=False):
        """Return True when this is a reserved final-action-only turn."""
        with self.lock:
            remaining = self.cfg.max_model_calls - self.models
            if role == "synthesis_lookup":
                ok = (self.synthesis_round_lookups < self.cfg.synthesis_lookup_max
                      and remaining > self._final_reserve() + sum(len(slots) for slots in self.holds.values()))
            elif role == "synthesis":
                reserve = 0 if self.synthesis_final_active else self._final_reserve()
                ok = remaining > reserve + sum(len(slots) for slots in self.holds.values())
            elif role == "meta":
                ok = remaining > self.cfg.meta_synthesis_reserve + sum(len(slots) for slots in self.holds.values())
            else:
                eligible = supplement or self._supplement(chain)
                ok = self._free(eligible) > 0
            last = False
            if not ok and role not in {"synthesis", "synthesis_lookup", "meta"}:
                if slot in self.holds.get(chain, set()):
                    self.holds[chain].remove(slot)
                    ok = last = True
            if not ok:
                raise Limit("model_budget_exhausted")
            if role not in {"synthesis", "synthesis_lookup", "meta"} and (supplement or self._supplement(chain)):
                if not last and self._free() <= 0 and self.reseed_models_left:
                    self.reseed_models_left -= 1
            self.models += 1
            if role == "synthesis_lookup":
                self.synthesis_lookups += 1
                self.synthesis_round_lookups += 1
            if role in {"meta", "synthesis", "synthesis_lookup"}:
                self.meta_left = max(0, self.meta_left - 1)
            return last or (role == "meta" and remaining == self.cfg.meta_synthesis_reserve + 1
                            + sum(len(slots) for slots in self.holds.values()))

    def allow_followup(self, meta_calls_to_keep=None):
        # 留下 Meta 最终决定和综合额度，且不占用其他链已预留的审核与反馈额度。
        with self.lock:
            required = (
                1 + self.cfg.meta_synthesis_reserve
                if meta_calls_to_keep is None else meta_calls_to_keep
            )
            required = max(required, self.cfg.meta_synthesis_reserve + 1)
            reserved = max(self.meta_left, required)
            free = self.cfg.max_model_calls - self.models - reserved - sum(len(s) for s in self.holds.values())
            if free < 3:
                return False
            self.meta_left = reserved
            return True

    def tool(self, chain=None, *, supplement=False):
        with self.lock:
            eligible = supplement or self._supplement(chain)
            limit = self.cfg.max_tool_calls - (0 if eligible else self.reseed_tools_left)
            if self.tools >= limit:
                raise Limit("tool_budget_exhausted")
            if eligible and self.tools >= self.cfg.max_tool_calls - self.reseed_tools_left and self.reseed_tools_left:
                self.reseed_tools_left -= 1
            self.tools += 1
            return self.tools  # 在锁内取得本案例工具调用号，并发执行不会重复。

    def snapshot(self):
        with self.lock:
            return {
                "model_calls": self.models,
                "max_model_calls": self.cfg.max_model_calls,
                "tool_calls": self.tools,
                "max_tool_calls": self.cfg.max_tool_calls,
                "meta_reserve": self.cfg.meta_reserve,
                "meta_reserved_left": self.meta_left,
                "meta_synthesis_reserve": self.cfg.meta_synthesis_reserve,
                "synthesis_lookups_total": self.synthesis_lookups,
                "synthesis_round_lookups": self.synthesis_round_lookups,
                "synthesis_final_reserve": self._final_reserve(),
                "reserved_chain_turns": sum(len(s) for s in self.holds.values()),
                "reseed_model_reserved_left": self.reseed_models_left,
                "reseed_tool_reserved_left": self.reseed_tools_left,
            }


def refs_in(obj):
    """Extract only explicit evidence reference fields, never IDs invented in prose."""
    if isinstance(obj, list):
        return set().union(*(refs_in(x) for x in obj)) if obj else set()
    if not isinstance(obj, dict):
        return set()
    refs = set()
    for key, value in obj.items():
        if key == "evidence_refs" and isinstance(value, list):
            refs.update(x for x in value if isinstance(x, str))
        elif key == "evidence_ref" and isinstance(value, str):
            refs.add(value)
        else:
            refs.update(refs_in(value))
    return refs


def report(rt, kind, *, chain=None, **fields):
    """把可公开的状态摘要发给批次。完整查询结果、提示词、模型原文仍只写原审计记录。"""
    if chain:
        cid = chain["chain_id"]
        fields = {
            **rt.records.chain_numbers.get(cid, {}),
            "chain_id": cid,
            "l1_id": chain["l1_id"],
            "l2_id": chain["l2_id"],
            "seed_id": chain["seed"]["seed_id"],
            "seed_service": chain["seed"]["service"],
            "revision": chain["revision"],
            "nodes": list(chain["nodes"]),
            "hop": len(chain["nodes"]) - 1,
            **fields,
        }
    emit(kind, case_handle=rt.handle, budget=rt.budget.snapshot(), **fields)


def view(c):
    return {
        k: copy.deepcopy(c[k])
        for k in (
            "chain_id",
            "revision",
            "l1_id",
            "l2_id",
            "seed",
            "nodes",
            "edges",
            "root",
            "fault_diagnosis",
            "symptom",
            "target_symptom",
            "parent_chain_id",
            "parent_revision",
            "status",
        ) if k in c
    }


def save_version(case, c):
    case.rt.records.save(f"l1/{c['chain_id']}/v{c['revision']:03d}.json", view(c))


def change(case, c, **fields):
    c.update(fields)
    c["revision"] += 1
    c["last_review"] = None
    save_version(case, c)


def finish(case, c, status, why):
    c.update(status=status, reason=why)
    case.rt.budget.release(c["chain_id"])
    case.rt.records.save(f"l1/{c['chain_id']}/closure_{len(c['decisions']):03d}_{status}.json", c)
    case.rt.records.save(f"l1/{c['chain_id']}/outcome.json", c)
    case.rt.records.event(
        c["l1_id"],
        "branch_closed",
        {"chain_id": c["chain_id"], "revision": c["revision"], "status": status, "reason": why},
    )
    report(
        case.rt, "chain_closed", chain=c, stage="l1", status=status, reason=why, step_no=len(c["decisions"])
    )
