from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .util import digest

class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    @property
    def digest(self): return digest(self)

class Subject(str, Enum):
    SOURCE = "SOURCE"
    BUILD = "BUILD"
    ARTIFACT = "ARTIFACT"

class Authority(str, Enum):
    EXTERNAL = "TRUSTED_EXTERNAL"
    BUILD = "TRUSTED_BUILD"
    IN_PROCESS = "SANDBOX_IN_PROCESS"
    SELF = "AGENT_SELF_REPORTED"
    HUMAN = "TRUSTED_HUMAN"

class Budget(Value):
    model_calls: int | None = Field(30, gt=0, le=1000)
    tool_calls: int = Field(100, gt=0, le=10000)
    wall_seconds: float = Field(600, gt=0, le=86400)
    output_bytes: int = Field(65536, gt=0, le=16777216)
    child_calls: int = Field(3, ge=0, le=16)

class Requirement(Value):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    critical: bool = True
    subjects: list[Subject] = Field(default_factory=lambda: [Subject.SOURCE])
    authorities: list[Authority] = Field(default_factory=lambda: [Authority.EXTERNAL, Authority.HUMAN])

class Check(Value):
    id: str = Field(min_length=1)
    kind: Literal["cli", "function", "asgi", "static_python", "build", "artifact_function", "file_json", "diagnostic", "human"]
    subject: Subject = Subject.SOURCE
    definition: dict[str, Any] = Field(default_factory=dict)
    protected: bool = True
    active: bool = True
    # Frozen per-check authority, independent of requirement coverage.
    permitted_authorities: list[Authority] = Field(default_factory=list)
    allowed_skips: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def authority_defaults(cls, value):
        if isinstance(value, dict) and "permitted_authorities" not in value:
            default = {"human": Authority.HUMAN, "build": Authority.BUILD}.get(value.get("kind"), Authority.EXTERNAL)
            return {**value, "permitted_authorities": [default]}
        return value

    timeout: float = Field(20, gt=0, le=600)
    @model_validator(mode="after")
    def subject_check(self):
        if not self.permitted_authorities or len(set(self.permitted_authorities)) != len(self.permitted_authorities):
            raise ValueError("permitted_authorities must be nonempty and unique")
        if self.kind == "build" and self.subject != Subject.BUILD: raise ValueError("build requires BUILD")
        if self.kind == "artifact_function" and self.subject != Subject.ARTIFACT: raise ValueError("artifact function requires ARTIFACT")
        if self.kind not in {"build", "artifact_function", "human"} and self.subject != Subject.SOURCE: raise ValueError("source checker requires SOURCE")
        return self

class Contract(Value):
    revision: int = Field(1, ge=1)
    status: Literal["DRAFT", "REVIEWABLE", "FROZEN"] = "DRAFT"
    requirements: list[Requirement]
    checks: list[Check]
    required_checks: list[str]
    global_required_checks: list[str] = Field(default_factory=list)
    coverage: dict[str, list[str]]
    allowed_paths: list[str] = Field(default_factory=lambda: ["**"])
    forbidden_paths: list[str] = Field(default_factory=lambda: [".git", ".git/**", ".repo-code", ".repo-code/**"])
    protected_paths: list[str] = Field(default_factory=list)
    max_changed_files: int = Field(100, ge=0, le=10000)
    deliverable: Literal["source", "artifact"] = "source"
    interface: dict[str, Any] = Field(default_factory=dict)
    reviewer: str | None = None

class TaskSpec(Value):
    task_id: str
    revision: int = Field(1, ge=1)
    goal: str = Field(min_length=1, max_length=100000)
    contract_digest: str | None
    exploration_only: bool = False

class ToolCall(Value):
    id: str
    name: str
    arguments: dict[str, Any]
    argument_error: str | None = None
    raw_arguments: str | None = None

class Assistant(Value):
    text: str = ""
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    stop_reason: str = "stop"
    native_blocks: list[dict[str, Any]] = Field(default_factory=list)

class OperationRequest(Value):
    protocol_version: Literal[1] = 1
    run_id: str
    turn_id: str
    tool_call_uid: str
    tool_name: str
    tool_spec_digest: str
    arguments: dict[str, Any]
    expected_state_digest: str | None = None
    approval_scope_digest: str | None = None
    @property
    def key(self): return digest([self.run_id, self.turn_id, self.tool_call_uid])
    @property
    def semantics(self): return self.digest

class VerificationIntent(Value):
    run_id: str
    task_spec_revision: int
    contract_digest: str
    release_digest: str
    knowledge_snapshot_digest: str
    knowledge_index_digest: str
    environment_digest: str
    control_epoch: int
    run_revision: int
    writer_epoch: int
    source_digest: str
    required_check_set_digest: str
    protected_baseline: str

class Evidence(Value):
    id: str
    run_id: str
    task_spec_revision: int
    contract_digest: str
    release_digest: str
    knowledge_snapshot_digest: str
    knowledge_index_digest: str
    environment_digest: str
    check_id: str
    definition_digest: str
    subject: Subject
    authority: Authority
    status: Literal["PASSED", "FAILED", "INVALID", "INCONCLUSIVE"]
    source_before: str | None = None
    source_after: str | None = None
    build_input_source: str | None = None
    built_artifact: str | None = None
    tested_artifact: str | None = None
    expected_ids: list[str] = Field(default_factory=list)
    executed_ids: list[str] = Field(default_factory=list)
    skipped_ids: list[str] = Field(default_factory=list)
    report_digest: str
    grader_digest: str
    config_digest: str
    signature: str = ""

class PreparedDecision(Value):
    intent_digest: str
    verified: bool
    reason: str
    evidence_ids: list[str]
    artifact_digest: str | None = None
    signature: str = ""

class Release(Value):
    protocol_version: Literal[1] = 1
    components: dict[str, str]
    base_digest: str | None
    worker_build_digest: str
    dependencies_digest: str
    gate_policy_digest: str
    build_attestation_digest: str
    generation: int = Field(0, ge=0)

class Candidate(Value):
    id: str
    level: Literal[1, 2] = 1
    pattern_id: str
    base_release_digest: str
    candidate_release_digest: str
    content_identity: str | None = None
    targets: list[str]
    operation: Literal["TUNE", "REPLACE", "SIMPLIFY", "MERGE", "REMOVE", "ADD"]
    hypothesis: str
    source_evidence: list[str]
    growth_justification: str = ""
    parents: list[str] = Field(default_factory=list)

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "SUPERSEDED", "PARTIAL", "ANSWERED"}
UNRESOLVED = {"REGISTERED", "WAITING_APPROVAL", "AUTHORIZED", "EXECUTING", "UNCERTAIN"}
