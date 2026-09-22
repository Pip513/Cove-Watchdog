"""Verify SMTP settings and preview what an alert looks like.

Renders a realistic alert from a synthetic stale server - the Files-fine /
SQL-dead case - so you can check delivery and formatting without waiting for a
real failure, and without any risk of mailing about a device that is healthy.

Run:  python send_test_email.py            # render only, sends nothing
      python send_test_email.py --send     # actually deliver it
      python send_test_email.py --send --recovery
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from cove.detection import Config, evaluate_device
from cove.devices import DatasourceStatus, Device
from cove.messages import alert_body, alert_subject, recovery_body, recovery_subject
from cove.notify import EmailDeliveryError, SmtpConfig, build_message, send
from cove.errors import CoveConfigError


def sample_result(now: datetime, healthy: bool):
    """A server whose SQL backup is dead while Files and System State are fine."""

    def source(code: str, hours: float, status: str = "Completed", in_flight: bool = False):
        return DatasourceStatus(
            code=code,
            last_success=now - timedelta(hours=hours),
            session_status=status,
            in_flight=in_flight,
        )

    sources = (
        [source("D01", 0.4), source("D02", 0.4), source("D10", 0.5)]
        if healthy
        else [
            source("D01", 0.4),
            source("D02", 0.6),
            source("D10", 73.2, status="InProcess", in_flight=True),
        ]
    )

    device = Device(
        account_id=100001,
        partner_id=200001,
        name="example-sql01",
        computer_name="EXAMPLE-SQL01",
        customer="Example Customer",
        os_type=2,
        os_version="Windows Server 2022 (20348), 64-bit",
        created=now - timedelta(days=400),
        profile="1 hour RPO Server",
        profile_id=300001,
        active_codes=[s.code for s in sources],
        datasources=sources,
    )
    return evaluate_device(device, Config(), now)


def main(argv: list[str]) -> int:
    load_dotenv()
    do_send = "--send" in argv
    recovery = "--recovery" in argv

    detection_config = Config.from_env()
    try:
        detection_config.validate()
    except CoveConfigError as exc:
        print(f"[FAIL] {exc}")
        return 2

    now = datetime.now(tz=timezone.utc)
    result = sample_result(now, healthy=recovery)

    if recovery:
        subject = recovery_subject(result)
        body = recovery_body(
            result, detection_config, now, down_since=now - timedelta(days=3)
        )
    else:
        subject = alert_subject(result)
        body = alert_body(result, detection_config, now)

    config = SmtpConfig.from_env()

    print("SMTP configuration")
    print(f"  host       {config.host or '(not set)'}:{config.port}")
    print(f"  security   {config.security}")
    print(f"  auth       {'yes, as ' + config.username if config.uses_auth else 'none'}")
    print(f"  verify tls {config.verify_cert}")
    print(f"  from       {config.from_name} <{config.from_address or '(not set)'}>")
    print(f"  to         {', '.join(config.to_addresses) or '(not set)'}")
    print()

    prefix = f"{config.subject_prefix} " if config.subject_prefix else ""
    print("=" * 68)
    print(f"Subject: {prefix}{subject}")
    print("=" * 68)
    print(body)
    print("=" * 68)
    print()

    if not do_send:
        print("Nothing sent. Re-run with --send to deliver this message.")
        return 0

    try:
        config.validate()
    except CoveConfigError as exc:
        print(f"[FAIL] {exc}")
        return 2

    try:
        send(config, build_message(config, subject, body))
    except EmailDeliveryError as exc:
        print(f"[FAIL] {exc}")
        return 1

    print(f"Sent to {', '.join(config.to_addresses)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
