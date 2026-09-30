"""Small domain models used by device operations."""


class OperationResult(str):
    """A user-facing message with machine-readable outcome metadata.

    It intentionally subclasses ``str`` so older integrations that render or
    compare operation messages keep working while new code can use ``success``
    and ``code`` instead of parsing Russian text prefixes.
    """

    def __new__(cls, message, *, success, code="ok", detail=""):
        value = super().__new__(cls, str(message))
        value.success = bool(success)
        value.code = str(code)
        value.detail = str(detail or "")
        return value

    def as_dict(self):
        return {
            "success": self.success,
            "code": self.code,
            "message": str(self),
            "detail": self.detail,
        }


def success(message, code="ok", detail=""):
    return OperationResult(message, success=True, code=code, detail=detail)


def failure(message, code="error", detail=""):
    return OperationResult(message, success=False, code=code, detail=detail)
