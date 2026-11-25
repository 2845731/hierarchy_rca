"""Meta 的选择、实际查询和正式答案必须逐项对应；本地评分投影只生成一次。"""

import json
from pathlib import Path
from filesystem import atomic_write_text

from reference import BASELINE  # 同时注册本机参考工程的导入路径
from contracts.agent_output import AgentRCAOutput, FaultKind
from local_answer_contract import LocalAgentRCAOutput

FAULT_KINDS = tuple(item.value for item in FaultKind)
OFFICIAL_CONTRACT = (BASELINE / "contracts/agent_contract.txt").read_text(encoding="utf-8")


class EvidenceSelectionError(ValueError):
    """证据已回放，但当前条目需要改选证据或调整判断。"""

    def __init__(self, path, evidence_refs, reason):
        self.path = path
        super().__init__(f"{reason}。当前引用：{', '.join(evidence_refs)}。请改选证据或调整该项判断")


def save_json(path: Path, data):
    """只替换已完整写入的文件；同一产物只由一个阶段负责写。"""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _items_by_key(items, keys):
    indexed = {tuple(item[k] for k in keys): item for item in items}
    if len(indexed) != len(items):
        raise ValueError(f"同一答案重复列出相同的 {keys}")
    return indexed


def export_answer(case_output: Path, answer: dict, selection: dict, telemetry, chains: list) -> dict:
    """正式结果只允许使用为该根因/边锁定的证据，不接受来自别的条目的碰巧可执行的 SQL。"""
    validated = AgentRCAOutput.model_validate(answer).model_dump(mode="json", by_alias=True)
    services = telemetry.service_names()
    replayed = {}
    for group, keys in (("root_causes", ("service", "fault_kind")), ("propagation", ("from", "to"))):
        locked = _items_by_key(selection[group], keys)
        actual = _items_by_key(validated[group], keys)
        if actual.keys() != locked.keys():
            raise ValueError(f"{group} 与 Meta 已锁定选择不一致，不得在综合阶段增删或改名")
        for key, claim in actual.items():
            names = key[:1] if group == "root_causes" else key
            if any(name not in services for name in names):
                raise ValueError(f"正式结果出现未在遥测中观察到的服务：{names}")
            allowed = set()
            for ref in locked[key]["evidence_refs"]:
                if ref not in replayed:
                    replayed[ref] = telemetry.verify(ref)
                proof = replayed[ref]
                if proof["rows"]:
                    allowed.add((proof["kind"], proof["sql"].strip()))
            for proof in claim["evidence"]:
                if (proof["kind"], proof["sql"].strip()) not in allowed:
                    raise ValueError(f"{key} 的证据必须逐字使用为该判断实际执行且返回行的 SQL")
            if group == "propagation" and not any(
                e["kind"] in ("trace", "metric") for e in claim["evidence"]
            ):
                raise ValueError(f"传播边 {key} 不能只有日志证据")

    return _save_outputs(case_output, validated, selection, chains, replayed)


def export_selected_answer(case_output: Path, selection: dict, telemetry, chains: list, *, synthesized=False):
    """从仲裁选择及逐条复核结果构造本地答案，不调用模型、不生成 claim。"""
    if synthesized and not selection.get("root_causes"):
        raise EvidenceSelectionError("root_causes", [], "最终综合必须提供至少一个有依据的根因")
    services = telemetry.service_names()
    replayed = {}
    answer = {"root_causes": [], "propagation": []}
    for group, keys in (("root_causes", ("service", "fault_kind")), ("propagation", ("from", "to"))):
        locked = _items_by_key(selection[group], keys)
        for index, (key, item) in enumerate(locked.items()):
            names = key[:1] if group == "root_causes" else key
            if any(name not in services for name in names):
                raise ValueError(f"正式结果出现未在遥测中观察到的服务：{names}")
            evidence = []
            # 每个判断内按仲裁证据编号去重；同一证据用于不同判断时分别保留。
            refs = list(dict.fromkeys(item["evidence_refs"]))
            for ref in refs:
                if ref not in replayed:
                    try:
                        replayed[ref] = telemetry.verify(ref)
                    except ValueError as exc:
                        raise ValueError(f"证据 {ref} 无法复核：{exc}") from exc
                proof = replayed[ref]
                if proof["rows"]:
                    evidence.append({"kind": proof["kind"], "sql": proof["sql"]})
            if not evidence:
                _save_replay(case_output, replayed)
                raise EvidenceSelectionError(f"{group}[{index}].evidence_refs", refs,
                                             f"{key} 引用的查询回放均未返回数据")
            if group == "propagation" and not any(e["kind"] in ("trace", "metric") for e in evidence):
                _save_replay(case_output, replayed)
                raise EvidenceSelectionError(f"{group}[{index}].evidence_refs", refs,
                                             f"传播边 {key} 回放后缺少返回数据的指标或调用证据")
            answer[group].append({**dict(zip(keys, key)), "evidence": evidence})
    validated = LocalAgentRCAOutput.model_validate(answer).model_dump(mode="json", by_alias=True)
    return validated, _save_outputs(case_output, validated, selection, chains, replayed, synthesized=synthesized)


def _save_outputs(case_output: Path, validated: dict, selection: dict, chains: list, replayed: dict, *, synthesized=False) -> dict:
    """两种答案共享原有的本地投影和保存方式。"""
    roots = [
        dict(
            service=r["service"],
            fault_kind=r["fault_kind"],
            causal_confirmed=False,
            evidence_ids=r["evidence_refs"],
        )
        for r in selection["root_causes"]
    ]
    edges = [[e["from"], e["to"]] for e in selection["propagation"]]
    selected_ids = set(selection["selected_chain_ids"])
    local = {
        "root_causes": roots,
        "service_nodes": sorted({r["service"] for r in roots} | {s for edge in edges for s in edge}),
        "service_edges": edges,
        "selected_chains": [chain for chain in chains if chain["chain_id"] in selected_ids],
        "causal_confirmed": False,  # L2 审核不是在真实服务上实施干预。
        "output_selection_status": "synthesized" if synthesized else "reviewed",
        "propagation_status": "synthesized" if synthesized else "reviewed",
    }
    _save_replay(case_output, replayed)
    save_json(case_output / "agent_answer.json", validated)
    save_json(case_output / "prediction.json", local)
    return local


def _save_replay(case_output: Path, replayed: dict):
    save_json(
        case_output / "phase4_output/evidence_replay.json",
        {
            ref: {"kind": item["kind"], "sql": item["sql"], "has_rows": bool(item["rows"])}
            for ref, item in replayed.items()
        },
    )
