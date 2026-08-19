"""LangSmith tracing configuration for clinical trial agents.

Configures LANGCHAIN_TRACING_V2 and provides @traceable wrapper helpers
that inject trial/patient/session metadata using UUIDs only (no raw PHI/PII).
"""

import os
import functools
from typing import Callable

from langsmith import traceable

from config.settings import LANGSMITH_PROJECTS


def configure_tracing(project_key: str = "screening") -> None:
    """Enable LangSmith tracing for the given project.

    Args:
        project_key: One of 'screening', 'monitoring', 'protocol', 'safety'.
    """
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_PROJECT"] = LANGSMITH_PROJECTS[project_key]


def traced_screening(
    trial_id: str, patient_id: str, session_id: str
) -> Callable:
    """Decorator for screening-related functions with metadata injection.

    Metadata uses UUIDs only — no raw PHI/PII in trace names or metadata.
    """

    def decorator(fn: Callable) -> Callable:
        @traceable(
            run_type="chain",
            name=f"screening_{session_id}",
            metadata={
                "trial_id": trial_id,
                "patient_id": patient_id,
                "session_id": session_id,
            },
        )
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def traced_monitoring(
    trial_id: str, patient_id: str, session_id: str
) -> Callable:
    """Decorator for monitoring-related functions with metadata injection."""

    def decorator(fn: Callable) -> Callable:
        @traceable(
            run_type="chain",
            name=f"monitoring_{session_id}",
            metadata={
                "trial_id": trial_id,
                "patient_id": patient_id,
                "session_id": session_id,
            },
        )
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def traced_tool(
    tool_name: str,
    trial_id: str | None = None,
    patient_id: str | None = None,
    session_id: str | None = None,
) -> Callable:
    """Decorator for tool-level tracing with optional metadata."""
    metadata = {}
    if trial_id:
        metadata["trial_id"] = trial_id
    if patient_id:
        metadata["patient_id"] = patient_id
    if session_id:
        metadata["session_id"] = session_id

    def decorator(fn: Callable) -> Callable:
        @traceable(
            run_type="tool",
            name=tool_name,
            metadata=metadata,
        )
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        return wrapper

    return decorator
