from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from ..runtime_db import FeedbackJobModel, utc_now


class FeedbackJobFactory:
    """Creates queued feedback jobs with a consistent persisted shape."""

    def __init__(
        self,
        *,
        session_factory: Any,
        tmp_jobs_dir: Path,
        runtime_version: str,
        agent_version_provider: Callable[[], Optional[str]],
    ) -> None:
        self.Session = session_factory
        self.tmp_jobs_dir = tmp_jobs_dir
        self.runtime_version = runtime_version
        self.agent_version_provider = agent_version_provider

    def create_queued_job(
        self,
        *,
        job_id: str,
        job_type: str,
        feedback_case_id: str,
        evidence_package_id: str,
        profile_name: str,
        input_payload: dict[str, Any],
        profile_version: Optional[dict[str, Any]] = None,
        attribution_job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            input_path = self.write_input(job_id, job_type, input_payload)
            job = self.job_record(
                job_id=job_id,
                job_type=job_type,
                feedback_case_id=feedback_case_id,
                evidence_package_id=evidence_package_id,
                status="queued",
                profile_name=profile_name,
                input_path=input_path,
                profile_version=profile_version,
                attribution_job_id=attribution_job_id,
            )
            job["input_json"] = input_payload
            self.persist(job)
            return job
        except Exception:
            self.cleanup(job_id)
            raise

    def write_input(self, job_id: str, job_type: str, payload: dict[str, Any]) -> str:
        job_dir = self.tmp_jobs_dir / job_id / job_type
        job_dir.mkdir(parents=True, exist_ok=True)
        path = job_dir / "input.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def cleanup(self, job_id: str) -> None:
        if job_id:
            shutil.rmtree(self.tmp_jobs_dir / job_id, ignore_errors=True)

    def job_record(
        self,
        *,
        job_id: str,
        job_type: str,
        feedback_case_id: str,
        evidence_package_id: str,
        status: str,
        profile_name: str,
        input_path: str,
        profile_version: Optional[dict[str, Any]] = None,
        attribution_job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now = utc_now()
        record = {
            "job_id": job_id,
            "job_type": job_type,
            "feedback_case_id": feedback_case_id,
            "evidence_package_id": evidence_package_id,
            "status": status,
            "profile_name": profile_name,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "timeout_seconds": 300,
            "retry_count": 0,
            "input_path": input_path,
            "raw_output_path": f"sqlite://feedback_jobs/{job_id}/raw_output_json",
            "validated_output_path": f"sqlite://feedback_jobs/{job_id}/validated_output_json",
            "error_path": f"sqlite://feedback_jobs/{job_id}/error_json",
            "langfuse_trace_id": None,
            "main_agent_version_id": self.agent_version_provider(),
            "runtime_version": self.runtime_version,
            "schema_version": f"{job_type}-job/v1",
        }
        if profile_version:
            record["profile_version"] = profile_version
            record[f"{job_type}_agent_version"] = profile_version.get("agent_version")
        if attribution_job_id:
            record["attribution_job_id"] = attribution_job_id
        return record

    def persist(self, job: dict[str, Any]) -> None:
        with self.Session.begin() as db:
            db.add(self.model_from_dict(job))

    def model_from_dict(self, job: dict[str, Any]) -> FeedbackJobModel:
        return FeedbackJobModel(
            job_id=job["job_id"],
            job_type=job["job_type"],
            feedback_case_id=job["feedback_case_id"],
            evidence_package_id=job["evidence_package_id"],
            attribution_job_id=_string(job.get("attribution_job_id")),
            status=job["status"],
            profile_name=job["profile_name"],
            created_at=job["created_at"],
            started_at=_string(job.get("started_at")),
            completed_at=_string(job.get("completed_at")),
            input_path=job["input_path"],
            raw_output_path=job["raw_output_path"],
            validated_output_path=job["validated_output_path"],
            error_path=job["error_path"],
            langfuse_trace_id=_string(job.get("langfuse_trace_id")),
            main_agent_version_id=_string(job.get("main_agent_version_id")),
            runtime_version=job.get("runtime_version") or self.runtime_version,
            schema_version=job["schema_version"],
            timeout_seconds=int(job.get("timeout_seconds") or 300),
            retry_count=int(job.get("retry_count") or 0),
            profile_version_json=job.get("profile_version"),
            input_json=job.get("input_json"),
        )


def _string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
