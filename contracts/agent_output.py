from enum import Enum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class FaultKind(str, Enum):
    pod_failure = "pod_failure"
    pod_unavailable = "pod_unavailable"
    network_delay = "network_delay"
    network_loss = "network_loss"
    network_partition = "network_partition"
    network_corrupt = "network_corrupt"
    network_duplicate = "network_duplicate"
    network_bandwidth_limit = "network_bandwidth_limit"
    http_aborted = "http_aborted"
    http_slow = "http_slow"
    http_payload_modified = "http_payload_modified"
    http_response_status_modified = "http_response_status_modified"
    cpu_stress = "cpu_stress"
    jvm_thread_cpu_stress = "jvm_thread_cpu_stress"
    mem_stress = "mem_stress"
    jvm_heap_stress = "jvm_heap_stress"
    jvm_gc_pressure = "jvm_gc_pressure"
    jvm_method_exception = "jvm_method_exception"
    jvm_jdbc_exception = "jvm_jdbc_exception"
    jvm_method_latency = "jvm_method_latency"
    jvm_jdbc_latency = "jvm_jdbc_latency"
    jvm_method_mutated = "jvm_method_mutated"
    dns_resolution_failed = "dns_resolution_failed"
    dns_resolution_wrong = "dns_resolution_wrong"
    clock_skew = "clock_skew"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(StrictModel):
    kind: Literal["metric", "trace", "log"]
    sql: str = Field(min_length=1)
    claim: str = Field(min_length=1)

    @field_validator("claim")
    @classmethod
    def word_limit(cls, value):
        if len(value.split()) > 20:
            raise ValueError("claim exceeds 20 whitespace-delimited words")
        return value


class RootCause(StrictModel):
    service: str = Field(min_length=1)
    fault_kind: FaultKind
    evidence: list[Evidence] = Field(min_length=1)


class Propagation(StrictModel):
    from_service: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)


class AgentRCAOutput(StrictModel):
    root_causes: list[RootCause]
    propagation: list[Propagation]
