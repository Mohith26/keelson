"""keelson: a model-driven data layer over split relational and time series storage."""

from .errors import (
    KeelsonError,
    MigrationError,
    ModelError,
    QueryError,
    ResolveError,
    StoreError,
)
from .model import Model, ResolvedField, ResolvedType, load, resolve

__all__ = [
    "KeelsonError",
    "ModelError",
    "ResolveError",
    "QueryError",
    "StoreError",
    "MigrationError",
    "Model",
    "ResolvedType",
    "ResolvedField",
    "load",
    "resolve",
    "Session",
    "open_session",
]

__version__ = "0.4.0"


def __getattr__(name):
    # Session pulls in sqlite3 and the planner; keep `import keelson` cheap
    # for callers that only want to compile a model.
    if name in ("Session", "open_session"):
        from .session import Session, open_session

        return {"Session": Session, "open_session": open_session}[name]
    raise AttributeError(name)
