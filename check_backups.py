"""Phase 2 - missed-backup detection, dry run.

Read-only. Sends nothing. Prints exactly which alerts would fire and why, so
the rules can be verified against a known-good fleet before any email exists.

Run:  python check_backups.py
      python check_backups.py --all      # include healthy and skipped devices
      python check_backups.py --report   # also render the weekly report
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from cove import CoveClient, CoveCredentials, CoveError
from cove.detection import Config, SkipReason, Verdict, evaluate_all, verify_scope
from cove.devices import fetch_devices
from cove.report import ReportConfig, is_report_due, report_body, report_subject


def main(argv: list[str]) -> int:
    show_all = "--all" in argv
    show_report = "--report" in argv
    load_dotenv()

    config = Config.from_env()
    try:
        config.validate()
    except CoveError as exc:
        print(f"[FAIL] {exc}")
        return 2
    now = datetime.now(tz=timezone.utc)

    creds = CoveCredentials(
        partner=os.getenv("COVE_PARTNER", ""),
        username=os.getenv("COVE_USERNAME", ""),
        password=os.getenv("COVE_PASSWORD", ""),
        endpoint=os.getenv("COVE_ENDPOINT", "https://api.backup.management/jsonapi"),
    )

    print(f"Missed-backup check (dry run) - {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  threshold {config.threshold_hours:g}h | grace {config.grace_hours:g}h")
    print(
        "  monitoring: "
        + (", ".join(config.monitor_profiles) if config.monitor_profiles
           else "all servers (no monitor profile set)")
    )
    print(
        "  muted:      "
        + (", ".join(config.ignore_profiles) if config.ignore_profiles else "(none set)")
    )
    print()

    try:
        client = CoveClient(creds)
        client.login()
        devices = fetch_devices(client)
    except CoveError as exc:
        # An integrity failure lands here. Alerting nothing is correct: we do
        # not know the fleet's state, and a false all-clear is worse than noise.
        print(f"[FAIL] {exc}")
        print("\nNo alerts evaluated. Treat this as an outage of the check itself.")
        return 1

    try:
        warnings = verify_scope(devices, config)
    except CoveError as exc:
        print(f"[FAIL] {exc}")
        return 2
    for warning in warnings:
        print(f"[warn] {warning}\n")

    results = evaluate_all(devices, config, now)
    alerting = [r for r in results if r.alerting]
    monitored = [r for r in results if r.skipped is None]

    # Sort worst first: never-backed-up, then oldest.
    alerting.sort(key=lambda r: (r.worst_age_hours is not None, -(r.worst_age_hours or 0)))

    if alerting:
        print(f"WOULD ALERT on {len(alerting)} device(s):\n")
        for result in alerting:
            d = result.device
            print(f"  !! {d.label}  ({d.customer})")
            print(f"     {d.os_version or 'unknown OS'}")
            print(f"     profile: {d.profile or '(none)'}")
            for finding in result.problems:
                print(f"     - {finding.describe()}")
            healthy = [f for f in result.findings if not f.is_problem]
            if healthy:
                print(
                    "     still healthy: "
                    + ", ".join(f.datasource.name for f in healthy)
                )
            print()
    else:
        print(f"No alerts. All {len(monitored)} monitored device(s) backed up "
              f"within {config.threshold_hours:g}h on every active data "
              f"source.\n")

    if show_all:
        print("-" * 68)
        print("All devices:\n")
        for result in results:
            d = result.device
            marker = "!!" if result.alerting else "  "
            print(f"  {marker} {d.label:<24} {result.summary()}")
        print()

    # --- coverage accounting ------------------------------------------------
    print("-" * 68)
    skipped_counts: dict[str, int] = {}
    for result in results:
        if result.skipped:
            skipped_counts[result.skipped.value] = (
                skipped_counts.get(result.skipped.value, 0) + 1
            )

    print(f"Devices fetched:  {len(devices)}")
    print(f"Monitored:        {len(monitored)}")
    for reason, count in sorted(skipped_counts.items()):
        print(f"  skipped ({reason}): {count}")

    if not config.ignore_profiles:
        print(
            "\nNote: WATCHDOG_IGNORE_PROFILE is not set, so no device can be "
            "muted yet."
        )

    # Surface monitored devices that are not on an hourly profile - a server
    # nobody configured for hourly backups is its own kind of problem.
    odd = [
        r.device
        for r in monitored
        if "hour" not in (r.device.profile or "").lower()
    ]
    if odd:
        print("\nMonitored devices not on an hourly profile (worth a look):")
        for d in odd:
            print(f"  {d.label}: profile {d.profile or '(none)'}")

    # --- weekly heartbeat report -------------------------------------------
    report_config = ReportConfig.from_env()
    try:
        report_config.validate()
    except CoveError as exc:
        print(f"\n[FAIL] {exc}")
        return 2

    due = is_report_due(now, config, report_config, last_sent=None)
    if show_report or due:
        print("\n" + "=" * 68)
        heading = "WEEKLY REPORT (due now)" if due else "WEEKLY REPORT (preview)"
        print(f"  {heading}")
        print("=" * 68)
        print(f"Subject: [Cove] {report_subject(results, now, config)}")
        print("-" * 68)
        print(report_body(results, devices, now, config, report_config))
    else:
        local = now.astimezone(ZoneInfo(config.display_timezone))
        print(
            f"\nWeekly report: not due (it is {local.strftime('%A %H:%M')}, "
            f"scheduled {report_config.day.capitalize()} "
            f"{report_config.hour:02d}:00). Use --report to preview it."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
