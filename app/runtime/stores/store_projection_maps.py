from __future__ import annotations

from typing import TypeAlias

from ..records.agent_job_records import AgentJobRecord
from ..records.case_records import FeedbackCaseRecord
from ..records.eval_case_records import EvalCaseRecord
from ..records.source_records import FeedbackSourceAnnotationRecord


SourceAnnotationsByKey: TypeAlias = dict[tuple[str, str], FeedbackSourceAnnotationRecord]
FeedbackCasesBySourceId: TypeAlias = dict[str, FeedbackCaseRecord]
EvalCasesByFeedbackCaseId: TypeAlias = dict[str, EvalCaseRecord]
AgentJobsById: TypeAlias = dict[str, AgentJobRecord]
