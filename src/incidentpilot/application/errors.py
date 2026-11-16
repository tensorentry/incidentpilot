"""Why an application service refused a request. Messages are safe to show the caller."""


class RequestError(Exception):
    """The request can't be carried out as given."""


class NotFound(RequestError):
    """The resource doesn't exist for this owner. Another owner's resource looks the same."""


class IdempotencyConflict(RequestError):
    """The key was already used for a different request."""


class Rejected(RequestError):
    """The request is well formed, but the system won't accept it, such as a full queue."""
