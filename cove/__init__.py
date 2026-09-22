"""Client library for the Cove Data Protection JSON-RPC API."""

from .client import CoveClient, CoveCredentials
from .errors import (
    CoveApiError,
    CoveAuthError,
    CoveConfigError,
    CoveDataError,
    CoveError,
    CoveTransportError,
)

__all__ = [
    "CoveClient",
    "CoveCredentials",
    "CoveError",
    "CoveConfigError",
    "CoveDataError",
    "CoveTransportError",
    "CoveApiError",
    "CoveAuthError",
]
