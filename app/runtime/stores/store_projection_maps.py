from __future__ import annotations

from typing import TypeAlias

from ..records.agent_job_records import AgentJobRecord
from ..records.case_records import FeedbackCaseRecord
from ..records.eval_case_records import EvalCaseRecord
from ..records.external_governance_records import ExternalGovernanceItemRecord
from ..records.source_records import FeedbackSourceAnnotationRecord
from ..runtime_db import ExternalGovernanceItemModel, ProposalReviewModel


SourceAnnotationsByKey: TypeAlias = dict[tuple[str, str], FeedbackSourceAnnotationRecord]
FeedbackCasesBySourceId: TypeAlias = dict[str, FeedbackCaseRecord]
EvalCasesByFeedbackCaseId: TypeAlias = dict[str, EvalCaseRecord]
AgentJobsById: TypeAlias = dict[str, AgentJobRecord]
ExternalGovernanceRowsBySourceIndex: TypeAlias = dict[
    int,
    tuple[ExternalGovernanceItemModel, ExternalGovernanceItemRecord],
]
ProposalReviewsByProposalId: TypeAlias = dict[str, ProposalReviewModel]
ProposalSupersedeCounts: TypeAlias = dict[str, int]
