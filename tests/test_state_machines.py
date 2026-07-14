import pytest
from app.runtime.errors import FeedbackStoreError
from app.runtime.state_machines import StateTransitionError, validate_transition


def test_state_transition_error_uses_feedback_error_contract():
    assert issubclass(StateTransitionError, FeedbackStoreError)
    assert not issubclass(StateTransitionError, ValueError)


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


def test_agent_change_set_state_machine_allows_current_publish_lifecycle():
    validate_transition("agent_change_set", "draft", "candidate_committed")
    validate_transition("agent_change_set", "candidate_committed", "regression_running")
    validate_transition("agent_change_set", "regression_running", "regression_review_required")
    validate_transition("agent_change_set", "regression_review_required", "regression_passed")
    validate_transition("agent_change_set", "regression_review_required", "regression_failed")
    validate_transition("agent_change_set", "regression_running", "regression_passed")
    validate_transition("agent_change_set", "regression_passed", "publishing")
    validate_transition("agent_change_set", "publishing", "published")
    validate_transition("agent_change_set", "publishing", "candidate_committed")
    with pytest.raises(StateTransitionError, match="regression_passed -> published"):
        validate_transition("agent_change_set", "regression_passed", "published")


def test_test_dataset_uses_central_lifecycle_state_machine():
    validate_transition("test_dataset", "draft", "active")
    validate_transition("test_dataset", "active", "evaluating")
    with pytest.raises(StateTransitionError, match="active -> draft"):
        validate_transition("test_dataset", "active", "draft")


def test_improvement_stage_state_machine_allows_four_stage_flow_with_refinement_edges():
    validate_transition("improvement_stage", "feedback_intake", "triage")
    validate_transition("improvement_stage", "triage", "attribution")
    validate_transition("improvement_stage", "attribution", "optimization")
    validate_transition("improvement_stage", "optimization", "execution")
    validate_transition("improvement_stage", "execution", "regression")
    validate_transition("improvement_stage", "regression", "release")
    validate_transition("improvement_stage", "regression", "optimization")


def test_improvement_execution_claim_must_finish_before_confirmation():
    validate_transition("improvement_execution", "draft", "applying")
    validate_transition("improvement_execution", "applying", "draft")
    with pytest.raises(StateTransitionError, match="applying -> confirmed"):
        validate_transition("improvement_execution", "applying", "confirmed")


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled", "interrupted"])
def test_session_turn_intent_only_moves_from_running_to_terminal(terminal):
    validate_transition("session_turn_intent", "running", terminal)
    with pytest.raises(StateTransitionError, match=f"{terminal} -> running"):
        validate_transition("session_turn_intent", terminal, "running")


def test_agent_release_operation_transition_table_is_complete():
    validate_transition("agent_release_operation", "reserved", "git_applied")
    validate_transition("agent_release_operation", "reserved", "failed")
    validate_transition("agent_release_operation", "git_applied", "completed")
    validate_transition("agent_release_operation", "git_applied", "failed")
    validate_transition("agent_release_operation", "failed", "reserved")
    with pytest.raises(StateTransitionError, match="completed -> reserved"):
        validate_transition("agent_release_operation", "completed", "reserved")


def test_state_machine_rejects_unknown_status():
    with pytest.raises(StateTransitionError, match="Unknown case status"):
        validate_transition("case", "pending_evidence", "almost_done")


def test_state_machine_rejects_missing_transition_table(monkeypatch):
    from app.runtime import state_machines

    monkeypatch.setitem(state_machines._KNOWN_STATES, "broken", {"one", "two"})
    with pytest.raises(StateTransitionError, match="has no transition table"):
        validate_transition("broken", "one", "two")
