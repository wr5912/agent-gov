import pytest
from app.runtime.errors import FeedbackStoreError
from app.runtime.state_machines import (
    AGENT_DELETION_STATES,
    AGENT_DELETION_TRANSITIONS,
    WORKSPACE_ACTIVATION_FENCE_STATES,
    WORKSPACE_ACTIVATION_STATES,
    WORKSPACE_ACTIVATION_TRANSITIONS,
    StateTransitionError,
    validate_transition,
)
from app.services.agent_workspace_activation_journal import RECOVERY_PHASE_TRANSITIONS


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
    validate_transition("agent_change_set", "candidate_committed", "publishing")
    validate_transition("agent_change_set", "publishing", "published")
    validate_transition("agent_change_set", "publishing", "candidate_committed")
    with pytest.raises(StateTransitionError, match="candidate_committed -> published"):
        validate_transition("agent_change_set", "candidate_committed", "published")


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


def test_workspace_activation_transition_table_covers_every_legal_and_illegal_edge():
    assert set(WORKSPACE_ACTIVATION_TRANSITIONS) == WORKSPACE_ACTIVATION_STATES
    assert {
        "preparing",
        "prepared",
        "completing",
        "rejecting",
        "recovery_required",
    } == WORKSPACE_ACTIVATION_FENCE_STATES
    for current in WORKSPACE_ACTIVATION_STATES:
        for target in WORKSPACE_ACTIVATION_STATES:
            if current == target or target in WORKSPACE_ACTIVATION_TRANSITIONS[current]:
                validate_transition("workspace_activation", current, target)
            else:
                with pytest.raises(StateTransitionError, match=f"{current} -> {target}"):
                    validate_transition("workspace_activation", current, target)


def test_workspace_activation_recovery_phase_table_is_complete_and_terminal_outcomes_do_not_reopen():
    assert set(RECOVERY_PHASE_TRANSITIONS) == {
        "none",
        "candidate_reset",
        "base_reset",
        "head_reset",
        "index_restore",
        "completion_outcome",
        "rejection_outcome",
    }
    assert RECOVERY_PHASE_TRANSITIONS["completion_outcome"] == set()
    assert RECOVERY_PHASE_TRANSITIONS["rejection_outcome"] == set()
    assert RECOVERY_PHASE_TRANSITIONS["none"] == {"candidate_reset", "base_reset"}
    assert "candidate_reset" not in RECOVERY_PHASE_TRANSITIONS["head_reset"]


def test_agent_deletion_has_one_way_complete_transition_and_terminal_does_not_reopen():
    assert set(AGENT_DELETION_TRANSITIONS) == AGENT_DELETION_STATES
    validate_transition("agent_deletion", "cleanup_pending", "completed")
    with pytest.raises(StateTransitionError, match="completed -> cleanup_pending"):
        validate_transition("agent_deletion", "completed", "cleanup_pending")
    with pytest.raises(StateTransitionError, match="Unknown agent_deletion status"):
        validate_transition("agent_deletion", "cleanup_pending", "failed")


def test_state_machine_rejects_unknown_status():
    with pytest.raises(StateTransitionError, match="Unknown case status"):
        validate_transition("case", "pending_evidence", "almost_done")


def test_state_machine_rejects_missing_transition_table(monkeypatch):
    from app.runtime import state_machines

    monkeypatch.setitem(state_machines._KNOWN_STATES, "broken", {"one", "two"})
    with pytest.raises(StateTransitionError, match="has no transition table"):
        validate_transition("broken", "one", "two")
