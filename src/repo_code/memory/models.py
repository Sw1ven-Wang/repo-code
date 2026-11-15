from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryProfile(Contract):
    schema_version: Literal[2] = 2
    implementation_revision: Literal['2.1'] = '2.1'
    implementation_digest: str | None = None
    mode: Literal["hybrid_semantic_strict", "lexical_only"] = "hybrid_semantic_strict"
    encoder: dict[str, Any] = Field(default_factory=dict)
    max_tokens: int = Field(24000, ge=3000, le=64000)
    reserve_tokens: int = Field(4000, ge=256)
    safety_tokens: int = Field(1000, ge=0)
    evidence_tokens: int = Field(3000, ge=128, le=8000)
    recent_groups: int = Field(4, ge=1, le=20)
    auxiliary_calls: int | None = Field(8, ge=0, le=100)
    total_token_budget: int | None = Field(500000, ge=1000)
    auxiliary_output_tokens: int = Field(2048, ge=256, le=8192)
    protocol_rebase_verified: bool = False
    protocol_rebase_profile_digests: list[str] = Field(default_factory=list)
    protocol_rebase_tool_digests: list[str] = Field(default_factory=list)
    auto_activate_observations: bool = False
    auto_activate_preferences: bool = False
    semantic_consolidation: bool = True
    learning_enabled: bool = True
    max_items: int = Field(2000, ge=1, le=100000)
    max_jobs: int = Field(100, ge=1, le=1000)
    dense_min_score: float = Field(0.80, ge=-1, le=1)
    rerank_min_score: float = 0.0
    calibration_digest: str | None = None

    @model_validator(mode="after")
    def budget_fits(self):
        if self.reserve_tokens + self.safety_tokens >= self.max_tokens:
            raise ValueError("output reserve and safety margin exhaust the window")
        return self


class EvidenceRef(Contract):
    source_kind: Literal["user_message", "tool_result", "model_message", "verification", "artifact", "file_observation"]
    source_id: str
    content_digest: str
    selector: dict[str, Any] = Field(default_factory=lambda: {"type": "json_pointer", "value": "/content"})
    run_id: str
    scope_id: str
    origin_class: Literal["HOST_OBSERVATION", "USER_STATED", "MODEL_PROPOSED"]
    visibility_domain: str
    observed_at: float
    source_version: dict[str, str] = Field(default_factory=dict)


class ClaimRecord(Contract):
    claim_id: str
    version: int = Field(1, ge=1)
    kind: Literal["FACT", "USER_PREFERENCE", "DECISION", "EPISODE", "HYPOTHESIS", "PROCEDURE"]
    text: str = Field(min_length=1, max_length=4000)
    scope_id: str
    visibility_domain: str = "DEV"
    evidence_schema_version: Literal[1,2] = 1
    support_refs: list[EvidenceRef] = Field(default_factory=list, max_length=32)
    contradiction_refs: list[EvidenceRef] = Field(default_factory=list)
    implementation_evidence: list[EvidenceRef] = Field(default_factory=list,max_length=32)
    verification_evidence: list[EvidenceRef] = Field(default_factory=list,max_length=32)
    context_refs: list[EvidenceRef] = Field(default_factory=list,max_length=32)
    derived_from: list[str] = Field(default_factory=list)
    epistemic_status: Literal["OBSERVED", "USER_STATED", "INFERRED", "DISPUTED"] = "INFERRED"
    lifecycle: Literal["CANDIDATE", "ACTIVE", "DEPRECATED", "ARCHIVED", "REVOKED"] = "CANDIDATE"
    validity: Literal["CURRENT", "HISTORICAL", "STALE", "UNKNOWN", "CONTRADICTED"] = "UNKNOWN"
    preconditions: dict[str, str] = Field(default_factory=dict)
    file_dependencies: dict[str, str] = Field(default_factory=dict)
    valid_until: float | None = None
    supersedes: list[str] = Field(default_factory=list)
    independent_support: list[str] = Field(default_factory=list)
    subject: str = ""

    @model_validator(mode='after')
    def evidence_required(self):
        if self.evidence_schema_version==1 and not self.support_refs:
            raise ValueError('legacy claims require support_refs')
        if not any([self.support_refs,self.contradiction_refs,self.implementation_evidence,self.verification_evidence,self.context_refs]):
            raise ValueError('claim requires at least one bound source')
        if self.evidence_schema_version==2 and not self.support_refs and self.independent_support:
            raise ValueError('independent positive support requires supporting evidence')
        return self


