"""Exact, reversible deduplication of model input; never selects or summarizes evidence.

Only repeated JSON containers are replaced. Pool objects contain their complete
original values, so the model needs at most one lookup to read an occurrence.
Identifiers are transport metadata, not evidence references or access grants.
"""

from collections import Counter
import hashlib
import json


POOL_KEY = "$rca_objects"
FORMAT_KEY = "$rca_transport"
REF_KEY = "$rca_ref"
FORMAT_VERSION = "exact_objects_v1"
RESERVED_KEYS = frozenset((POOL_KEY, FORMAT_KEY, REF_KEY))

class _Context(dict):
    """Carry an encoding flag outside JSON, so literal reserved data stays data."""

    def __init__(self, value, *, encoded):
        super().__init__(value)
        self.encoded = encoded


TRANSPORT_INSTRUCTION = (
    "\n若输入含对象池，则按可逆对象去重读取：$rca_objects 保存完整原对象，"
    "{\"$rca_ref\":\"编号\"} 在原位置代表该编号对应的全部内容，请展开理解；"
    "对象池不是摘要，引用不新增证据，也不改变原位置的会话权限或事实/主张属性。"
    "$rca_ref 仅用于读取输入，不是证据编号；输出仍须使用原动作协议、真实证据编号或规定的证据短编号，"
    "不得输出这些引用标记。$rca_transport 只是传输版本。"
)


def _dump(value):
    # Match the existing model serializer, including its non-finite-value check.
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False, separators=(",", ":"))


