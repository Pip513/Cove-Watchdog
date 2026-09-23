"""The watchdog itself - one complete run.

This is what a scheduler invokes. `function_app.py` will call the same
`execute()` so the hosted version and the local one cannot drift apart.

Defaults to a dry run: it prints exactly what it would send and writes no
state. `--send` is required to deliver anything, so an accidental invocation
cannot mail anyone.

Run:  python run_watchdog.py              # dry run
      python run_watchdog.py --send       # deliver, and record what was sent
      python run_watchdog.py --verbose    # include library logging
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from cove.deliver import deliver, plan_for_delivered, render
from cove.detection import Config
from cove.dispatch import commit, plan
from cove.errors import CoveConfigError
from cove.notify import SmtpConfig
from cove.report import ReportConfig
from cove.runner import run_check
from cove.state import StateStore, default_store

log = logging.getLogger("watchdog")


def execute(
    config: Config,
    report_config: ReportConfig,
    smtp_config: SmtpConfig,
    store: StateStore,
    *,
    send_enabled: bool,
    now: datetime | None = None,
) -> tuple[int, list[str]]:
    """Run once. Returns (exit code, human-readable lines describing the run).

    Never raises: a scheduled job that dies on an exception looks exactly like
    one that found nothing wrong.
    """
    now = now or datetime.now(tz=timezone.utc)
    lines: list[str] = []

    outcome = run_check(config, report_config, now=now)
    for warning in outcome.warnings:
        lines.append(f"[warn] {warning}")

    dispatch = plan(outcome, store, config, report_config)
    messages = render(dispatch, outcome, config, report_config)

    if not messages:
        lines.append("Nothing to send.")
        if not outcome.ok:
            # Suppressed by the daily cap rather than by there being no problem.
            lines.append(
                f"(the check is failing - {outcome.failure.kind.value} - but the "
                "failure email has already gone out today)"
            )
        return (1 if not outcome.ok else 0), lines

    if not send_enabled:
        lines.append(f"DRY RUN - {len(messages)} message(s) would be sent:\n")
        for message in messages:
            lines.append("=" * 68)
            lines.append(f"[{message.kind}] Subject: "
                         f"{smtp_config.subject_prefix} {message.subject}".strip())
            lines.append("-" * 68)
            lines.append(message.body)
            lines.append("")
        lines.append("Nothing was sent and no state was written. Use --send.")
        return (1 if not outcome.ok else 0), lines

    # Validate SMTP before sending, so a misconfiguration is reported as a
    # configuration problem rather than N identical delivery errors.
    try:
        smtp_config.validate()
    except CoveConfigError as exc:
        lines.append(f"[FAIL] SMTP configuration: {exc}")
        return 2, lines

    delivery = deliver(messages, smtp_config)

    for message in delivery.sent:
        lines.append(f"  sent     [{message.kind}] {message.subject}")
    for message, error in delivery.failed:
        lines.append(f"  FAILED   [{message.kind}] {message.subject} - {error}")

    # Record only what actually went out; the rest retries next run.
    commit(plan_for_delivered(dispatch, delivery), store, sent=True)

    if delivery.any_failed:
        lines.append(
            f"\n{len(delivery.failed)} message(s) could not be delivered and were "
            "not recorded as sent. They will be retried on the next run."
        )
        return 2, lines

    if not outcome.ok:
        return 1, lines
    return 0, lines


def main(argv: list[str]) -> int:
    send_enabled = "--send" in argv
    verbose = "--verbose" in argv

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()

    try:
        config = Config.from_env()
        report_config = ReportConfig.from_env()
        smtp_config = SmtpConfig.from_env()
    except (ValueError, CoveConfigError) as exc:
        print(f"[FAIL] Could not read configuration: {exc}")
        return 2

    now = datetime.now(tz=timezone.utc)
    print(f"Cove backup watchdog - {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  mode       {'SEND' if send_enabled else 'dry run'}")
    print(
        "  monitoring "
        + (", ".join(config.monitor_profiles) if config.monitor_profiles
           else "all servers")
    )
    print(
        "  muted      "
        + (", ".join(config.ignore_profiles) if config.ignore_profiles else "(none)")
    )

    store = default_store()
    print(f"  state      {getattr(store, 'path', 'in memory')}")
    print()

    code, lines = execute(
        config, report_config, smtp_config, store,
        send_enabled=send_enabled, now=now,
    )
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
