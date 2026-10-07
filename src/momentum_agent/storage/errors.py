"""Typed errors shared by storage backends and HTTP handlers."""


class IdempotencyConflict(Exception):
    """A previously used idempotency key was submitted with different data."""


class FocusTaskNotFound(Exception):
    """The focus task is not owned by the authenticated user."""


class TaskCannotBePostponed(Exception):
    """The owned task has no due date or is not in an open status."""
