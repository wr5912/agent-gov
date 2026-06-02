from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator
from pydantic.types import JsonValue

from app.runtime.runtime_db import EvidenceFileModel, EvidencePackageModel

from .json_types import JsonObject, StrictRuntimeRecord


EvidencePackageSchemaVersion = Literal["evidence-package/v1"]


class EvidenceSourceRefsRecord(StrictRuntimeRecord):
    """Source references captured by one evidence package manifest."""

    feedback_ids: list[str] = Field(default_factory=list)
    signal_ids: list[str] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)
    alert_ids: list[str] = Field(default_factory=list)
    case_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)

    @field_validator("*")
    @classmethod
    def validate_string_list(cls, value: list[str]) -> list[str]:
        return [str(item) for item in value if item]


class EvidenceIncludedFileRecord(StrictRuntimeRecord):
    """Manifest metadata for one materialized evidence file."""

    path: str
    sha256: str
    type: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value or Path(value).name != value or value == "manifest.json":
            raise ValueError(f"unsafe evidence file path: {value}")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if len(value) != 64:
            raise ValueError("evidence file sha256 must be 64 hex characters")
        int(value, 16)
        return value

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence file type cannot be empty")
        return value

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")


class EvidenceRedactionRecord(StrictRuntimeRecord):
    """Redaction policy applied when evidence was captured."""

    enabled: bool = False
    policy: str = ""
    redacted_fields: list[str] = Field(default_factory=list)

    @field_validator("redacted_fields")
    @classmethod
    def validate_redacted_fields(cls, value: list[str]) -> list[str]:
        return [str(item) for item in value if item]


class EvidenceCompletenessRecord(StrictRuntimeRecord):
    """Completeness flags for expected evidence package contents."""

    has_feedback: bool = False
    has_runs: bool = False
    has_tool_calls: bool = False
    has_trace_summary: bool = False
    has_main_agent_version: bool = False
    has_messages: bool = False
    has_agent_activity: bool = False
    has_langfuse_trace_refs: bool = False
    has_langfuse_trace_details: bool = False


class EvidencePackageRecord(StrictRuntimeRecord):
    """Internal source of truth for one evidence package manifest."""

    schema_version: EvidencePackageSchemaVersion
    evidence_package_id: str
    feedback_case_id: str
    created_at: str
    created_by: str
    main_agent_version_id: Optional[str] = None
    source_refs: EvidenceSourceRefsRecord
    included_files: list[EvidenceIncludedFileRecord] = Field(default_factory=list)
    redaction: EvidenceRedactionRecord
    completeness: EvidenceCompletenessRecord

    @model_validator(mode="after")
    def validate_manifest_shape(self) -> "EvidencePackageRecord":
        if not self.evidence_package_id.strip():
            raise ValueError("evidence_package_id cannot be empty")
        if not self.feedback_case_id.strip():
            raise ValueError("feedback_case_id cannot be empty")
        if not self.created_at.strip():
            raise ValueError("created_at cannot be empty")
        if not self.created_by.strip():
            raise ValueError("created_by cannot be empty")
        paths = [item.path for item in self.included_files]
        if len(paths) != len(set(paths)):
            raise ValueError("evidence package included_files cannot contain duplicate paths")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: EvidencePackageModel) -> "EvidencePackageRecord":
        payload = dict(row.manifest_json or {})
        payload.update(
            {
                "evidence_package_id": row.evidence_package_id,
                "feedback_case_id": row.feedback_case_id,
                "created_at": row.created_at,
            }
        )
        return cls.model_validate(payload)


class EvidencePackageFileRecord(StrictRuntimeRecord):
    """Internal source of truth for one evidence file projection."""

    evidence_package_id: str
    file_name: str
    sha256: str
    content: JsonValue

    @field_validator("file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        if not value or Path(value).name != value or value == "manifest.json":
            raise ValueError(f"unsafe evidence file name: {value}")
        return value

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: EvidenceFileModel) -> "EvidencePackageFileRecord":
        return cls.model_validate(
            {
                "evidence_package_id": row.evidence_package_id,
                "file_name": row.file_name,
                "sha256": row.sha256,
                "content": row.content_json,
            }
        )
