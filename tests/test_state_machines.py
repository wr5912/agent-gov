import pytest

from app.runtime.errors import FeedbackStoreError
from app.runtime.state_machines import JOB_IN_PROGRESS_STATES, JOB_STATES, StateTransitionError, validate_transition


def test_execution_job_state_machine_allows_apply_lifecycle():
    validate_transition("execution_job", "queued", "running")
    validate_transition("execution_job", "running", "ready")
    validate_transition("execution_job", "ready", "completed")


def test_execution_job_state_machine_rejects_completed_reopen():
    assert issubclass(StateTransitionError, FeedbackStoreError)
    assert not issubclass(StateTransitionError, ValueError)
    with pytest.raises(StateTransitionError, match="completed -> running"):
        validate_transition("execution_job", "completed", "running")


def test_job_state_machine_rejects_completed_reopen():
    with pytest.raises(StateTransitionError, match="completed -> running"):
        validate_transition("job", "completed", "running")


def test_agent_job_state_machine_rejects_completed_reopen():
    validate_transition("agent_job", "queued", "running")
    validate_transition("agent_job", "running", "schema_validating")
    validate_transition("agent_job", "schema_validating", "timeout")
    validate_transition("agent_job", "evidence_packaging", "timeout")
    validate_transition("agent_job", "schema_validating", "completed")
    with pytest.raises(StateTransitionError, match="completed -> running"):
        validate_transition("agent_job", "completed", "running")


def test_execution_application_state_machine_rejects_applied_reopen():
    validate_transition("execution_application", "created", "applied")
    with pytest.raises(StateTransitionError, match="applied -> failed"):
        validate_transition("execution_application", "applied", "failed")


def test_job_in_progress_states_are_known_job_states():
    assert JOB_IN_PROGRESS_STATES <= JOB_STATES
    assert "completed" not in JOB_IN_PROGRESS_STATES


def test_batch_state_machine_allows_main_lifecycle():
    validate_transition("batch", "draft", "attribution_running")
    validate_transition("batch", "attribution_running", "draft")
    validate_transition("batch", "attribution_running", "attribution_completed")
    validate_transition("batch", "attribution_completed", "optimization_plan_queued")
    validate_transition("batch", "optimization_plan_queued", "pending_approval")
    validate_transition("batch", "pending_approval", "execution_planning")
    validate_transition("batch", "execution_planning", "execution_ready")
    validate_transition("batch", "execution_ready", "applied_pending_regression")
    validate_transition("batch", "applied_pending_regression", "regression_running")
    validate_transition("batch", "regression_running", "completed")


def test_batch_state_machine_rejects_terminal_reopen():
    with pytest.raises(StateTransitionError, match="completed -> draft"):
        validate_transition("batch", "completed", "draft")


def test_batch_state_machine_rejects_rejected_to_approved():
    with pytest.raises(StateTransitionError, match="rejected -> approved"):
        validate_transition("batch", "rejected", "approved")


def test_task_state_machine_allows_execution_and_regression_lifecycle():
    validate_transition("task", "pending_execution", "execution_planning")
    validate_transition("task", "execution_planning", "execution_ready")
    validate_transition("task", "execution_ready", "applied_pending_regression")
    validate_transition("task", "applied_pending_regression", "regression_running")
    validate_transition("task", "regression_running", "completed")


def test_task_state_machine_rejects_completed_reopen():
    with pytest.raises(StateTransitionError, match="completed -> pending_execution"):
        validate_transition("task", "completed", "pending_execution")


def test_case_state_machine_allows_retry_to_attribution():
    validate_transition("case", "pending_evidence", "pending_attribution")
    validate_transition("case", "pending_attribution", "attribution_queued")
    validate_transition("case", "attribution_queued", "pending_proposal")
    validate_transition("case", "pending_proposal", "pending_attribution")


def test_case_state_machine_rejects_review_to_pending_evidence():
    with pytest.raises(StateTransitionError, match="pending_review -> pending_evidence"):
        validate_transition("case", "pending_review", "pending_evidence")


def test_eval_run_state_machine_rejects_completed_to_failed():
    with pytest.raises(StateTransitionError, match="completed -> failed"):
        validate_transition("eval_run", "completed", "failed")


def test_regression_impact_analysis_state_machine_allows_rerun_only_to_pending():
    validate_transition("regression_impact_analysis", "pending", "completed")
    validate_transition("regression_impact_analysis", "completed", "pending")
    with pytest.raises(StateTransitionError, match="completed -> failed"):
        validate_transition("regression_impact_analysis", "completed", "failed")


def test_pending_correlation_state_machine_rejects_resolved_to_pending():
    validate_transition("pending_correlation", "pending", "resolved")
    with pytest.raises(StateTransitionError, match="resolved -> pending"):
        validate_transition("pending_correlation", "resolved", "pending")


def test_eval_case_state_machine_allows_governed_lifecycle():
    validate_transition("eval_case", "draft", "active")
    validate_transition("eval_case", "active", "archived")
    validate_transition("eval_case_promotion", "candidate", "approved")
    validate_transition("eval_case_promotion", "approved", "superseded")


def test_eval_case_state_machine_rejects_archived_reopen():
    with pytest.raises(StateTransitionError, match="archived -> active"):
        validate_transition("eval_case", "archived", "active")


def test_proposal_state_machine_rejects_rejected_to_approved():
    with pytest.raises(StateTransitionError, match="rejected -> approved"):
        validate_transition("proposal", "rejected", "approved")


def test_external_governance_item_state_machine_rejects_superseded_reopen():
    with pytest.raises(StateTransitionError, match="superseded -> notified"):
        validate_transition("external_governance_item", "superseded", "notified")


def test_state_machine_rejects_unknown_status():
    with pytest.raises(StateTransitionError, match="Unknown job status"):
        validate_transition("job", "queued", "almost_done")


def test_state_machine_rejects_missing_transition_table(monkeypatch):
    from app.runtime import state_machines

    monkeypatch.setitem(state_machines._KNOWN_STATES, "broken", {"one", "two"})
    with pytest.raises(StateTransitionError, match="has no transition table"):
        validate_transition("broken", "one", "two")
