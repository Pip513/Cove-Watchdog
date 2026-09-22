"""Failures of the check itself.

A backup monitor that dies quietly is worse than no monitor: nobody is watching,
and the absence of alerts reads exactly like good news. So every way this can
fail is classified, suppressed from spamming, and mailed out with the fix.

Two rules hold throughout:

1. When the fleet's state cannot be established - bad credentials, the API
   unreachable, a response we do not trust - no backup alerts are sent at all.
   A false all-clear is worse than no message. The failure email goes instead.

2. The failure email is sent on the first failure and then at most once a day,
   matching the device alert cadence. An outage lasting a week produces seven
   emails, not one hundred and sixty-eight.

If SMTP itself is down, nothing can be delivered and the weekly report's absence
becomes the last line of defence. That is why the report exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .errors import (
    CoveApiError,
    CoveAuthError,
    CoveConfigError,
    CoveDataError,
    CoveTransportError,
)


class FailureKind(str, Enum):
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    API_UNREACHABLE = "api unreachable"
    API_ERROR = "api error"
    UNTRUSTED_DATA = "untrusted data"
    EMAIL = "email delivery"
    UNEXPECTED = "unexpected"


#: What to actually do about each kind, in the email.
REMEDIATION: dict[FailureKind, str] = {
    FailureKind.CONFIGURATION: (
        "A setting is wrong or has gone stale. The message above names it.\n"
        "Profile names are matched exactly and are the usual culprit - if a\n"
        "backup profile was renamed in the Cove console, update\n"
        "WATCHDOG_MONITOR_PROFILE or WATCHDOG_IGNORE_PROFILE to match."
    ),
    FailureKind.AUTHENTICATION: (
        "Cove rejected the API credentials. Common causes:\n"
        "  - the API user was deleted or disabled\n"
        "  - its token was regenerated, so the stored one is stale\n"
        "  - the customer/partner name changed (it must match the console\n"
        "    exactly, including the contact email in parentheses)\n"
        "Recreate the API user under Management > Users > API Users and update\n"
        "the stored credentials. The token is shown only once."
    ),
    FailureKind.API_UNREACHABLE: (
        "The Cove API could not be reached. This is often transient - a network\n"
        "blip or maintenance - and the next hourly run may simply succeed.\n"
        "If it persists for more than a few hours, check status.n-able.com and\n"
        "whether outbound HTTPS from the host is being blocked."
    ),
    FailureKind.API_ERROR: (
        "Cove accepted the request but returned an error. If this repeats,\n"
        "the API user may lack a permission it needs: it requires a role that\n"
        "can read devices, customers and backup profiles."
    ),
    FailureKind.UNTRUSTED_DATA: (
        "Cove returned a response that cannot be trusted, so no backup alerts\n"
        "were evaluated. Malformed column codes are ignored silently by the\n"
        "API - they return no error and no data - which would otherwise look\n"
        "identical to a fleet that has never backed up.\n"
        "This usually means Cove changed its statistics columns and the code\n"
        "needs updating. Check the devices in the console directly until it is."
    ),
    FailureKind.EMAIL: (
        "Alerts were produced but could not be delivered. Check the SMTP host,\n"
        "port, credentials and that the sending address is authorised."
    ),
    FailureKind.UNEXPECTED: (
        "An unhandled error. The detail above is the raw exception; treat this\n"
        "as a bug in the watchdog rather than a problem with Cove."
    ),
}


@dataclass
class CheckFailure:
    kind: FailureKind
    message: str
    detail: str | None = None

    @property
    def remediation(self) -> str:
        return REMEDIATION[self.kind]

    @property
    def alerts_suppressed(self) -> bool:
        """Whether this failure means no backup alerts could be evaluated."""
        return self.kind is not FailureKind.EMAIL


def classify(exc: BaseException) -> CheckFailure:
    """Map an exception to a failure kind.

    Order matters: CoveAuthError subclasses CoveApiError, so it is tested first.
    """
    detail = f"{type(exc).__name__}: {exc}"

    if isinstance(exc, CoveConfigError):
        return CheckFailure(FailureKind.CONFIGURATION, str(exc), detail)
    if isinstance(exc, CoveAuthError):
        return CheckFailure(FailureKind.AUTHENTICATION, str(exc), detail)
    if isinstance(exc, CoveDataError):
        return CheckFailure(FailureKind.UNTRUSTED_DATA, str(exc), detail)
    if isinstance(exc, CoveTransportError):
        return CheckFailure(FailureKind.API_UNREACHABLE, str(exc), detail)
    if isinstance(exc, CoveApiError):
        return CheckFailure(FailureKind.API_ERROR, str(exc), detail)
    return CheckFailure(FailureKind.UNEXPECTED, str(exc), detail)


def should_send_failure(
    now: datetime, last_sent: datetime | None, *, repeat_hours: float = 24.0
) -> bool:
    """First failure goes out at once, then at most once a day.

    Unlike device alerts this does not wait for a fixed hour: a blind watchdog
    should be reported as soon as it goes blind, not the next morning.
    """
    if last_sent is None:
        return True
    return now - last_sent >= timedelta(hours=repeat_hours)


def failure_subject(failure: CheckFailure) -> str:
    return f"Backup check FAILED - {failure.kind.value}"


def failure_body(
    failure: CheckFailure,
    now: datetime,
    timezone_name: str,
    *,
    first_failed_at: datetime | None = None,
) -> str:
    from .messages import _local  # local import keeps the modules decoupled

    lines = [
        "The backup check could not complete.",
        "",
        f"Problem   {failure.kind.value}",
        f"Detected  {_local(now, timezone_name)}",
    ]
    if first_failed_at is not None and first_failed_at != now:
        hours = (now - first_failed_at).total_seconds() / 3600
        span = f"{hours:,.1f}h" if hours < 48 else f"{hours / 24:,.1f}d"
        lines.append(f"Failing   since {_local(first_failed_at, timezone_name)} ({span})")

    lines += ["", "WHAT HAPPENED", f"  {failure.message}", ""]

    if failure.alerts_suppressed:
        lines += [
            "NO BACKUP ALERTS WERE SENT",
            "  The state of your devices could not be established, so this run",
            "  deliberately sent nothing about them. Backups may be fine or may",
            "  be failing - this check cannot currently tell you which.",
            "",
        ]

    lines += ["WHAT TO DO", *(f"  {line}" for line in failure.remediation.splitlines()), ""]

    if failure.detail and failure.detail != failure.message:
        lines += ["DETAIL", f"  {failure.detail}", ""]

    lines.append("This message repeats once a day until the check succeeds again.")
    return "\n".join(lines)


def recovered_subject() -> str:
    return "Backup check recovered"


def recovered_body(
    failure_kind: FailureKind,
    now: datetime,
    timezone_name: str,
    *,
    first_failed_at: datetime | None = None,
) -> str:
    from .messages import _local

    lines = ["The backup check is working again.", ""]
    if first_failed_at is not None:
        hours = (now - first_failed_at).total_seconds() / 3600
        span = f"{hours:,.1f}h" if hours < 48 else f"{hours / 24:,.1f}d"
        lines += [
            f"Was failing  {failure_kind.value}",
            f"Since        {_local(first_failed_at, timezone_name)} ({span})",
            "",
        ]
    lines += [
        "Device checks have resumed. Any device that started missing backups",
        "during the outage will be reported on the next run.",
    ]
    return "\n".join(lines)
