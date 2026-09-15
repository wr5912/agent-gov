from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from ._store_operations import RuntimeChatOperationStoreMixin
from ._store_resources import RuntimeResourceStoreMixin
from ._store_run_queries import RuntimeRunQueryStoreMixin
from ._store_run_recovery import RuntimeRunRecoveryStoreMixin
from ._store_runs import RuntimeRunStoreMixin
from ._store_sessions import RuntimeSessionStoreMixin
from ._store_support import (
    RuntimeAuthenticationError as RuntimeAuthenticationError,
)
from ._store_support import (
    RuntimeInputRejected as RuntimeInputRejected,
)
from ._store_support import (
    RuntimeObjectNotFound as RuntimeObjectNotFound,
)
from ._store_support import (
    RuntimeRestartRequired as RuntimeRestartRequired,
)
from ._store_support import (
    RuntimeStateConflict as RuntimeStateConflict,
)
from ._store_support import (
    RuntimeStoreError as RuntimeStoreError,
)
from ._store_support import (
    RuntimeTemplateRestartRequired as RuntimeTemplateRestartRequired,
)
from ._store_support import (
    SessionCreationStatus as SessionCreationStatus,
)
from ._store_support import (
    harness_digest as harness_digest,
)


class RuntimeRunStore(
    RuntimeResourceStoreMixin,
    RuntimeSessionStoreMixin,
    RuntimeChatOperationStoreMixin,
    RuntimeRunStoreMixin,
    RuntimeRunRecoveryStoreMixin,
    RuntimeRunQueryStoreMixin,
):
    """Session/Run/HITL/Trace 的 AgentGov 持久化门面。"""

    def __init__(self, session_factory: sessionmaker) -> None:
        self.Session = session_factory
