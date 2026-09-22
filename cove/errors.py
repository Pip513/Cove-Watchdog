"""Exception types for the Cove JSON-RPC client."""


class CoveError(Exception):
    """Base class for all Cove client failures."""


class CoveConfigError(CoveError):
    """Required configuration is missing or malformed."""


class CoveTransportError(CoveError):
    """The request never produced a usable HTTP response."""


class CoveApiError(CoveError):
    """The API returned a JSON-RPC error.

    Cove documents a "custom JSON-RPC protocol" but does not publish its error
    payload shape, so the raw body is retained for inspection. Phase 1 should
    deliberately trigger a few failures (bad password, bad method) and record
    what actually comes back.
    """

    def __init__(self, message, *, method=None, code=None, payload=None):
        super().__init__(message)
        self.method = method
        self.code = code
        self.payload = payload


class CoveAuthError(CoveApiError):
    """Login was rejected, or a call failed because the visa is not valid."""


class CoveDataError(CoveError):
    """The response arrived but cannot be trusted.

    Distinct from CoveApiError because the API reported success. A malformed
    column code returns no error and no data, so an incomplete response would
    otherwise read as "nothing has ever backed up" and alert on a whole fleet.
    """
