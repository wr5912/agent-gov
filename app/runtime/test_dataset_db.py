from __future__ import annotations

from typing import Optional

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .runtime_db_base import Base, utc_now


class TestDatasetModel(Base):
    __tablename__ = "test_datasets"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_test_datasets_positive_revision"),
        CheckConstraint(
            "lifecycle_state IN ('draft', 'active', 'evaluating', 'deprecated', 'archived')",
            name="ck_test_datasets_lifecycle_state",
        ),
    )

    dataset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    owner_kind: Mapped[str] = mapped_column(String(32), default="business_agent", index=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    source_improvement_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(String(512), default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    lifecycle_state: Mapped[str] = mapped_column(String(32), index=True)
    source_regression_assessment_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source_regression_assessment_updated_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_normalized_feedback_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source_normalized_feedback_updated_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_attribution_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source_attribution_updated_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_optimization_plan_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source_optimization_plan_updated_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_execution_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source_execution_updated_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    baseline_agent_version_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    candidate_agent_version_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    source_feedback_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    quality_tags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)


class TestDatasetCaseModel(Base):
    __tablename__ = "test_dataset_cases"

    case_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("test_datasets.dataset_id", ondelete="CASCADE"),
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[str] = mapped_column(Text)
    expected_behavior: Mapped[str] = mapped_column(Text)
    checkpoints_json: Mapped[list[str]] = mapped_column(JSON, default=list)


Index("ux_test_dataset_cases_position", TestDatasetCaseModel.dataset_id, TestDatasetCaseModel.position, unique=True)


class TestDatasetRevisionModel(Base):
    __tablename__ = "test_dataset_revisions"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_test_dataset_revisions_positive_revision"),
        CheckConstraint(
            "lifecycle_state IN ('draft', 'active', 'evaluating', 'deprecated', 'archived')",
            name="ck_test_dataset_revisions_lifecycle_state",
        ),
    )

    revision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("test_datasets.dataset_id", ondelete="CASCADE"),
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer)
    previous_lifecycle_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    lifecycle_state: Mapped[str] = mapped_column(String(32))
    operator: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(2048))
    before_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    after_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)


Index(
    "ux_test_dataset_revisions_dataset_revision",
    TestDatasetRevisionModel.dataset_id,
    TestDatasetRevisionModel.revision,
    unique=True,
)


class ArchivedTestDatasetAssetModel(Base):
    __tablename__ = "archived_test_dataset_assets"

    legacy_asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text, default="")
    source_improvement_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    inherited_from: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[str] = mapped_column(String(64))
    archived_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    reason: Mapped[str] = mapped_column(String(512))
