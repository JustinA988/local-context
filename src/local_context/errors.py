"""Errors whose text is safe to return to the model."""


class LocalError(Exception):
    """A request the server refused or could not complete."""
