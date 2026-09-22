"""Print every email variant to the terminal for review.

Sends nothing and needs no SMTP settings. Use this to check wording and layout
against the cases that matter before any of them fire for real.

Run:  python preview_emails.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from cove.detection import Config, evaluate_device
from cove.devices import DatasourceStatus, Device
from cove.messages import (
    alert_body,
    alert_subject,
    recovery_body,
    recovery_subject,
)


def _source(now, code, hours, status="Completed", in_flight=False):
    return DatasourceStatus(
        code=code,
        last_success=None if hours is None else now - timedelta(hours=hours),
        session_status=status,
        in_flight=in_flight,
    )


def _device(
    now,
    sources,
    *,
    name="EXAMPLE-SQL01",
    customer="Example Customer",
    created_days=400,
):
    return Device(
        account_id=100001,
        partner_id=200001,
        name=name.lower(),
        computer_name=name,
        customer=customer,
        os_type=2,
        os_version="Windows Server 2022 (20348), 64-bit",
        created=now - timedelta(days=created_days),
        profile="1 hour RPO Server",
        profile_id=300001,
        active_codes=[s.code for s in sources],
        datasources=sources,
    )


def scenarios(now, config):
    """The cases worth eyeballing before this goes live."""
    return [
        (
            "SQL dead, Files and System State healthy",
            "The failure Cove's own dashboard would show as fine.",
            evaluate_device(
                _device(
                    now,
                    [
                        _source(now, "D01", 0.4),
                        _source(now, "D02", 0.6),
                        _source(now, "D10", 73.2, "InProcess", True),
                    ],
                ),
                config,
                now,
            ),
            "alert",
            False,
            None,
        ),
        (
            "Every data source stale",
            "Typically a powered-off or disconnected server.",
            evaluate_device(
                _device(
                    now,
                    [_source(now, "D01", 388.0), _source(now, "D02", 388.0)],
                    name="EXAMPLE-FS02",
                ),
                config,
                now,
            ),
            "alert",
            False,
            None,
        ),
        (
            "Repeat alert, second day",
            "Wording shifts on the daily 08:00 re-send.",
            evaluate_device(
                _device(
                    now,
                    [
                        _source(now, "D01", 0.4),
                        _source(now, "D10", 97.0, "InProcess", True),
                    ],
                ),
                config,
                now,
            ),
            "alert",
            True,
            None,
        ),
        (
            "Never backed up",
            "Past the grace period with no successful backup on record.",
            evaluate_device(
                _device(
                    now,
                    [_source(now, "D01", None, "NotStarted")],
                    name="EXAMPLE-NEW01",
                    created_days=3,
                ),
                config,
                now,
            ),
            "alert",
            False,
            None,
        ),
        (
            "All clear",
            "Sent once when a device starts backing up again.",
            evaluate_device(
                _device(
                    now,
                    [
                        _source(now, "D01", 0.3),
                        _source(now, "D02", 0.3),
                        _source(now, "D10", 0.4),
                    ],
                ),
                config,
                now,
            ),
            "recovery",
            False,
            now - timedelta(days=3),
        ),
    ]


def main(argv: list[str]) -> int:
    load_dotenv()
    config = Config.from_env()
    try:
        config.validate()
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 2

    now = datetime.now(tz=timezone.utc)
    prefix = "[Cove] "

    for title, note, result, kind, repeat, down_since in scenarios(now, config):
        if kind == "alert":
            subject = alert_subject(result)
            body = alert_body(result, config, now, repeat=repeat)
        else:
            subject = recovery_subject(result)
            body = recovery_body(result, config, now, down_since=down_since)

        print("=" * 72)
        print(f"  {title}")
        print(f"  {note}")
        print("=" * 72)
        print(f"Subject: {prefix}{subject}")
        print("-" * 72)
        print(body)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
