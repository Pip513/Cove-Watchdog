"""Turning a dispatch plan into messages, and sending them.

Rendering is separated from sending so message content stays testable without
a mail server, and so a delivery failure can be attributed to a specific
message rather than to the run as a whole.

The ordering rule that matters: **only what actually sent is recorded**. A
message that fails to deliver is retried on the next run instead of being
remembered as sent, because a monitoring tool that silently drops an alert is
the failure mode this whole project exists to avoid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .detection import Config
from .dispatch import AlertMessage, DispatchPlan, RecoveryMessage
from .health import (
    FailureKind,
    failure_body,
    failure_subject,
    recovered_body,
    recovered_subject,
)
from .messages import (
    alert_body,
    alert_subject,
    capped_summary_body,
    capped_summary_subject,
    recovery_body,
    recovery_subject,
)
from .notify import EmailDeliveryError, SmtpConfig, build_message, send
from .report import ReportConfig, report_body, report_subject
from .runner import CheckOutcome

log = logging.getLogger(__name__)


@dataclass
class OutgoingMessage:
    kind: str  # alert | recovery | summary | report | failure | failure_recovered
    subject: str
    body: str
    #: The plan item this came from, so a partial delivery can be recorded
    #: accurately. None for messages not tied to one device.
    source: Any = None


@dataclass
class DeliveryResult:
    sent: list[OutgoingMessage] = field(default_factory=list)
    failed: list[tuple[OutgoingMessage, str]] = field(default_factory=list)

    @property
    def any_failed(self) -> bool:
        return bool(self.failed)


def render(
    plan: DispatchPlan,
    outcome: CheckOutcome,
    config: Config,
    report_config: ReportConfig,
) -> list[OutgoingMessage]:
    """Build every message this plan calls for, in the order they should send."""
    messages: list[OutgoingMessage] = []
    now = plan.now
    tz = config.display_timezone

    # Failures first: if the check is broken, that is the headline.
    if plan.failure is not None:
        messages.append(
            OutgoingMessage(
                "failure",
                failure_subject(plan.failure),
                failure_body(plan.failure, now, tz),
            )
        )
        # A failed run produces nothing else - it knows nothing about devices.
        return messages

    if plan.failure_recovered is not None:
        try:
            kind = FailureKind(plan.failure_recovered)
        except ValueError:
            kind = FailureKind.UNEXPECTED
        messages.append(
            OutgoingMessage(
                "failure_recovered",
                recovered_subject(),
                recovered_body(kind, now, tz),
            )
        )

    if plan.capped:
        results = [message.result for message in plan.alerts]
        messages.append(
            OutgoingMessage(
                "summary",
                capped_summary_subject(len(results)),
                capped_summary_body(results, config, now, config.max_emails_per_run),
                source=plan.alerts,
            )
        )
    else:
        for alert in plan.alerts:
            messages.append(
                OutgoingMessage(
                    "alert",
                    alert_subject(alert.result),
                    alert_body(alert.result, config, now, repeat=alert.repeat),
                    source=alert,
                )
            )

    for recovery in plan.recoveries:
        messages.append(
            OutgoingMessage(
                "recovery",
                recovery_subject(recovery.result),
                recovery_body(
                    recovery.result, config, now, down_since=recovery.down_since
                ),
                source=recovery,
            )
        )

    if plan.send_report:
        messages.append(
            OutgoingMessage(
                "report",
                report_subject(outcome.results, now, config),
                report_body(
                    outcome.results, outcome.devices, now, config, report_config
                ),
            )
        )

    return messages


def deliver(
    messages: list[OutgoingMessage], smtp_config: SmtpConfig
) -> DeliveryResult:
    """Send each message, continuing past individual failures.

    One bad recipient must not stop the rest: a failure email is worth more
    than the alert that preceded it, and vice versa.
    """
    result = DeliveryResult()
    for message in messages:
        try:
            send(smtp_config, build_message(smtp_config, message.subject, message.body))
        except EmailDeliveryError as exc:
            log.error("Could not send %s (%s): %s", message.kind, message.subject, exc)
            result.failed.append((message, str(exc)))
        else:
            log.info("Sent %s: %s", message.kind, message.subject)
            result.sent.append(message)
    return result


def plan_for_delivered(plan: DispatchPlan, delivery: DeliveryResult) -> DispatchPlan:
    """A copy of the plan reduced to what actually sent.

    Committing this instead of the original means an undelivered alert is
    retried next run rather than recorded as sent. State that is only advanced
    by a successful send can never quietly swallow a message.
    """
    if not delivery.any_failed:
        return plan

    kinds_sent = {m.kind for m in delivery.sent}
    alerts_sent: list[AlertMessage] = []
    recoveries_sent: list[RecoveryMessage] = []

    for message in delivery.sent:
        if message.kind == "alert" and isinstance(message.source, AlertMessage):
            alerts_sent.append(message.source)
        elif message.kind == "recovery" and isinstance(message.source, RecoveryMessage):
            recoveries_sent.append(message.source)
        elif message.kind == "summary" and isinstance(message.source, list):
            # The summary stands in for all of them, so all are recorded.
            alerts_sent.extend(message.source)

    return DispatchPlan(
        now=plan.now,
        alerts=alerts_sent,
        recoveries=recoveries_sent,
        send_report="report" in kinds_sent,
        failure=plan.failure if "failure" in kinds_sent else None,
        failure_recovered=(
            plan.failure_recovered if "failure_recovered" in kinds_sent else None
        ),
        # Forgetting a device needs no message, so it is never blocked by one.
        forgotten=plan.forgotten,
        capped=plan.capped,
    )
