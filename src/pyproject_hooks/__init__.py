"""Wrappers to call pyproject.toml-based build backend hooks.
"""

from typing import TYPE_CHECKING

from ._impl import (
    BuildBackendWarning,
    BackendUnavailable,
    BuildBackendHookCaller,
    CorrelationError,
    HookMissing,
    UnsupportedOperation,
    default_subprocess_runner,
    quiet_subprocess_runner,
)

__version__ = "1.3.3"
__all__ = [
    "BuildBackendWarning",
    "BackendUnavailable",
    "BackendInvalid",
    "CorrelationError",
    "HookMissing",
    "UnsupportedOperation",
    "default_subprocess_runner",
    "quiet_subprocess_runner",
    "BuildBackendHookCaller",
]

BackendInvalid = BackendUnavailable  # Deprecated alias, previously a separate exception

if TYPE_CHECKING:
    from ._impl import SubprocessRunner

    __all__ += ["SubprocessRunner"]
