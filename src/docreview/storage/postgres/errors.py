"""由 HTTP 适配器映射的稳定 repository 错误。"""


class RecordNotFoundError(LookupError):
    pass


class SessionNotFoundError(LookupError):
    pass


class FileContentNotFoundError(LookupError):
    pass


__all__ = ["FileContentNotFoundError", "RecordNotFoundError", "SessionNotFoundError"]