class ClaimDelta(Contract):
    action: Literal["ADD", "UPDATE", "RETIRE", "SUPERSEDE", "CONFLICT", "RETRACT_PROPOSAL"] = "ADD"
    claim_id: str
    kind: Literal["FACT", "USER_PREFERENCE", "DECISION", "EPISODE", "HYPOTHESIS", "PROCEDURE"]
    text: str = Field(min_length=1, max_length=1200)
    source_ids: list[str] = Field(min_length=1, max_length=8)
    quote: str = Field(min_length=1, max_length=1200)
    epistemic_status: Literal["OBSERVED", "USER_STATED", "INFERRED", "DISPUTED"] = "INFERRED"


class CompactionProposal(Contract):
    changes: list[ClaimDelta] = Field(default_factory=list, max_length=32)
    retained_open_loop_ids: list[str] = Field(default_factory=list, max_length=100)


class QuoteClaimDelta(Contract):
    action: Literal['ADD','UPDATE','RETIRE','SUPERSEDE','CONFLICT','RETRACT_PROPOSAL']='ADD'
    claim_id: str
    kind: Literal['FACT','USER_PREFERENCE','DECISION','EPISODE','HYPOTHESIS','PROCEDURE']
    quote_id: str = Field(min_length=1,max_length=100)
    additional_quote_ids: list[str] = Field(default_factory=list,max_length=7)
    text: str = Field('',max_length=1200)
    epistemic_status: Literal['OBSERVED','USER_STATED','INFERRED','DISPUTED']='INFERRED'


class QuoteCompactionProposal(Contract):
    changes: list[QuoteClaimDelta] = Field(default_factory=list,max_length=32)
    retained_open_loop_ids: list[str] = Field(default_factory=list,max_length=100)


class ProviderEnvelope(Contract):
    profile_digest: str
    messages: list[dict[str, Any]]
    opaque_refs: list[str] = Field(default_factory=list)
    rebase_verified: bool = False


class IntentFrame(Contract):
    run_id: str
    task_revision: int
    contract_digest: str | None
    phase: Literal["PLAN", "EDIT", "DEBUG", "VERIFY", "RESUME"]
    current_subgoal: str
    active_files: list[str]
    active_symbols: list[str] = Field(default_factory=list)
    recent_error_codes: list[str] = Field(default_factory=list)
    authorized_scopes: list[str]
    visibility_domain: str
    knowledge_snapshot_digest: str
    index_bundle_digest: str
    working_state_revision: str
    event_cutoff: int
    semantic_query: str
    environment: dict[str, str] = Field(default_factory=dict)


class RequestContextManifest(Contract):
    schema_version: Literal[2] = 2
    run_id: str
    turn_request_id: str
    release_digest: str
    model_config_digest: str
    provider_protocol_profile_digest: str
    knowledge_snapshot_digest: str
    knowledge_index_digest: str
    working_state_digest: str
    summary_node_ids: list[str]
    summary_cutoff: int
    summary_segments: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_trace_digest: str
    selected_memory_ids_versions: list[dict[str, Any]]
    tool_schema_digest: str
    control_epoch: int
    writer_epoch: int
    revocation_epoch: int
    authorization_digest: str
    visibility_domain: str
    estimated_input_tokens: int
    reserved_output_tokens: int
    estimate_method: str = "unicode-weighted-estimate-v1"
    serialized_request_digest: str
    protocol_envelope_ref: str
    mode: str
    budget_breakdown: dict[str,Any] = Field(default_factory=dict)
    packing_attempts: int = 1
