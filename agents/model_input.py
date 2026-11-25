"""模型输入的表示层；不改变状态、证据权限、工具调用或决策流程。"""

import json

from .context_transport import (
    FORMAT_KEY, POOL_KEY, RESERVED_KEYS, TRANSPORT_INSTRUCTION, encode_context, is_encoded_context,
)


# 只重排最外层字段。数组顺序、嵌套字段顺序及所有值保持不变。
STABLE_FIELDS = (
    "fault_kinds", "catalog", "incident_description", "case_handle",
    "symptoms", "seed_and_target_samples", "evidence_samples", "working_set",
    "investigation_summary", "history", "available_refs", "available_evidence_choices",
)
DYNAMIC_FIELDS = (
    "mode", "chain", "last_review", "previous_review_context", "review_request",
    "feedback", "branch_frontier", "family_coverage", "sibling_chain_ids", "limits",
    "budget", "final_action_only", "query_control", "allowed_control_actions",
    "phase", "allowed_actions", "lookup_calls_used", "lookup_calls_max",
    "formal_attempts_used", "formal_attempts_max", "meta_returns_left",
    "previous_answer", "validation_feedback", "format_feedback", "original_objects",
    FORMAT_KEY, POOL_KEY, "本轮纠错指令",
)


def prefix_order(payload):
    """稳定前缀布局；保留未知字段及其相对顺序，输入对象不被修改。"""
    result = {key: payload[key] for key in STABLE_FIELDS if key in payload}
    result.update((key, value) for key, value in payload.items()
                  if key not in result and key not in DYNAMIC_FIELDS)
    result.update((key, payload[key]) for key in DYNAMIC_FIELDS if key in payload)
    return result


def model_context(payload, cfg):
    """同请求内精确去重并排列字段，关闭开关时保留原序列化行为。"""
    if getattr(cfg, "context_prefix_cache", True):
        payload = prefix_order(payload)
    if getattr(cfg, "context_exact_dedup", True):
        payload = encode_context(payload)
    if is_encoded_context(payload) and "本轮纠错指令" in payload:
        # 对象池之后仍保留末尾纠错提示；不触碰未编码的原始对象。
        payload["本轮纠错指令"] = payload.pop("本轮纠错指令")
    return payload


def _has_reserved_keys(value):
    if isinstance(value, dict):
        return bool(RESERVED_KEYS.intersection(value)) or any(_has_reserved_keys(v) for v in value.values())
    return isinstance(value, list) and any(_has_reserved_keys(v) for v in value)


def context_system_instruction(cfg, payload=None):
    if not getattr(cfg, "context_exact_dedup", True):
        return ""
    if payload is not None and not is_encoded_context(payload) and _has_reserved_keys(payload):
        return ("\n本轮未启用对象去重。输入中的 $rca_transport、$rca_objects、$rca_ref "
                "均为原始数据中的字面字段，不是传输引用；不得展开或据此新增证据权限。")
    if payload is not None and not is_encoded_context(payload):
        return ""  # 小请求不为未采用的格式付出额外说明成本。
    # 原角色规则和工具说明保持前缀；只为实际采用的格式追加固定说明。
    return "\n" + TRANSPORT_INSTRUCTION


def render_context(payload, cfg):
    prepared = model_context(payload, cfg)
    serialized = json.dumps(prepared, ensure_ascii=False, default=str,
                            allow_nan=False, separators=(",", ":"))
    return serialized, context_system_instruction(cfg, prepared)


def serialize_context(payload, cfg):
    return render_context(payload, cfg)[0]
