"""Missed-backup detection rules.

Evaluates each active datasource on each server independently. Cove's aggregate
"Total" datasource reports the most recent success across sources, so a server
whose Files backup runs hourly looks healthy even when its SQL backup has been
dead for weeks. Per-source evaluation is the whole point.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from .devices import DatasourceStatus, Device
from .errors import CoveConfigError


class Verdict(str, Enum):
    OK = "ok"
    STALE = "stale"  # succeeded before, but too long ago
    NEVER = "never"  # no successful backup on record


class SkipReason(str, Enum):
    NOT_A_SERVER = "not a server"
    NOT_MONITORED = "not on a monitored profile"
    IGNORED_PROFILE = "assigned to the ignore profile"
    IN_GRACE_PERIOD = "within the post-creation grace period"
    NO_DATASOURCES = "no active datasources"


def _profile_list(raw: str | None) -> list[str]:
    """Split a comma-separated profile setting.

    Original casing is kept so config echoes back the way it was written;
    normalisation happens at match time.
    """
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _matches_profile(profile: str | None, wanted: list[str]) -> bool:
    """Exact, case-insensitive match against any configured profile name.

    Exact rather than substring on purpose. Real Cove fleets have profiles like
    "1 hour RPO" for workstations and "1 hour RPO Server" for servers; a
    substring match on the former would silently pull in the latter.
    """
    if not wanted or not profile:
        return False
    return profile.strip().lower() in {w.strip().lower() for w in wanted}


@dataclass
class Config:
    #: Hours without a successful backup before a datasource is considered stale.
    threshold_hours: float = 4.0
    #: Hours after device creation during which it cannot alert. Covers the
    #: initial seed, which legitimately runs long and has no prior success.
    grace_hours: float = 24.0
    #: Profile names to monitor, as shown in the console, e.g.
    #: ["1 hour rpo server"]. When empty, every server (I32 == 2) is monitored.
    #: When set, this replaces the server test - so a workstation profile can
    #: be monitored too if that is what you want.
    monitor_profiles: list[str] = field(default_factory=list)
    #: Profile names that are never alerted on. Always wins over the above.
    ignore_profiles: list[str] = field(default_factory=list)
    #: Display timezone for human-readable output and report scheduling.
    display_timezone: str = "America/New_York"
    #: Local hour at which a still-failing device is alerted on again. The
    #: first alert fires immediately; repeats land at a predictable time.
    realert_hour: int = 8
    #: Above this many device alerts in one run, the caller sends a single
    #: summary instead. Guards against a site-wide outage - or a bug here -
    #: producing a hundred emails.
    max_emails_per_run: int = 25

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            threshold_hours=float(os.getenv("WATCHDOG_THRESHOLD_HOURS", "4")),
            grace_hours=float(os.getenv("WATCHDOG_GRACE_HOURS", "24")),
            monitor_profiles=_profile_list(os.getenv("WATCHDOG_MONITOR_PROFILE")),
            ignore_profiles=_profile_list(os.getenv("WATCHDOG_IGNORE_PROFILE")),
            display_timezone=os.getenv("WATCHDOG_TIMEZONE", "America/New_York"),
            realert_hour=int(os.getenv("WATCHDOG_REALERT_HOUR", "8")),
            max_emails_per_run=int(os.getenv("WATCHDOG_MAX_EMAILS_PER_RUN", "25")),
        )

    def validate(self) -> None:
        """Fail fast on a misconfigured timezone.

        The daily re-alert fires at a local wall-clock time, so a timezone that
        silently falls back to UTC would send at the wrong hour without ever
        complaining. Better to refuse at startup.
        """
        try:
            ZoneInfo(self.display_timezone)
        except Exception as exc:
            raise CoveConfigError(
                f"WATCHDOG_TIMEZONE {self.display_timezone!r} could not be "
                f"resolved ({exc}). Use an IANA name such as America/New_York. "
                "On Windows this also requires the 'tzdata' package."
            ) from exc

        if self.threshold_hours <= 0:
            raise CoveConfigError("WATCHDOG_THRESHOLD_HOURS must be greater than 0")
        if self.grace_hours < 0:
            raise CoveConfigError("WATCHDOG_GRACE_HOURS cannot be negative")
        if not 0 <= self.realert_hour <= 23:
            raise CoveConfigError("WATCHDOG_REALERT_HOUR must be between 0 and 23")
        if self.max_emails_per_run < 1:
            raise CoveConfigError("WATCHDOG_MAX_EMAILS_PER_RUN must be at least 1")


@dataclass
class DatasourceFinding:
    datasource: DatasourceStatus
    verdict: Verdict
    age_hours: float | None

    @property
    def is_problem(self) -> bool:
        return self.verdict is not Verdict.OK

    def describe(self) -> str:
        name = self.datasource.name
        if self.verdict is Verdict.NEVER:
            return f"{name}: no successful backup on record"
        if self.verdict is Verdict.STALE:
            days = (self.age_hours or 0) / 24
            span = f"{self.age_hours:,.1f}h" if (self.age_hours or 0) < 48 else f"{days:,.1f} days"
            note = " (a session is running but has not succeeded)" if self.datasource.in_flight else ""
            return f"{name}: last success {span} ago{note}"
        return f"{name}: ok"


@dataclass
class DeviceResult:
    device: Device
    skipped: SkipReason | None = None
    findings: list[DatasourceFinding] = field(default_factory=list)

    @property
    def problems(self) -> list[DatasourceFinding]:
        return [f for f in self.findings if f.is_problem]

    @property
    def alerting(self) -> bool:
        return self.skipped is None and bool(self.problems)

    @property
    def worst_age_hours(self) -> float | None:
        """Oldest stale datasource. None outranks any number - never is worst."""
        problems = self.problems
        if not problems:
            return None
        if any(p.verdict is Verdict.NEVER for p in problems):
            return None
        return max(p.age_hours or 0.0 for p in problems)

    def summary(self) -> str:
        if self.skipped:
            return f"skipped - {self.skipped.value}"
        if not self.problems:
            return "ok"
        return "; ".join(f.describe() for f in self.problems)


def evaluate_device(device: Device, config: Config, now: datetime) -> DeviceResult:
    """Apply the rules to one device, in scope-narrowing order."""
    # Muting wins over everything, so a device can always be silenced by
    # moving it to the ignore profile in the console.
    if _matches_profile(device.profile, config.ignore_profiles):
        return DeviceResult(device, skipped=SkipReason.IGNORED_PROFILE)

    if config.monitor_profiles:
        if not _matches_profile(device.profile, config.monitor_profiles):
            return DeviceResult(device, skipped=SkipReason.NOT_MONITORED)
    elif not device.is_server:
        return DeviceResult(device, skipped=SkipReason.NOT_A_SERVER)

    device_age = device.age_hours(now)
    if device_age is not None and device_age < config.grace_hours:
        return DeviceResult(device, skipped=SkipReason.IN_GRACE_PERIOD)

    if not device.datasources:
        return DeviceResult(device, skipped=SkipReason.NO_DATASOURCES)

    findings = []
    for source in device.datasources:
        age = source.age_hours(now)
        if age is None:
            verdict = Verdict.NEVER
        elif age > config.threshold_hours:
            verdict = Verdict.STALE
        else:
            verdict = Verdict.OK
        findings.append(DatasourceFinding(source, verdict, age))

    return DeviceResult(device, findings=findings)


def evaluate_all(
    devices: list[Device], config: Config, now: datetime | None = None
) -> list[DeviceResult]:
    now = now or datetime.now(tz=timezone.utc)
    return [evaluate_device(d, config, now) for d in devices]


def verify_scope(devices: list[Device], config: Config) -> list[str]:
    """Check configured profile names against the profiles that actually exist.

    Exact matching means a mistyped or renamed profile matches nothing, and a
    monitor list that matches nothing produces a serene "no alerts" over a fleet
    nobody is watching. Profiles get renamed in the console without anyone
    touching this config, so this is a live failure mode, not a hypothetical.

    An unmatched monitor profile raises. An unmatched ignore profile only warns:
    the result there is unwanted noise rather than false silence.
    """
    existing = {d.profile.strip().lower() for d in devices if d.profile}

    unmatched_monitor = [
        p for p in config.monitor_profiles if p.strip().lower() not in existing
    ]
    if unmatched_monitor:
        available = ", ".join(sorted(p for p in {d.profile for d in devices if d.profile}))
        raise CoveConfigError(
            "WATCHDOG_MONITOR_PROFILE names profiles that no device uses: "
            + ", ".join(repr(p) for p in unmatched_monitor)
            + f". Profiles currently in use: {available or '(none)'}. "
            "Refusing to run - monitoring nothing would look identical to "
            "everything being healthy."
        )

    return [
        f"WATCHDOG_IGNORE_PROFILE names {p!r}, which no device uses. "
        "Nothing is muted by that entry."
        for p in config.ignore_profiles
        if p.strip().lower() not in existing
    ]
