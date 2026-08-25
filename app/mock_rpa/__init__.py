"""Competition-safe mock RPA service maintained with the application code."""

from .server import app, notifications_db, tasks_db

__all__ = ["app", "tasks_db", "notifications_db"]
