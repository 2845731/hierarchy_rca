"""Synchronous chat with the reference project's configuration and retry policy.

The historical filename and chat(system, user) interface are retained. Production
uses the experiment's explicit primary/fallback with reference parameters,
explicit retries, and no SDK retry.
A fallback serves one call; the next call starts at the primary again.
"""
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid

from .errors import AllProvidersFailedError

PROJECT_ROOT = Path(__file__).resolve().parents[1]

def filesystem_path(path):
    path = Path(path)
    if os.name == "nt":
        value = os.path.abspath(path)
        if not value.startswith("\\\\?\\"):
            value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
        return Path(value)
    return path

_LOCK = threading.RLock()
_SECRETS = set()
_RECORDS = {}

# Additive fields only: existing usage/audit readers keep their original fields.
USAGE_TOKEN_FIELDS = (
    "prompt_tokens", "completion_tokens", "total_tokens", "provider_cached_tokens",
    "uncached_prompt_tokens", "cache_usage_prompt_tokens", "reasoning_tokens",
)
USAGE_COUNTER_FIELDS = (
    "missing_usage_responses", "cache_usage_responses", "missing_cache_usage_responses",
    "missing_reasoning_usage_responses",
)
TIMING_FIELDS = ("queue_wait_seconds", "http_seconds")


def redact(text):
    """Safe for both terminal text and a final exception traceback."""
    value = str(text)
    with _LOCK:
        secrets = tuple(_SECRETS)
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+", r"\1[REDACTED]", value)
    return "".join(c if c in "\n\t" or ord(c) >= 32 and ord(c) != 127 else "?" for c in value)


def _event(icon, message):
    with _LOCK:
        stamp = datetime.now().strftime("%H:%M:%S")
        case = os.environ.get("PEARLRCA_CASE_NAME", "单次调用")
        stage = os.environ.get("PEARLRCA_RUN_STAGE", "LLM")
        text = redact(f"[{stamp}] [{case}] [{stage}] {icon} {message}")
        try:
            print(text, flush=True)
        except (OSError, UnicodeError, ValueError):
            pass  # A display failure cannot change a diagnosis.


def _redact_payload(value):
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return redact(value) if isinstance(value, str) else value


