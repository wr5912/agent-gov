from __future__ import annotations

from typing import TypeAlias

from ..records.external_governance_records import ExternalGovernanceItemRecord
from ..records.json_types import JsonObject
from ..runtime_db import ExternalGovernanceItemModel, ProposalReviewModel


SourceAnnotationsByKey: TypeAlias = dict[tuple[str, str], JsonObject]
FeedbackCasesBySourceId: TypeAlias = dict[str, JsonObject]
EvalCasesByFeedbackCaseId: TypeAlias = dict[str, JsonObject]
AgentJobsById: TypeAlias = dict[str, JsonObject]
ExternalGovernanceRowsBySourceIndex: TypeAlias = dict[
    int,
    tuple[ExternalGovernanceItemModel, ExternalGovernanceItemRecord],
]
ProposalReviewsByProposalId: TypeAlias = dict[str, ProposalReviewModel]
ProposalSupersedeCounts: TypeAlias = dict[str, int]
