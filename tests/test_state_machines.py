import pytest

from app.runtime.errors import FeedbackStoreError
from app.runtime.state_machines import StateTransitionError, validate_transition


def test_execution_job_state_machine_allows_apply_lifecycle():
    validate_transition("execution_job", "queued", "running")
    validate_transition("execution_job", "running", "ready")
    validate_transition("execution_job", "ready", "completed")


def test_execution_job_state_machine_rejects_completed_reopen():
    assert issubclass(StateTransitionError, FeedbackStoreError)
    with pytest.raises(StateTransitionError, match="completed -> running"):
        validate_transition("execution_job", "completed", "running")


def test_state_machine_rejects_unknown_status():
    with pytest.raises(StateTransitionError, match="Unknown job status"):
        validate_transition("job", "queued", "almost_done")
