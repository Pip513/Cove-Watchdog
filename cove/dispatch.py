"""Deciding what to send, and remembering that we sent it.

`run_check` establishes what is true now. This module compares that against
what was true last run and produces the messages that follow. Separating the
two keeps every cadence rule testable without a mail server or an API.

Cadence:

  - a device that starts failing is alerted on immediately, on whichever run
    catches it
  - a device that is still failing is alerted on once a day, at a fixed local
    hour, so the second message lands at a predictable time rather than
    whenever the outage happens to tick over
  - a device whose failure *spreads* to another data source is alerted on
    immediately, because the scope of the problem has changed
  - a device that recovers produces one all-clear, then nothing
  - a device that is muted or disappears is forgotten silently: it was not
    fixed, so claiming it recovered would be a lie

Nothing here sends anything. `plan()` decides; the caller sends; `commit()`
records. Keeping the record separate means a send failure does not mark a
message as delivered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .detection import Config, DeviceResult
from .health import CheckFailure, FailureKind, should_send_failure
from .report import ReportConfig, is_report_due
from .runner import CheckOutcome
from .state import (
    META_FAILURE_FIRST_AT,
    META_FAILURE_KIND,
    META_FAILURE_LAST_SENT,
    META_REPORT_LAST_SENT,
    DeviceState,
    StateStore,
)

log = logging.getLogger(__name__)


def is_daily_due(
    now: datetime, last_sent: datetime | None, hour: int, timezone_name: str
) -> bool:
    """Whether a once-a-day message is due, at a fixed local hour.

    Fires on the first run at or after `hour` on a later local day than the
    last send. A run missed entirely still sends later the same day rather than
    skipping a day.
    """
    if last_sent is None:
        return True
    tz = ZoneInfo(timezone_name)
    local_now = now.astimezone(tz)
    local_last = last_sent.astimezone(tz)
    if local_last.date() >= local_now.date():
        return False
    return local_now.hour >= hour


@dataclass
class AlertMessage:
    result: DeviceResult
    repeat: bool


@dataclass
class RecoveryMessage:
    result: DeviceResult
    down_since: datetime


@dataclass
class DispatchPlan:
    """What this run should send, and the state changes that follow."""

    now: datetime
    alerts: list[AlertMessage] = field(default_factory=list)
    recoveries: list[RecoveryMessage] = field(default_factory=list)
    send_report: bool = False
    failure: CheckFailure | None = None
    failure_recovered: str | None = None
    #: Devices whose state is dropped without a message (muted, or gone).
    forgotten: list[int] = field(default_factory=list)
    #: Set when the per-run email cap trips; the caller should send one
    #: summary instead of the individual alerts.
    capped: bool = False

    @property
    def message_count(self) -> int:
        return (
            len(self.alerts)
            + len(self.recoveries)
            + (1 if self.send_report else 0)
            + (1 if self.failure else 0)
            + (1 if self.failure_recovered else 0)
        )

    @property
    def is_empty(self) -> bool:
        return self.message_count == 0


def _failing_codes(result: DeviceResult) -> list[str]:
    return sorted(f.datasource.code for f in result.problems)


def plan(
    outcome: CheckOutcome,
    store: StateStore,
    config: Config,
    report_config: ReportConfig,
) -> DispatchPlan:
    """Decide what this run should send. Reads state; does not write it."""
    now = outcome.now
    result = DispatchPlan(now=now)

    # --- the check itself failed ----------------------------------------
    if outcome.failure is not None:
        last_sent = store.get_meta_time(META_FAILURE_LAST_SENT)
        if should_send_failure(now, last_sent):
            result.failure = outcome.failure
        # Device state is deliberately left untouched. We do not know the
        # fleet's state, so nothing about a device has been learned - not that
        # it is still failing, and not that it recovered.
        return result

    # --- the check recovered after failing -------------------------------
    previous_kind = store.get_meta(META_FAILURE_KIND)
    if previous_kind:
        result.failure_recovered = previous_kind

    # --- devices ---------------------------------------------------------
    seen: set[int] = set()

    for device_result in outcome.results:
        account_id = device_result.device.account_id
        if account_id is None:
            continue
        seen.add(account_id)
        previous = store.get_device(account_id)

        if device_result.skipped is not None:
            # Muted or out of scope. If it was failing, drop the record without
            # an all-clear: muting is not fixing, and claiming otherwise would
            # be worse than silence.
            if previous is not None:
                result.forgotten.append(account_id)
            continue

        if device_result.alerting:
            codes = _failing_codes(device_result)
            if previous is None:
                result.alerts.append(AlertMessage(device_result, repeat=False))
            elif set(codes) - set(previous.failing_sources):
                # The problem spread to a source that was healthy last time.
                # That is new information, so it does not wait for tomorrow.
                log.info(
                    "Device %s: failure spread to %s",
                    account_id,
                    sorted(set(codes) - set(previous.failing_sources)),
                )
                result.alerts.append(AlertMessage(device_result, repeat=True))
            elif is_daily_due(
                now, previous.last_alerted, config.realert_hour, config.display_timezone
            ):
                result.alerts.append(AlertMessage(device_result, repeat=True))
        elif previous is not None:
            result.recoveries.append(
                RecoveryMessage(device_result, down_since=previous.first_detected)
            )

    # A device deleted from Cove leaves a record behind. Drop it quietly -
    # there is nobody to alert about and nothing was fixed.
    for state in store.all_devices():
        if state.account_id not in seen:
            result.forgotten.append(state.account_id)

    # --- the weekly report ------------------------------------------------
    if is_report_due(
        now, config, report_config, store.get_meta_time(META_REPORT_LAST_SENT)
    ):
        result.send_report = True

    # --- blast radius -----------------------------------------------------
    # A site-wide outage, or a bug on our side, should not produce a hundred
    # emails. Past the cap the caller sends one summary instead.
    if len(result.alerts) > config.max_emails_per_run:
        log.warning(
            "%d alerts exceeds the cap of %d; collapsing to a summary",
            len(result.alerts),
            config.max_emails_per_run,
        )
        result.capped = True

    return result


def commit(plan: DispatchPlan, store: StateStore, *, sent: bool = True) -> None:
    """Record what was sent.

    Called after delivery so that a failed send is retried next run rather than
    being remembered as delivered. `sent=False` records nothing, which is what
    a dry run wants.
    """
    if not sent:
        return

    now = plan.now

    if plan.failure is not None:
        store.set_meta_time(META_FAILURE_LAST_SENT, now)
        store.set_meta(META_FAILURE_KIND, plan.failure.kind.value)
        if store.get_meta_time(META_FAILURE_FIRST_AT) is None:
            store.set_meta_time(META_FAILURE_FIRST_AT, now)
        store.commit()
        return

    if plan.failure_recovered is not None:
        store.set_meta(META_FAILURE_KIND, None)
        store.set_meta(META_FAILURE_LAST_SENT, None)
        store.set_meta(META_FAILURE_FIRST_AT, None)

    for message in plan.alerts:
        device = message.result.device
        account_id = device.account_id
        if account_id is None:
            continue
        previous = store.get_device(account_id)
        store.put_device(
            DeviceState(
                account_id=account_id,
                # Preserve the original detection time across repeats, so the
                # all-clear can say how long the outage really lasted.
                first_detected=previous.first_detected if previous else now,
                last_alerted=now,
                failing_sources=_failing_codes(message.result),
                label=device.label,
            )
        )

    for message in plan.recoveries:
        account_id = message.result.device.account_id
        if account_id is not None:
            store.delete_device(account_id)

    for account_id in plan.forgotten:
        store.delete_device(account_id)

    if plan.send_report:
        store.set_meta_time(META_REPORT_LAST_SENT, now)

    store.commit()
