from __future__ import annotations

from typing import TypeAlias

from ..records.agent_job_records import AgentJobRecord
from ..records.case_records import FeedbackCaseRecord
from ..records.source_records import FeedbackSourceAnnotationRecord

SourceAnnotationsByKey: TypeAlias = dict[tuple[str, str], FeedbackSourceAnnotationRecord]
FeedbackCasesBySourceRef: TypeAlias = dict[tuple[str, str], FeedbackCaseRecord]
AgentJobsById: TypeAlias = dict[str, AgentJobRecord]
