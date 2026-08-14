"""Stable repository errors mapped by HTTP adapters."""


class RecordNotFoundError(LookupError):
    pass


class SessionNotFoundError(LookupError):
    pass


class FileContentNotFoundError(LookupError):
    pass


__all__ = ["FileContentNotFoundError", "RecordNotFoundError", "SessionNotFoundError"]