def _contains_reserved(value):
    if isinstance(value, dict):
        return bool(RESERVED_KEYS.intersection(value)) or any(_contains_reserved(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_reserved(v) for v in value)
    return False


def _object_id(text):
    return "o" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_encoded_context(payload):
    """Whether encode_context encoded this mapping, rather than a key guess."""
    return isinstance(payload, _Context) and payload.encoded


def encode_context(payload, *, enabled=True, min_chars=240, min_savings=64):
    """Return a shorter, losslessly encoded request or its unchanged JSON values.

    The top-level request remains a mapping. Its original fields and their order
    are retained; only repeated values within them may become references. The
    pool and format marker are appended. The size gate includes the instruction
    needed to explain the format and requires savings in both characters and
    UTF-8 bytes; this is deliberately not presented as a tokenizer estimate.

    Existing reserved keys disable encoding, preventing accidental interpretation
    of application data as our markers. Unsupported/cyclic/non-finite inputs
    retain the existing JSON serializer's error behavior.
    """
    if not enabled:
        return payload
    if not isinstance(payload, dict):
        raise TypeError("context payload must be a mapping")
    if type(min_chars) is not int or min_chars < 1 or type(min_savings) is not int or min_savings < 0:
        raise ValueError("min_chars must be positive and min_savings must be nonnegative integers")

    original_text = _dump(payload)
    normalized = json.loads(original_text)
    if _contains_reserved(normalized):
        return _Context(normalized, encoded=False)

    counts, values = Counter(), {}

    def collect(value):
        if not isinstance(value, (dict, list)):
            return
        text = _dump(value)
        if len(text) >= min_chars:
            counts[text] += 1
            values.setdefault(text, value)
        for child in value.values() if isinstance(value, dict) else value:
            collect(child)

    # The request itself is never replaced: callers retain top-level access.
    for value in normalized.values():
        collect(value)
    candidates = {text for text, count in counts.items() if count >= 2}
    if not candidates:
        return normalized

    identifiers, occupied = {}, {}
    for text in candidates:
        identifier = _object_id(text)
        if identifier in occupied and occupied[identifier] != text:
            # Even a hash collision must not alias distinct evidence or claims.
            return normalized
        identifiers[text] = identifier
        occupied[identifier] = text

    def render():
        pool, used = {}, Counter()

        def replace(value):
            if not isinstance(value, (dict, list)):
                return value
            text = _dump(value)
            if text in candidates:
                identifier = identifiers[text]
                # Originals deliberately contain no nested transport references.
                pool.setdefault(identifier, values[text])
                used[text] += 1
                return {REF_KEY: identifier}
            if isinstance(value, dict):
                return {key: replace(child) for key, child in value.items()}
            return [replace(child) for child in value]

        result = {key: replace(value) for key, value in normalized.items()}
        result[FORMAT_KEY] = FORMAT_VERSION
        result[POOL_KEY] = pool
        return result, used

    # Replacing a repeated parent hides its children. Do not keep a child pool
    # entry that now has only one visible use; the complete child is already in
    # the parent's original value. Removing a candidate can reveal more children.
    while candidates:
        encoded, used = render()
        singletons = {text for text in candidates if used[text] < 2}
        if not singletons:
            break
        candidates.difference_update(singletons)
    else:
        return normalized

    encoded_text = _dump(encoded)
    instruction_chars = len(TRANSPORT_INSTRUCTION)
    instruction_bytes = len(TRANSPORT_INSTRUCTION.encode("utf-8"))
    if (len(original_text) - len(encoded_text) - instruction_chars < min_savings
            or len(original_text.encode("utf-8")) - len(encoded_text.encode("utf-8"))
            - instruction_bytes < min_savings):
        return normalized
    return _Context(encoded, encoded=True)


def expand_context(payload, *, encoded=None):
    """Expand a known transport payload for offline equivalence checks.

    This function is not used to accept model actions or to bypass any evidence
    validation. Malformed references and pools are rejected, never guessed.
    Ordinary unencoded payloads are returned as independent JSON-compatible data.
    Pass encoded=False for known literal JSON after serialization, even if it
    contains all reserved fields. Pass encoded=True for known transport JSON;
    its version and pool are then mandatory. The default uses the in-memory
    encoder flag, or detects metadata for known parsed transport convenience.
    Arbitrary external JSON must use the explicit flag because reserved names
    alone cannot distinguish application data from an encoded payload.
    """
    if not isinstance(payload, dict):
        raise TypeError("context payload must be a mapping")
    if encoded is not None and type(encoded) is not bool:
        raise TypeError("encoded must be True, False, or None")
    normalized = json.loads(_dump(payload))
    if encoded is None and isinstance(payload, _Context):
        encoded = payload.encoded
    if encoded is False:
        return normalized
    if normalized.get(FORMAT_KEY) != FORMAT_VERSION or POOL_KEY not in normalized:
        if encoded is True:
            raise ValueError("encoded context requires the expected transport version and object pool")
        return normalized
    pool = normalized[POOL_KEY]
    if not isinstance(pool, dict) or not pool:
        raise ValueError("transport object pool must be a nonempty mapping")
    for identifier, value in pool.items():
        if not isinstance(identifier, str) or not identifier or not isinstance(value, (dict, list)):
            raise ValueError("transport pool requires named complete JSON containers")
        if _contains_reserved(value):
            raise ValueError("transport pool must contain originals without nested references or cycles")

    used = set()

    def expand(value):
        if isinstance(value, dict):
            if REF_KEY in value:
                if set(value) != {REF_KEY} or not isinstance(value[REF_KEY], str):
                    raise ValueError("malformed transport reference")
                identifier = value[REF_KEY]
                if identifier not in pool:
                    raise ValueError("unknown transport object reference")
                used.add(identifier)
                # Every occurrence gets an independent copy, as before JSON
                # serialization. An edit in one position cannot affect another.
                return json.loads(_dump(pool[identifier]))
            if RESERVED_KEYS.intersection(value):
                raise ValueError("unexpected transport metadata inside payload")
            return {key: expand(child) for key, child in value.items()}
        if isinstance(value, list):
            return [expand(child) for child in value]
        return value

    result = {key: expand(value) for key, value in normalized.items() if key not in (FORMAT_KEY, POOL_KEY)}
    if used != set(pool):
        raise ValueError("transport object pool contains unreferenced objects")
    return result
