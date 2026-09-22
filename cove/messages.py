"""Rendering alert and recovery emails.

Plain text, written for a technician triaging at 8am: what is broken, how long
it has been broken, and enough identifiers to find it in Cove. Aligned columns
so the failing source is obvious at a glance.

Kept free of SMTP so message content can be tested without a mail server.

Subject lines are stable per device and per kind, so a ticket system threading
on subject groups repeat alerts for the same server into one ticket rather than
opening a new one every morning.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .detection import Config, DatasourceFinding, DeviceResult


def _local(when: datetime | None, timezone: str) -> str:
    if when is None:
        return "never"
    try:
        tz = ZoneInfo(timezone)
    except Exception:  # never fail an alert over a timezone lookup
        return when.strftime("%Y-%m-%d %H:%M UTC")
    return when.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _span(hours: float) -> str:
    if hours < 48:
        return f"{hours:,.1f}h"
    return f"{hours / 24:,.1f}d"


#: Width of the timestamp column, e.g. "2026-09-18 19:20 EDT".
_WHEN_WIDTH = 20


def _source_lines(
    findings: list[DatasourceFinding],
    config: Config,
    *,
    with_status: bool,
    width: int,
) -> list[str]:
    """Aligned rows, one datasource each.

    Two lines per failing source rather than one: Outlook and most mobile
    clients hard-wrap plain text near 78 characters, and a single line carrying
    name, timestamp, age and status ran to 96 - wrapping precisely where the
    table matters most. Status moves to an indented continuation line, which
    keeps every line under 55 and survives any client.

    Width is passed in rather than derived per section so the FAILING and OK
    blocks line up as one table.
    """
    lines = []
    for finding in findings:
        source = finding.datasource
        if finding.age_hours is None:
            when, age = "never", ""
        else:
            when = _local(source.last_success, config.display_timezone)
            age = f"{_span(finding.age_hours)} ago"

        row = f"  {source.name:<{width}}   {when:<{_WHEN_WIDTH}}   {age:>9}"
        lines.append(row.rstrip())

        if with_status:
            note = source.session_status
            if source.in_flight:
                note += " - session running but not succeeding"
            elif finding.age_hours is None:
                note += " - no successful backup on record"
            lines.append(f"      {note}")
    return lines


def _footer(result: DeviceResult, config: Config, now: datetime) -> list[str]:
    d = result.device
    # Cove holds two names: I18 is the machine's hostname (what a tech
    # recognises, used in the header) and I1 is the Cove account name (what you
    # search the console by). Both are needed to act on this.
    cove_device = d.name or "(unknown)"
    if d.account_id is not None:
        cove_device += f"   (ID {d.account_id})"

    rows = [
        ("Profile", d.profile or "(none)"),
        ("Cove device", cove_device),
        ("Checked", _local(now, config.display_timezone)),
    ]
    width = max(len(label) for label, _ in rows)
    lines = [d.os_version or "unknown OS"]
    lines += [f"{label:<{width}}  {value}" for label, value in rows]
    return lines


def _header(result: DeviceResult) -> str:
    d = result.device
    return f"{d.label} - {d.customer}" if d.customer else d.label


def alert_subject(result: DeviceResult) -> str:
    d = result.device
    customer = f" ({d.customer})" if d.customer else ""
    return f"Missed backup: {d.label}{customer}"


def alert_body(
    result: DeviceResult,
    config: Config,
    now: datetime,
    *,
    repeat: bool = False,
) -> str:
    problems = result.problems
    healthy = [f for f in result.findings if not f.is_problem]

    lines = [_header(result), ""]

    verb = "still not backing up" if repeat else "not backing up"
    total = len(result.findings)
    noun = "data source" if total == 1 else "data sources"
    lines.append(
        f"{len(problems)} of {total} {noun} {verb} "
        f"(threshold {config.threshold_hours:g}h)."
    )
    lines.append("")

    # One width across both blocks so they read as a single table.
    width = max(len(f.datasource.name) for f in result.findings)

    lines.append("FAILING")
    lines += _source_lines(problems, config, with_status=True, width=width)

    if healthy:
        lines.append("")
        lines.append("OK")
        lines += _source_lines(healthy, config, with_status=False, width=width)

    lines.append("")
    lines += _footer(result, config, now)
    lines.append("")
    lines.append("Next alert 08:00 tomorrow unless resolved.")
    return "\n".join(lines)


def capped_summary_subject(count: int) -> str:
    return f"Missed backups on {count} devices"


def capped_summary_body(
    results: list[DeviceResult], config: Config, now: datetime, cap: int
) -> str:
    """Sent instead of individual alerts when too many fire at once.

    A site-wide outage - or a bug in here - should not put a hundred messages
    in an inbox or open a hundred tickets. One message naming every device is
    more useful than a hundred that are individually useless.
    """
    lines = [
        f"{len(results)} devices have missed backups, above the per-run limit "
        f"of {cap}.",
        "",
        "Individual alerts were NOT sent for these. Something affecting many",
        "devices at once is more likely one cause than many, so this is a",
        "single message rather than one per device.",
        "",
    ]

    width = max((len(r.device.label) for r in results), default=10)
    lines.append("AFFECTED")
    for result in results:
        sources = ", ".join(f.datasource.name for f in result.problems)
        customer = f"  ({result.device.customer})" if result.device.customer else ""
        lines.append(f"  {result.device.label:<{width}}   {sources}{customer}")

    lines += [
        "",
        f"Checked  {_local(now, config.display_timezone)}",
        "",
        "Check the Cove console, and whether this check itself is misbehaving,",
        "before treating these as unrelated failures.",
    ]
    return "\n".join(lines)


def recovery_subject(result: DeviceResult) -> str:
    d = result.device
    customer = f" ({d.customer})" if d.customer else ""
    return f"Resolved: {d.label}{customer}"


def recovery_body(
    result: DeviceResult,
    config: Config,
    now: datetime,
    *,
    down_since: datetime | None = None,
) -> str:
    lines = [_header(result), ""]

    if down_since is not None:
        down_for = (now - down_since).total_seconds() / 3600
        lines.append(
            f"Backing up normally again. Was failing for {_span(down_for)}, "
            f"since {_local(down_since, config.display_timezone)}."
        )
    else:
        lines.append("Backing up normally again.")
    lines.append("")

    lines.append("OK")
    width = max(len(f.datasource.name) for f in result.findings)
    lines += _source_lines(
        result.findings, config, with_status=False, width=width
    )

    lines.append("")
    lines += _footer(result, config, now)
    lines.append("")
    lines.append("No further alerts for this device.")
    return "\n".join(lines)
