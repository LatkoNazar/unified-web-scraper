"""Provider-neutral errors for failed or oversized page requests."""


class FetchError(RuntimeError):
    """A request failed after bounded retries or was rejected by the origin."""

    def __init__(self, url: str, message: str, *, status_code: int | None = None):
        super().__init__(f"{url}: {message}")
        self.url = url
        self.status_code = status_code
