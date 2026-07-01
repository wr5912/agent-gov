import pytest
from app.runtime.errors import FeedbackStoreError
from app.runtime.state_machines import JOB_IN_PROGRESS_STATES, JOB_STATES, StateTransitionError, validate_transition


def test_job_state_machine_rejects_completed_reopen():
    assert issubclass(StateTransitionError, FeedbackStoreError)
    assert not issubclass(StateTransitionError, ValueError)
    with pytest.raises(StateTransitionError, match="completed -> running"):
        validate_transition("job", "completed", "running")


def test_agent_job_state_machine_allows_current_lifecycle():
    validate_transition("agent_job", "queued", "running")
    validate_transition("agent_job", "running", "schema_validating")
    validate_transition("agent_job", "schema_validating", "timeout")
    validate_transition("agent_job", "evidence_packaging", "timeout")
    validate_transition("agent_job", "schema_validating", "completed")
    with pytest.raises(StateTransitionError, match="completed -> running"):
        validate_transition("agent_job", "completed", "running")


def test_job_in_progress_states_are_known_job_states():
    assert JOB_IN_PROGRESS_STATES <= JOB_STATES
    assert "completed" not in JOB_IN_PROGRESS_STATES


def test_case_state_machine_allows_retry_to_attribution():
    validate_transition("case", "pending_evidence", "pending_attribution")
    validate_transition("case", "pending_attribution", "attribution_queued")
    validate_transition("case", "attribution_queued", "pending_review")
    validate_transition("case", "pending_review", "pending_attribution")


def test_case_state_machine_rejects_review_to_pending_evidence():
    with pytest.raises(StateTransitionError, match="pending_review -> pending_evidence"):
        validate_transition("case", "pending_review", "pending_evidence")


def test_eval_run_state_machine_rejects_completed_to_failed():
    with pytest.raises(StateTransitionError, match="completed -> failed"):
        validate_transition("eval_run", "completed", "failed")


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


def test_agent_change_set_state_machine_allows_current_publish_lifecycle():
    validate_transition("agent_change_set", "draft", "candidate_committed")
    validate_transition("agent_change_set", "candidate_committed", "regression_running")
    validate_transition("agent_change_set", "regression_running", "regression_passed")
    validate_transition("agent_change_set", "regression_passed", "published")


def test_improvement_stage_state_machine_allows_four_stage_flow_with_refinement_edges():
    validate_transition("improvement_stage", "feedback_intake", "triage")
    validate_transition("improvement_stage", "triage", "attribution")
    validate_transition("improvement_stage", "attribution", "optimization")
    validate_transition("improvement_stage", "optimization", "execution")
    validate_transition("improvement_stage", "execution", "regression")
    validate_transition("improvement_stage", "regression", "release")
    validate_transition("improvement_stage", "regression", "optimization")


def test_state_machine_rejects_unknown_status():
    with pytest.raises(StateTransitionError, match="Unknown job status"):
        validate_transition("job", "queued", "almost_done")


def test_state_machine_rejects_missing_transition_table(monkeypatch):
    from app.runtime import state_machines

    monkeypatch.setitem(state_machines._KNOWN_STATES, "broken", {"one", "two"})
    with pytest.raises(StateTransitionError, match="has no transition table"):
        validate_transition("broken", "one", "two")
