"""Failure-path tests.

The success path is easy to verify by running it. These cover what happens when
things break - which is the part that decides whether silence means "all good"
or "nobody is watching".

Run:  python test_failures.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from cove.client import CoveCredentials
from cove.detection import Config, evaluate_all
from cove.devices import DatasourceStatus, Device
from cove.errors import (
    CoveApiError,
    CoveAuthError,
    CoveConfigError,
    CoveDataError,
    CoveTransportError,
)
from cove.health import (
    CheckFailure,
    FailureKind,
    classify,
    failure_body,
    failure_subject,
    should_send_failure,
)
from cove.notify import EmailDeliveryError, SmtpConfig
from cove.report import ReportConfig
from cove.runner import CheckOutcome, run_check

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
TZ = "America/New_York"

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" - {detail}" if detail else ""))
        _failures.append(name)


print("Failure classification\n")

CASES = [
    (CoveConfigError("bad profile"), FailureKind.CONFIGURATION),
    (CoveAuthError("bad token", method="Login", code=2100), FailureKind.AUTHENTICATION),
    (CoveDataError("columns missing"), FailureKind.UNTRUSTED_DATA),
    (CoveTransportError("connection refused"), FailureKind.API_UNREACHABLE),
    (CoveApiError("permission denied", method="X"), FailureKind.API_ERROR),
    (ValueError("something odd"), FailureKind.UNEXPECTED),
]
for exc, expected in CASES:
    got = classify(exc).kind
    check(f"{type(exc).__name__} -> {expected.value}", got is expected, f"got {got.value}")

check(
    "auth error is not misread as a generic API error",
    classify(CoveAuthError("x", method="Login")).kind is FailureKind.AUTHENTICATION,
    "CoveAuthError subclasses CoveApiError, so ordering matters",
)

print("\nAlert suppression when the fleet's state is unknown\n")

for kind in (
    FailureKind.CONFIGURATION,
    FailureKind.AUTHENTICATION,
    FailureKind.API_UNREACHABLE,
    FailureKind.UNTRUSTED_DATA,
):
    check(
        f"{kind.value} suppresses device alerts",
        CheckFailure(kind, "x").alerts_suppressed,
    )
check(
    "email failure does NOT suppress - the alerts were real",
    not CheckFailure(FailureKind.EMAIL, "x").alerts_suppressed,
)


def stale_device() -> Device:
    source = DatasourceStatus(
        code="D01",
        last_success=NOW - timedelta(hours=500),
        session_status="Completed",
        in_flight=False,
    )
    return Device(
        account_id=1, partner_id=1, name="test", computer_name="TESTSERVER",
        customer="Test", os_type=2, os_version="Windows Server 2022",
        created=NOW - timedelta(days=100), profile="1 hour RPO Server",
        profile_id=1, active_codes=["D01"], datasources=[source],
    )


outcome = CheckOutcome(now=NOW, results=evaluate_all([stale_device()], Config(), NOW))
check("a clean run reports its alerting devices", len(outcome.alerting) == 1)
outcome.failure = CheckFailure(FailureKind.AUTHENTICATION, "token rejected")
check(
    "the same run with a failure reports NO alerting devices",
    outcome.alerting == [],
    "a device known to be stale must still not be alerted on when the run failed",
)

print("\nFailure email suppression\n")

check("first failure sends immediately", should_send_failure(NOW, None))
check(
    "one hour later does not resend",
    not should_send_failure(NOW + timedelta(hours=1), NOW),
)
check(
    "25 hours later resends",
    should_send_failure(NOW + timedelta(hours=25), NOW),
)
# Simulate a week-long outage against an hourly schedule.
_sent = 0
_last: datetime | None = None
for _hour in range(168):
    _at = NOW + timedelta(hours=_hour)
    if should_send_failure(_at, _last):
        _sent += 1
        _last = _at
check(
    "a week-long outage produces 7 emails, not 168",
    _sent == 7,
    f"sent {_sent}",
)

print("\nFailure email content\n")

failure = classify(CoveAuthError("Unknown partner/username or bad password", code=2100))
body = failure_body(failure, NOW, TZ, first_failed_at=NOW - timedelta(hours=30))
check("subject names the failure kind", "authentication" in failure_subject(failure))
check("body states that no alerts were sent", "NO BACKUP ALERTS WERE SENT" in body)
check("body says backups may be fine or may not", "may be fine" in body)
check("body gives remediation", "API Users" in body)
check("body shows how long it has been failing", "30.0h" in body, body)

cfg_failure = classify(CoveConfigError("WATCHDOG_MONITOR_PROFILE names '1 hour RPO'"))
cfg_body = failure_body(cfg_failure, NOW, TZ)
check(
    "config failure points at renamed profiles",
    "renamed in the Cove console" in cfg_body,
)

email_failure = CheckFailure(FailureKind.EMAIL, "SMTP auth rejected")
check(
    "email failure body omits the suppression notice",
    "NO BACKUP ALERTS WERE SENT" not in failure_body(email_failure, NOW, TZ),
)

print("\nrun_check never raises\n")

bad_config = Config(display_timezone="Mars/Olympus")
outcome = run_check(bad_config, ReportConfig())
check("invalid timezone -> failure, not exception", outcome.failure is not None)
check("  classified as configuration", outcome.failure.kind is FailureKind.CONFIGURATION)
check("  no devices fetched", outcome.devices == [])
check("  no alerts", outcome.alerting == [])

bad_report = ReportConfig(day="funday")
outcome = run_check(Config(), bad_report)
check("invalid report day -> configuration failure", outcome.failure.kind is FailureKind.CONFIGURATION)

unreachable = CoveCredentials(
    partner="x", username="y", password="z", endpoint="http://127.0.0.1:9"
)
outcome = run_check(Config(), ReportConfig(), credentials=unreachable)
check(
    "unreachable endpoint -> api unreachable, no exception",
    outcome.failure is not None
    and outcome.failure.kind is FailureKind.API_UNREACHABLE,
    str(outcome.failure.kind if outcome.failure else "no failure"),
)
check("  still reports no alerts", outcome.alerting == [])

print("\nSMTP configuration errors\n")

for name, cfg in [
    ("missing host", SmtpConfig(from_address="a@b.c", to_addresses=["d@e.f"])),
    ("missing sender", SmtpConfig(host="h", to_addresses=["d@e.f"])),
    ("missing recipient", SmtpConfig(host="h", from_address="a@b.c")),
    ("bad security mode", SmtpConfig(host="h", from_address="a@b.c", to_addresses=["d@e.f"], security="quantum")),
    ("username without password", SmtpConfig(host="h", from_address="a@b.c", to_addresses=["d@e.f"], username="u")),
]:
    try:
        cfg.validate()
        ok = False
    except CoveConfigError:
        ok = True
    check(f"{name} -> rejected", ok)

good = SmtpConfig(host="h", from_address="a@b.c", to_addresses=["d@e.f"])
try:
    good.validate()
    ok = True
except CoveConfigError:
    ok = False
check("valid minimal config -> accepted", ok)
check("unauthenticated relay is allowed", not good.uses_auth)

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All failure-path tests pass.")