def _reported_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _accumulate_usage(record, usage):
    """Count reported fields without treating missing provider cache data as zero hits."""
    if isinstance(usage, dict):
        total = record.setdefault("usage", None)
        if total is None:
            total = record["usage"] = {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(field)
            if _reported_number(value):
                total[field] = total.get(field, 0) + value
        prompt = usage.get("prompt_tokens")
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if _reported_number(prompt) and _reported_number(cached) and cached <= prompt:
            for field, value in (("provider_cached_tokens", cached),
                                 ("uncached_prompt_tokens", prompt - cached),
                                 ("cache_usage_prompt_tokens", prompt)):
                total[field] = total.get(field, 0) + value
            record["cache_usage_responses"] += 1
        else:
            record["missing_cache_usage_responses"] += 1
        details = usage.get("completion_tokens_details")
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if _reported_number(reasoning):
            total["reasoning_tokens"] = total.get("reasoning_tokens", 0) + reasoning
        else:
            record["missing_reasoning_usage_responses"] += 1
    else:
        record["missing_cache_usage_responses"] += 1
        record["missing_reasoning_usage_responses"] += 1
    if not isinstance(usage, dict) or not _reported_number(usage.get("total_tokens")):
        record["missing_usage_responses"] += 1


def _write_audit_json(path, value):
    """Preserve the existing atomic, long-path-aware audit write behavior."""
    path = filesystem_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ("." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for retry in range(5):
        try:
            temporary.replace(path)
            break
        except OSError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or retry == 4:
                _event("⚠️", f"审计文件替换失败 | {type(exc).__name__} | winerror={getattr(exc, 'winerror', None)} | 临时文件 {temporary} | 目标文件 {path}")
                raise
            time.sleep(0.1 * (2 ** retry))


def read_audit_record(path):
    """Read a completed legacy/full record or reconstruct the latest interrupted state."""
    path = filesystem_path(path)
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    if record.get("audit_final") is True:
        return record  # Terminal snapshots are complete even if an old sidecar was damaged later.
    state_path = path.parent / "_states" / path.name
    if state_path.is_file():
        update = json.loads(state_path.read_text(encoding="utf-8-sig"))
        if update.get("call_id") != record.get("call_id"):
            raise ValueError("审计状态与请求编号不一致")
        if update.get("audit_revision", 0) > record.get("audit_revision", 0):
            if "system" in update or "user" in update:
                raise ValueError("中间审计状态不能改写请求正文")
            record.update(update)
    return record


class _AuditWriter:
    """Write large request text at start/end; intermediate states contain no prompt copy."""
    def __init__(self, folder, call_id):
        self.path = filesystem_path(Path(folder) / (call_id + ".json")) if folder else None
        self.started = False
        self.request_text = None

    def write(self, record, *, final=False):
        if self.path is None:
            return
        if self.request_text is None:
            self.request_text = _redact_payload({"system": record["system"], "user": record["user"]})
        state = _redact_payload({key: value for key, value in record.items() if key not in {"system", "user"}})
        if not self.started or final:
            _write_audit_json(self.path, {**state, **self.request_text})
            self.started = True
        else:
            _write_audit_json(self.path.parent / "_states" / self.path.name, state)


def runtime_summary(attempt_id=None):
    """In-process totals; batch readers use matching llm_calls/*.json fields."""
    attempt_id = attempt_id or os.environ.get("PEARLRCA_RUN_ATTEMPT_ID")
    with _LOCK:
        rows = [dict(row) for row in _RECORDS.values() if row.get("attempt_id") == attempt_id]
    usage_rows = [row for row in rows if isinstance(row.get("usage"), dict)]
    totals = {key: sum((row["usage"].get(key) or 0) for row in usage_rows) for key in USAGE_TOKEN_FIELDS}
    return {"logical_calls": len({row["logical_call_id"] for row in rows}),
            "request_attempts": sum(row.get("api_requests", 0) for row in rows),
            "retries": sum(row.get("transport_retry_count", 0) or 0 for row in rows),
            "cache_hits": sum(bool(row.get("cache_hit")) for row in rows),
            "fallback_calls": len({row["logical_call_id"] for row in rows if row.get("fallback_used")}),
            **{key: sum(row.get(key, 0) for row in rows) for key in USAGE_COUNTER_FIELDS},
            **{key: round(sum(row.get(key, 0) for row in rows), 6) for key in TIMING_FIELDS},
            "failed_requests": sum(sum(a.get("status") == "failed" for a in row.get("request_attempts", [])) for row in rows),
            **totals, "usage_note": "仅累计接口已报告用量；cache_hits 是本地整答缓存，provider_cached_tokens 是服务端缓存；未命中量仅累计有完整缓存字段的响应，分母为 cache_usage_prompt_tokens；reasoning_tokens 已包含于输出量，不额外相加；缺失用量不推算费用",
            "timing_note": "queue_wait_seconds 为请求槽排队；http_seconds 为 SDK 调用；elapsed_seconds 仍包含本地开销、排队和重试等待"}


@contextmanager
def request_slot():
    """Shared account-level request slots; keep four unless explicitly configured."""
    raw_limit = os.environ.get("PEARLRCA_LLM_MAX_CONCURRENT_REQUESTS", "4")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise ValueError("HTTP 并发请求槽数量必须是 1 至 64 的整数") from None
    if not 1 <= limit <= 64:
        raise ValueError("HTTP 并发请求槽数量必须是 1 至 64 的整数")
    folder = Path(os.environ.get("PEARLRCA_LLM_SLOT_DIR") or PROJECT_ROOT / ".runtime/request_slots")
    folder.mkdir(parents=True, exist_ok=True)
    handle = None
    while handle is None:
        for index in range(limit):
            candidate = (folder / f"slot-{index}.lock").open("a+b")
            candidate.seek(0, 2)
            if candidate.tell() == 0:
                candidate.write(b"0")
                candidate.flush()
            candidate.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(candidate.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle = candidate
                break
            except OSError:
                candidate.close()
        if handle is None:
            time.sleep(0.1)
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def temporary_request_error(exc):
    """Reference retry.py: API failures retry; local/content failures do not."""
    from openai import APIError
    return isinstance(exc, APIError)


def _is_provider_level_error(exc):
    return temporary_request_error(exc)


def final_answer(content):
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LLM 返回了空内容")
    text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()
    if re.search(r"</?think>", text, flags=re.IGNORECASE):
        raise RuntimeError("LLM 思考内容未完整结束；不能将其当成最终答案")
    if not text:
        raise RuntimeError("LLM 只返回了思考内容，没有最终答案")
    return text


class _LazyTransport:
    def __init__(self, settings, key):
        self.settings, self.key, self._sdk = settings, key, None
        self._lock = threading.Lock()

    @property
    def chat(self):
        with self._lock:
            if self._sdk is None:
                if not self.key:
                    raise ValueError(f"缺少环境变量 {self.settings['api_key_env']}；未发起请求")
                from openai import OpenAI
                self._sdk = OpenAI(api_key=self.key, base_url=self.settings["base_url"],
                                   timeout=self.settings["timeout_seconds"], max_retries=0)
            return self._sdk.chat


class MiniMaxClient:
    """One provider; constructor remains usable by explicit local SDK callers."""
    def __init__(self, sdk, model, max_tokens=8192, provider="minimax", extra_body=None, settings=None, usable=True):
        self.sdk, self.model, self.max_tokens = sdk, model, max_tokens
        self.provider, self.extra_body = provider, extra_body
        self.settings, self.usable = settings, usable

    def chat(self, system, user, *, logical_call_id=None, fallback_used=False):
        from shared.time_grid import bucket_seconds
        logical_call_id = logical_call_id or uuid.uuid4().hex
        system += f"\n时间字段统一为 UTC；lag_min/history_lag_min 是网格步数，每步 {bucket_seconds()} 秒。ServiceTest 是入口观测的聚合症状，不是真实服务。"
        case_name = os.environ.get("PEARLRCA_CASE_NAME", "")
        if case_name:
            system, user = system.replace(case_name, "anonymous-case"), user.replace(case_name, "anonymous-case")
        stage = os.environ.get("PEARLRCA_RUN_STAGE", "")
        settings = self.settings
        if settings is not None:
            phase = "synthesis" if stage in ("answer", "meta") else "investigation"
            maximum = settings.get(f"{phase}_max_tokens")
            retries = settings["max_retries"]
            initial, cap = settings["retry_initial_seconds"], settings["retry_max_seconds"]
            temperature = settings["temperature"]
        else:
            # Compatibility only: production load_client never uses this branch.
            maximum = self.max_tokens
            retries = max(0, min(30, int(os.environ.get("PEARLRCA_LLM_MAX_RETRIES", "0"))))
            initial, cap, temperature = 2, 30, None
        request = {"model": self.model, "messages": [{"role": "system", "content": system},
                                                        {"role": "user", "content": user}]}
        if maximum is not None:
            request["max_tokens"] = maximum
        if temperature is not None:
            request["temperature"] = temperature
        if self.extra_body:
            request["extra_body"] = self.extra_body
        # New namespace and full request prevent reuse of old generation settings.
        key = hashlib.sha256(json.dumps(["baseline-v1", self.provider, request], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cache_dir = os.environ.get("PEARLRCA_LLM_CACHE_DIR")
        cache = filesystem_path(Path(cache_dir) / (key + ".json")) if cache_dir else None
        started = time.perf_counter()
        record = {"schema_version": "llm-call-audit-v4", "call_id": uuid.uuid4().hex,
                  "logical_call_id": logical_call_id, "attempt_id": os.environ.get("PEARLRCA_RUN_ATTEMPT_ID"),
                  "stage": stage, "started_utc": datetime.now(timezone.utc).isoformat(),
                  "provider": self.provider, "model": self.model, "max_tokens": maximum,
                  "temperature": temperature, "system": system, "user": user, "cache_key": key,
                  "cache_hit": False, "api_requests": 0, "usage": None, "response_received": False,
                  "response_count": 0, **dict.fromkeys(USAGE_COUNTER_FIELDS, 0),
                  "queue_wait_seconds": 0.0, "http_seconds": 0.0, "audit_revision": 0,
                  "fallback_used": fallback_used, "transport_retry_count": 0,
                  "transport_retry_note": "Explicit provider-local retries; production SDK retries disabled",
                  "request_attempts": []}

        audit = _AuditWriter(os.environ.get("PEARLRCA_LLM_AUDIT_DIR"), record["call_id"])

        def persist(status, *, final=False, **extra):
            record["audit_revision"] += 1
            record.update(status=status, audit_final=final,
                          elapsed_seconds=round(time.perf_counter() - started, 6), **extra)
            with _LOCK:
                _RECORDS[record["call_id"]] = dict(record)
            audit.write(record, final=final)

        if cache is not None and cache.is_file():
            try:
                answer = final_answer(json.loads(cache.read_text(encoding="utf-8"))["answer"])
            except Exception as exc:
                persist("cache_error", final=True, error_type=type(exc).__name__)
                raise
            persist("completed", final=True, cache_hit=True, answer=answer)
            _event("📦", f"缓存命中 | {self.provider} / {self.model} | 本次不发起接口请求")
            return answer
        try:
            for attempt in range(retries + 1):
                record["transport_retry_count"] = attempt
                request_started = time.perf_counter()
                response_usage = None
                _event("🤖", f"调用 {self.provider} / {self.model} | 第 {attempt + 1}/{retries + 1} 次尝试"
                       f" | 超时 {settings['timeout_seconds'] if settings else '由客户端配置'} 秒 | 最大输出 {maximum if maximum is not None else '接口默认'}")
                persist("waiting_for_slot")
                queue_started = time.perf_counter()
                queue_wait = http_seconds = 0.0
                slot_acquired = False
                try:
                    with request_slot():
                        slot_acquired = True
                        queue_wait = time.perf_counter() - queue_started
                        record["queue_wait_seconds"] += queue_wait
                        record["api_requests"] += 1
                        persist("requesting")
                        http_started = time.perf_counter()
                        try:
                            response = self.sdk.chat.completions.create(**request)
                        finally:
                            http_seconds = time.perf_counter() - http_started
                            record["http_seconds"] += http_seconds
                    usage = getattr(response, "usage", None)
                    response_usage = usage.model_dump(mode="json") if hasattr(usage, "model_dump") else usage if isinstance(usage, dict) else None
                    _accumulate_usage(record, response_usage)
                    record["response_count"] += 1
                    record["response_received"] = True
                    record["returned_model"] = getattr(response, "model", None)
                    base = getattr(response, "base_resp", None) or {}
                    if hasattr(base, "model_dump"):
                        base = base.model_dump()
                    if isinstance(base, dict) and base.get("status_code") not in (None, 0):
                        from openai import APIError
                        import httpx
                        raise APIError(f"{self.provider} returned provider error code={base.get('status_code')}",
                                       httpx.Request("POST", settings["base_url"] if settings else "https://provider.invalid"), body=None)
                    if not response.choices:
                        raise RuntimeError(f"{self.provider} 未返回回复；停止当前调用")
                    record["request_attempts"].append({"number": attempt + 1, "status": "completed",
                        "usage": response_usage,
                        "queue_wait_seconds": round(queue_wait, 6), "http_seconds": round(http_seconds, 6),
                        "elapsed_seconds": round(time.perf_counter() - request_started, 6)})
                    persist("response_received")
                    break
                except Exception as exc:
                    if not slot_acquired:
                        queue_wait = time.perf_counter() - queue_started
                        record["queue_wait_seconds"] += queue_wait
                    retryable = temporary_request_error(exc)
                    if settings is None:
                        # Explicit legacy SDK callers retain their old 401/403
                        # behavior; reference-aligned production retries APIError.
                        from openai import APIConnectionError
                        status = getattr(exc, "status_code", None)
                        retryable = (isinstance(exc, APIConnectionError) or status in (408, 409, 429)
                                     or isinstance(status, int) and 500 <= status < 600)
                    will_retry = retryable and attempt < retries
                    delay = min(initial * 2 ** attempt, cap) if will_retry else 0
                    code = getattr(exc, "status_code", None)
                    record["last_provider_error"] = {"error_type": type(exc).__name__, "status_code": code}
                    record["request_attempts"].append({"number": attempt + 1, "status": "failed",
                        "usage": response_usage,
                        "error_type": type(exc).__name__, "status_code": code,
                        "queue_wait_seconds": round(queue_wait, 6), "http_seconds": round(http_seconds, 6),
                        "elapsed_seconds": round(time.perf_counter() - request_started, 6),
                        "retry_wait_seconds": delay, "retry_scheduled": will_retry})
                    persist("retrying" if will_retry else "failed", error_type=type(exc).__name__, status_code=code)
                    _event("❌", f"{self.provider} / {self.model} 调用失败 | {type(exc).__name__}"
                           f" | HTTP {code if code is not None else '无状态码'} | 耗时 {time.perf_counter() - request_started:.1f} 秒")
                    if not will_retry:
                        raise
                    _event("🔄", f"{delay:g} 秒后重连同一接口 | 重试 {attempt + 1}/{retries} | {self.model}")
                    time.sleep(delay)
            choice = response.choices[0]
            record["finish_reason"] = getattr(choice, "finish_reason", None)
            if record["finish_reason"] in ("length", "content_filter"):
                raise RuntimeError(f"{self.provider} 输出被截断或被过滤；不能用于诊断")
            if getattr(choice.message, "refusal", None):
                raise RuntimeError(f"{self.provider} 拒绝回答")
            answer = final_answer(choice.message.content)
        except Exception as exc:
            try:
                persist("failed", final=True, error_type=type(exc).__name__)
            except OSError as audit_error:
                _event("⚠️", f"记录失败状态时审计写入失败 | {type(audit_error).__name__}: {audit_error}")
            raise
        persist("completed", final=True, answer=answer)
        total = (record["usage"] or {}).get("total_tokens")
        _event("✅", f"{self.provider} / {self.model} 返回完成 | 耗时 {record['elapsed_seconds']:.1f} 秒"
               f" | 接口报告token {total if total is not None else '未知'} | 正文 {len(answer)} 字符")
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_name(cache.name + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(json.dumps({"model": self.model, "answer": answer}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(cache)
        return answer


class FallbackClient:
    def __init__(self, providers, max_rounds=1):
        if not providers:
            raise ValueError("FallbackClient 至少需要一个通道")
        self.providers = providers
        self.max_rounds = max(1, int(max_rounds))  # Explicit legacy callers only.
        self.model = providers[0].model

    def chat(self, system, user):
        logical_id = uuid.uuid4().hex
        errors = []
        for round_no in range(self.max_rounds):
            for index, client in enumerate(self.providers):
                if index and not client.usable:
                    _event("⚠️", f"备用通道 {client.provider} 未配置密钥，不能接管本次请求")
                    continue
                try:
                    return client.chat(system, user, logical_call_id=logical_id, fallback_used=index > 0)
                except Exception as exc:
                    if not _is_provider_level_error(exc):
                        raise
                    errors.append(f"{client.provider}/{client.model}: {type(exc).__name__} HTTP {getattr(exc, 'status_code', None)}")
                    next_client = next((item for item in self.providers[index + 1:] if item.usable), None)
                    if next_client is not None:
                        _event("🚨", "!" * 60)
                        _event("🔀", f"{client.model} 重试额度已用完 → 切换 {next_client.provider} / {next_client.model}"
                               "；仅接管当前调用，下次调用返回主模型")
                        _event("🚨", "!" * 60)
        report = "所有已授权LLM通道重试耗尽，停止当前案例和后续批次。" + "；".join(errors)
        _event("🛑", report)
        raise AllProvidersFailedError(redact(report)) from None


