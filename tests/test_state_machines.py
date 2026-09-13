import pytest
from app.runtime import state_machines
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


def test_agent_test_run_state_machine_has_terminal_results():
    validate_transition("agent_test_run", "queued", "running")
    validate_transition("agent_test_run", "queued", "cancelled")
    for terminal in ("passed", "failed", "error", "cancelled", "interrupted"):
        validate_transition("agent_test_run", "running", terminal)
        with pytest.raises(StateTransitionError, match=f"{terminal} -> running"):
            validate_transition("agent_test_run", terminal, "running")


def test_pending_correlation_state_machine_rejects_resolved_to_pending():
    validate_transition("pending_correlation", "pending", "resolved")
    with pytest.raises(StateTransitionError, match="resolved -> pending"):
        validate_transition("pending_correlation", "resolved", "pending")


def test_agent_change_set_state_machine_allows_current_publish_lifecycle():
    validate_transition("agent_change_set", "draft", "candidate_committed")
    validate_transition("agent_change_set", "candidate_committed", "candidate_committed")
    validate_transition("agent_change_set", "candidate_committed", "publishing")
    validate_transition("agent_change_set", "publishing", "published")
    validate_transition("agent_change_set", "publishing", "candidate_committed")
    with pytest.raises(StateTransitionError, match="candidate_committed -> published"):
        validate_transition("agent_change_set", "candidate_committed", "published")
    with pytest.raises(StateTransitionError, match="candidate_committed -> approved"):
        validate_transition("agent_change_set", "candidate_committed", "approved")
    validate_transition("agent_change_set", "candidate_committed", "pending_approval")
    validate_transition("agent_change_set", "pending_approval", "approved")


def test_draft_agent_activation_is_reserved_for_release_orchestration() -> None:
    with pytest.raises(StateTransitionError, match="draft -> active"):
        validate_transition("agent_lifecycle", "draft", "active")
    validate_transition("agent_release_activation", "draft", "active")
    validate_transition("agent_release_activation", "active", "active")
    with pytest.raises(StateTransitionError, match="Unknown current agent_release_activation status: evaluating"):
        validate_transition("agent_release_activation", "evaluating", "active")


@pytest.mark.parametrize(
    ("status", "expected"),
    [("active", True), ("evaluating", True), ("draft", False), ("deprecated", False), ("archived", False), (None, False)],
)
def test_agent_runnable_lifecycle_predicate_is_the_single_semantic_source(status: object, expected: bool) -> None:
    assert state_machines.is_agent_lifecycle_runnable(status) is expected


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


def test_state_machine_rejects_unknown_status():
    with pytest.raises(StateTransitionError, match="Unknown case status"):
        validate_transition("case", "pending_evidence", "almost_done")


def test_every_declared_state_machine_has_a_complete_transition_table():
    assert set(state_machines._KNOWN_STATES) == set(state_machines._TRANSITIONS)
    for machine, known_states in state_machines._KNOWN_STATES.items():
        assert set(state_machines._TRANSITIONS[machine]) == known_states
