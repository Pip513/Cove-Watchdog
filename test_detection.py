"""Detection and scheduling tests against synthetic data.

The live fleet is healthy, so a real run never exercises the alerting path.
These cases drive the rules through the failures we actually care about -
above all the one that motivated per-datasource evaluation: Files backing up
fine while SQL is dead.

Run:  python test_detection.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from cove.detection import (
    Config,
    SkipReason,
    Verdict,
    evaluate_device,
    verify_scope,
)
from cove.errors import CoveConfigError
from cove.devices import DatasourceStatus, Device
from cove.report import ReportConfig, is_report_due

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
CONFIG = Config(
    threshold_hours=4.0,
    grace_hours=24.0,
    ignore_profiles=["ignore-monitor"],
)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" - {detail}" if detail else ""))
        _failures.append(name)


def ago(hours: float | None) -> datetime | None:
    return None if hours is None else NOW - timedelta(hours=hours)


def source(code: str, hours: float | None, *, in_flight: bool = False, status: str = "Completed"):
    return DatasourceStatus(
        code=code,
        last_success=ago(hours),
        session_status=status,
        in_flight=in_flight,
    )


def server(*sources, created_hours_ago: float = 1000.0, profile: str = "1 hour RPO Server"):
    return Device(
        account_id=1,
        partner_id=1,
        name="test",
        computer_name="TESTSERVER",
        customer="Test Customer",
        os_type=2,
        os_version="Windows Server 2022",
        created=ago(created_hours_ago),
        profile=profile,
        profile_id=1,
        active_codes=[s.code for s in sources],
        datasources=list(sources),
    )


print("Detection rules\n")

# --- the case that motivated the whole design ---------------------------
r = evaluate_device(
    server(source("D01", 0.5), source("D02", 0.5), source("D10", 72.0)), CONFIG, NOW
)
check("SQL dead while Files and System State are fine -> alerts", r.alerting)
check(
    "  names the failing datasource, not just the device",
    any("MsSql" in f.describe() for f in r.problems),
    r.summary(),
)
check(
    "  does not flag the healthy datasources",
    len(r.problems) == 1,
    f"{len(r.problems)} problems: {r.summary()}",
)

# --- healthy -------------------------------------------------------------
r = evaluate_device(server(source("D01", 0.5), source("D02", 1.0)), CONFIG, NOW)
check("all datasources fresh -> no alert", not r.alerting)

# --- the false-positive guard -------------------------------------------
r = evaluate_device(
    server(source("D01", 0.9, in_flight=True, status="InProcess"), source("D02", 0.5)),
    CONFIG,
    NOW,
)
check("backup running now, recent success -> no alert", not r.alerting, r.summary())

# --- the stuck-session case: a job wedged for weeks ----------------------
r = evaluate_device(
    server(source("D01", 384.0, in_flight=True, status="InProcess"), source("D02", 0.5)),
    CONFIG,
    NOW,
)
check("stuck InProcess for 16 days -> alerts", r.alerting)
check(
    "  alert text mentions the running-but-not-succeeding session",
    any("not succeeded" in f.describe() for f in r.problems),
    r.summary(),
)

# --- boundary ------------------------------------------------------------
check("3.9h old -> no alert", not evaluate_device(server(source("D01", 3.9)), CONFIG, NOW).alerting)
check("4.1h old -> alerts", evaluate_device(server(source("D01", 4.1)), CONFIG, NOW).alerting)

# --- never backed up -----------------------------------------------------
r = evaluate_device(server(source("D01", None)), CONFIG, NOW)
check("never succeeded, past grace -> alerts", r.alerting)
check(
    "  reported as 'never', not as an age",
    r.problems and r.problems[0].verdict is Verdict.NEVER,
)

# --- grace period --------------------------------------------------------
r = evaluate_device(server(source("D01", None), created_hours_ago=5.0), CONFIG, NOW)
check("new device mid-seed -> skipped", r.skipped is SkipReason.IN_GRACE_PERIOD)
r = evaluate_device(server(source("D01", None), created_hours_ago=25.0), CONFIG, NOW)
check("same device past 24h grace -> alerts", r.alerting)

# --- scope: default is all servers --------------------------------------
workstation = server(source("D01", 500.0))
workstation.os_type = 1
check(
    "stale workstation -> skipped",
    evaluate_device(workstation, CONFIG, NOW).skipped is SkipReason.NOT_A_SERVER,
)

m365 = server(source("D19", 500.0))
m365.os_type = 0
check(
    "stale M365 tenant -> skipped",
    evaluate_device(m365, CONFIG, NOW).skipped is SkipReason.NOT_A_SERVER,
)

print("\nProfile scoping\n")

# --- mute ----------------------------------------------------------------
r = evaluate_device(server(source("D01", 500.0), profile="IGNORE-MONITOR"), CONFIG, NOW)
check("ignore profile -> skipped", r.skipped is SkipReason.IGNORED_PROFILE)
r = evaluate_device(server(source("D01", 500.0), profile="  ignore-monitor  "), CONFIG, NOW)
check("ignore match ignores case and surrounding space", r.skipped is SkipReason.IGNORED_PROFILE)
r = evaluate_device(server(source("D01", 500.0), profile=""), CONFIG, NOW)
check("empty profile is not treated as ignored", r.alerting)

# --- the substring collision this design exists to avoid -----------------
SUBSTRING_TRAP = Config(ignore_profiles=["1 hour rpo"])
r = evaluate_device(server(source("D01", 500.0), profile="1 hour RPO Server"), SUBSTRING_TRAP, NOW)
check(
    "'1 hour RPO' does NOT silently mute '1 hour RPO Server'",
    r.alerting,
    "substring matching would have muted a server here",
)

# --- monitor profile -----------------------------------------------------
MONITORED = Config(monitor_profiles=["1 hour rpo server"])
check(
    "monitor profile match -> evaluated",
    evaluate_device(server(source("D01", 500.0), profile="1 hour RPO Server"), MONITORED, NOW).alerting,
)
check(
    "profile outside the monitor list -> skipped",
    evaluate_device(server(source("D01", 500.0), profile="1 hour RPO"), MONITORED, NOW).skipped
    is SkipReason.NOT_MONITORED,
)

ws = server(source("D01", 500.0), profile="1 hour RPO Server")
ws.os_type = 1
check(
    "monitor profile overrides the server test",
    evaluate_device(ws, MONITORED, NOW).alerting,
    "a workstation on a monitored profile should still be checked",
)

# --- comma-separated lists ----------------------------------------------
MULTI = Config(monitor_profiles=["1 hour rpo server", "1 hour rpo"])
for profile in ("1 hour RPO Server", "1 hour RPO"):
    check(
        f"comma-separated monitor list includes {profile!r}",
        evaluate_device(server(source("D01", 500.0), profile=profile), MULTI, NOW).alerting,
    )
check(
    "profile in neither entry of the list -> skipped",
    evaluate_device(server(source("D01", 500.0), profile="4 hour RPO"), MULTI, NOW).skipped
    is SkipReason.NOT_MONITORED,
)

BOTH = Config(monitor_profiles=["1 hour rpo server"], ignore_profiles=["1 hour rpo server"])
check(
    "ignore wins when a profile is in both lists",
    evaluate_device(server(source("D01", 500.0), profile="1 hour RPO Server"), BOTH, NOW).skipped
    is SkipReason.IGNORED_PROFILE,
)

# --- ordering ------------------------------------------------------------
never = evaluate_device(server(source("D01", None)), CONFIG, NOW)
old = evaluate_device(server(source("D01", 100.0)), CONFIG, NOW)
check(
    "never-backed-up outranks merely old",
    never.worst_age_hours is None and old.worst_age_hours == 100.0,
)

print("\nScope verification (stale or mistyped profile names)\n")

FLEET = [
    server(source("D01", 1.0), profile="1 hour RPO Server"),
    server(source("D01", 1.0), profile="1 hour RPO Workstation"),
]


def raises_config_error(config: Config) -> bool:
    try:
        verify_scope(FLEET, config)
    except CoveConfigError:
        return True
    return False


check(
    "monitor profile matching no device -> refuses to run",
    raises_config_error(Config(monitor_profiles=["1 hour RPO"])),
    "a renamed profile would otherwise report a serene all-clear over nothing",
)
try:
    verify_scope(FLEET, Config(monitor_profiles=["1 hour RPO"]))
    _msg = ""
except CoveConfigError as exc:
    _msg = str(exc)
check("  error lists profiles currently in use", "1 hour RPO Workstation" in _msg, _msg)

check(
    "monitor profile that matches -> no error",
    not raises_config_error(Config(monitor_profiles=["1 hour RPO Server"])),
)
check(
    "one good and one bad entry -> still refuses",
    raises_config_error(
        Config(monitor_profiles=["1 hour RPO Server", "Typo Profile"])
    ),
)
check(
    "no monitor profile set -> nothing to verify",
    not raises_config_error(Config()),
)
check(
    "stale ignore profile warns rather than blocking",
    verify_scope(FLEET, Config(ignore_profiles=["No Monitoring"])) != [],
)
check(
    "valid ignore profile produces no warning",
    verify_scope(FLEET, Config(ignore_profiles=["1 hour RPO Workstation"])) == [],
)

print("\nWeekly report scheduling\n")

RC = ReportConfig(enabled=True, day="monday", hour=8)
EASTERN = ZoneInfo("America/New_York")


def at(local_str: str) -> datetime:
    """Build a UTC instant from an Eastern wall-clock time."""
    naive = datetime.strptime(local_str, "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=EASTERN).astimezone(timezone.utc)


# 2026-09-21 is a Monday; 2026-09-22 a Tuesday.
check("Monday 08:00 -> due", is_report_due(at("2026-09-21 08:00"), CONFIG, RC, None))
check("Monday 07:59 -> not yet", not is_report_due(at("2026-09-21 07:59"), CONFIG, RC, None))
check("Tuesday 08:00 -> not due", not is_report_due(at("2026-09-22 08:00"), CONFIG, RC, None))
check(
    "already sent today -> not due again",
    not is_report_due(at("2026-09-21 11:00"), CONFIG, RC, at("2026-09-21 08:00")),
)
check(
    "sent last week -> due again this Monday",
    is_report_due(at("2026-09-21 08:00"), CONFIG, RC, at("2026-09-14 08:00")),
)
check(
    "missed the 08:00 run -> still sends later the same day",
    is_report_due(at("2026-09-21 14:00"), CONFIG, RC, None),
)
check(
    "disabled -> never due",
    not is_report_due(at("2026-09-21 08:00"), CONFIG, ReportConfig(enabled=False), None),
)

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All tests pass.")
