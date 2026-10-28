"""Stable error codes exposed at the API boundary."""


class TriageError(Exception):
    def __init__(
        self, code: str, message: str, status_code: int, *, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after
