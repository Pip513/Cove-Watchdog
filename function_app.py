"""Azure Functions host.

A thin adapter. All the behaviour lives in `cove/` and in `run_watchdog.execute`,
which this calls directly, so the hosted version and a local run cannot drift
apart.

Schedule is UTC by design. Do **not** set WEBSITE_TIME_ZONE: the application
converts to local time itself, via WATCHDOG_TIMEZONE, to decide when the daily
re-alert and the weekly report fire. Two independent timezone interpretations
would disagree at daylight-saving boundaries and send at the wrong hour twice a
year.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import azure.functions as func

from cove.detection import Config
from cove.errors import CoveConfigError
from cove.notify import SmtpConfig
from cove.report import ReportConfig
from cove.state import default_store
from run_watchdog import execute

app = func.FunctionApp()

# Hourly, on the hour. Azure NCRONTAB has six fields, seconds first - so this
# is hourly, not "every minute" as the five-field cron equivalent would be.
SCHEDULE = os.getenv("WATCHDOG_SCHEDULE", "0 0 * * * *")


@app.timer_trigger(
    schedule=SCHEDULE,
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def backup_watchdog(timer: func.TimerRequest) -> None:
    """One check, hourly.

    Raising marks the execution as failed in Application Insights. The rule for
    when to raise is deliberate and narrow:

      - the check failed, but a failure email went out -> do NOT raise. The
        system worked: a human has been told. Raising here would make a Cove
        outage look like a defect in this job, and would fire an alert about a
        situation that has already been reported.
      - we could not notify anyone -> DO raise. This is the case nobody would
        otherwise discover, so it must show up as a failed execution.

    Configure an Application Insights alert on BOTH failed executions and on
    the absence of successful ones. If the Function stops being invoked at all,
    no code here can tell you, because none of it is running.
    """
    started = datetime.now(tz=timezone.utc)

    if timer.past_due:
        logging.warning("Timer is past due; a scheduled run was missed.")

    try:
        config = Config.from_env()
        report_config = ReportConfig.from_env()
        smtp_config = SmtpConfig.from_env()
    except (ValueError, CoveConfigError) as exc:
        logging.exception("Configuration could not be read")
        raise RuntimeError(
            f"Watchdog configuration is invalid, so nothing could be checked "
            f"or reported: {exc}"
        ) from exc

    store = default_store()

    code, lines = execute(
        config,
        report_config,
        smtp_config,
        store,
        send_enabled=True,
        now=started,
    )

    for line in lines:
        logging.info(line)

    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()

    if code == 2:
        # Could not deliver, or SMTP is misconfigured. Nobody has been told.
        logging.error("Watchdog could not notify anyone (%.1fs)", elapsed)
        raise RuntimeError(
            "The watchdog ran but could not send email. Alerts may have been "
            "produced and not delivered - check SMTP settings."
        )

    if code == 1:
        # The check failed and said so by email. Working as designed.
        logging.warning(
            "Watchdog reported a check failure by email (%.1fs)", elapsed
        )
        return

    logging.info("Watchdog completed (%.1fs)", elapsed)
