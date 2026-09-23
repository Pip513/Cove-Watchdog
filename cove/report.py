"""The scheduled heartbeat report.

Sent on a fixed weekly slot whether or not anything is wrong, and independently
of the alert path. That is the whole point: a report that only arrives when
there is news cannot tell you the watchdog itself has died. If Monday comes and
no report arrives, something upstream is broken - the function, the credentials,
the mail path - and that silence is the signal.

Because it must survive the thing it is reporting on, it is deliberately sent
even on a run that is also firing alerts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .detection import Config, DeviceResult, SkipReason
from .devices import Device
from .env import env_bool, env_int, env_str
from .errors import CoveConfigError

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass
class ReportConfig:
    enabled: bool = True
    #: Weekday name, lowercase.
    day: str = "monday"
    #: Local hour, 0-23. Matches the daily re-alert hour by default.
    hour: int = 8
    #: How many devices to list before collapsing to a count.
    device_limit: int = 5

    @classmethod
    def from_env(cls) -> "ReportConfig":
        return cls(
            enabled=env_bool("REPORT_ENABLED", True),
            day=env_str("REPORT_DAY", "monday").lower(),
            hour=env_int("REPORT_HOUR", 8),
            device_limit=env_int("REPORT_DEVICE_LIMIT", 5),
        )

    def validate(self) -> None:
        if self.day not in WEEKDAYS:
            raise CoveConfigError(
                f"REPORT_DAY {self.day!r} is not a weekday name. "
                f"Use one of: {', '.join(WEEKDAYS)}."
            )
        if not 0 <= self.hour <= 23:
            raise CoveConfigError("REPORT_HOUR must be between 0 and 23")
        if self.device_limit < 1:
            raise CoveConfigError("REPORT_DEVICE_LIMIT must be at least 1")

    @property
    def weekday(self) -> int:
        return WEEKDAYS[self.day]


def is_report_due(
    now: datetime,
    config: Config,
    report_config: ReportConfig,
    last_sent: datetime | None,
) -> bool:
    """Whether the scheduled report should go out on this run.

    Assumes an hourly check: it fires on the first run at or after the
    configured local hour on the configured day, and `last_sent` stops it
    repeating. A run missed entirely (the function was down, say) still sends
    later that same day rather than skipping the week.
    """
    if not report_config.enabled:
        return False

    local_now = now.astimezone(ZoneInfo(config.display_timezone))
    if local_now.weekday() != report_config.weekday:
        return False
    if local_now.hour < report_config.hour:
        return False

    if last_sent is not None:
        local_last = last_sent.astimezone(ZoneInfo(config.display_timezone))
        if local_last.date() == local_now.date():
            return False

    return True


def _span(hours: float) -> str:
    return f"{hours:,.1f}h" if hours < 48 else f"{hours / 24:,.1f}d"


def _newest_success(result: DeviceResult) -> datetime | None:
    """Most recent successful backup across a device's data sources."""
    stamps = [
        f.datasource.last_success
        for f in result.findings
        if f.datasource.last_success is not None
    ]
    return max(stamps) if stamps else None


def report_subject(results: list[DeviceResult], now: datetime, config: Config) -> str:
    local = now.astimezone(ZoneInfo(config.display_timezone))
    alerting = [r for r in results if r.alerting]
    monitored = [r for r in results if r.skipped is None]
    state = (
        f"{len(alerting)} of {len(monitored)} with missed backups"
        if alerting
        else "all healthy"
    )
    return f"Weekly backup report - {state} - {local.strftime('%d %b %Y')}"


def report_body(
    results: list[DeviceResult],
    devices: list[Device],
    now: datetime,
    config: Config,
    report_config: ReportConfig,
) -> str:
    local = now.astimezone(ZoneInfo(config.display_timezone))
    monitored = [r for r in results if r.skipped is None]
    alerting = [r for r in monitored if r.alerting]
    healthy = [r for r in monitored if not r.alerting]

    lines = [
        f"Weekly backup report - {local.strftime('%A %d %B %Y, %H:%M %Z')}",
        "",
        f"{len(monitored)} monitored, {len(healthy)} healthy, "
        f"{len(alerting)} with missed backups.",
        "",
    ]

    if alerting:
        lines.append("MISSED BACKUPS")
        for result in alerting:
            worst = ", ".join(f.datasource.name for f in result.problems)
            lines.append(f"  {result.device.label:<20} {worst}")
        lines.append("")
        lines.append("A separate alert has been sent for each of these.")
        lines.append("")

    if healthy:
        # Most recently backed up first: the point of this list is to show
        # fresh evidence that backups are running, not to rank problems.
        ranked = sorted(
            healthy,
            key=lambda r: _newest_success(r) or datetime.min.replace(tzinfo=local.tzinfo),
            reverse=True,
        )
        shown = ranked[: report_config.device_limit]

        lines.append("HEALTHY - most recently backed up")
        width = max(len(r.device.label) for r in shown)
        for result in shown:
            newest = _newest_success(result)
            when = (
                newest.astimezone(ZoneInfo(config.display_timezone)).strftime(
                    "%Y-%m-%d %H:%M %Z"
                )
                if newest
                else "never"
            )
            age = (
                f"{_span((now - newest).total_seconds() / 3600)} ago" if newest else ""
            )
            lines.append(f"  {result.device.label:<{width}}   {when:<20}   {age:>9}".rstrip())

        if len(ranked) > len(shown):
            lines.append(f"  ... and {len(ranked) - len(shown)} more, all healthy")
        lines.append("")

        # The oldest healthy device is the one closest to alerting. One line of
        # early warning without printing a second full list.
        oldest = ranked[-1]
        oldest_stamp = _newest_success(oldest)
        if oldest_stamp is not None and len(ranked) > 1:
            age_h = (now - oldest_stamp).total_seconds() / 3600
            lines.append(
                f"Closest to the {config.threshold_hours:g}h threshold: "
                f"{oldest.device.label} at {_span(age_h)}."
            )
            lines.append("")

    # --- coverage -----------------------------------------------------------
    counts: dict[str, int] = {}
    for result in results:
        if result.skipped:
            counts[result.skipped.value] = counts.get(result.skipped.value, 0) + 1

    lines.append("COVERAGE")
    lines.append(f"  {len(devices)} devices visible, {len(monitored)} monitored")
    for reason, count in sorted(counts.items()):
        lines.append(f"  {count} skipped - {reason}")

    scope = (
        ", ".join(config.monitor_profiles)
        if config.monitor_profiles
        else "all servers"
    )
    lines.append(f"  scope: {scope}")
    if config.ignore_profiles:
        lines.append(f"  muted profiles: {', '.join(config.ignore_profiles)}")
    lines.append("")

    # Two lines, not one: with the longest day name and a timezone this would
    # otherwise pass the ~78 characters at which mail clients hard-wrap.
    lines.append(
        f"This report is sent every {report_config.day.capitalize()} at "
        f"{report_config.hour:02d}:00 {local.strftime('%Z')}, whether or not anything"
    )
    lines.append("is wrong. If it stops arriving, the check itself has stopped running.")
    return "\n".join(lines)
