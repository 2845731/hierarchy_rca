"""本版本的无 claim 答案格式；论文原格式仍由参考工程单独提供。"""

from typing import Literal

from pydantic import Field
from reference import BASELINE  # noqa: F401，注册参考格式的导入路径
from contracts.agent_output import FaultKind, StrictModel


LEGACY_FORMAT = "paper_agent_rca_v2"
LOCAL_FORMAT = "local_no_claim_v1"


class LocalEvidence(StrictModel):
    kind: Literal["metric", "trace", "log"]
    sql: str = Field(min_length=1)


class LocalRootCause(StrictModel):
    service: str = Field(min_length=1)
    fault_kind: FaultKind
    evidence: list[LocalEvidence] = Field(min_length=1)


class LocalPropagation(StrictModel):
    from_service: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)
    evidence: list[LocalEvidence] = Field(min_length=1)


class LocalAgentRCAOutput(StrictModel):
    root_causes: list[LocalRootCause]
    propagation: list[LocalPropagation]
