"""One run of the check, with every failure path accounted for.

Shared by the dry run and (in the next phase) the Azure Function, so both
behave identically. The contract is deliberately narrow: run_check never
raises. It returns an outcome describing what happened, because a monitoring
job that dies on an unhandled exception is indistinguishable from one that
found nothing wrong.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .client import CoveClient, CoveCredentials
from .detection import Config, DeviceResult, evaluate_all, verify_scope
from .devices import Device, fetch_devices
from .env import env_secret, env_str
from .health import CheckFailure, classify
from .report import ReportConfig, is_report_due

log = logging.getLogger(__name__)


@dataclass
class CheckOutcome:
    """What a single run produced."""

    now: datetime
    devices: list[Device] = field(default_factory=list)
    results: list[DeviceResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failure: CheckFailure | None = None
    report_due: bool = False

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def alerting(self) -> list[DeviceResult]:
        """Devices to alert on - empty whenever the run failed.

        The emptiness is load-bearing: on failure we know nothing about the
        fleet, and staying silent about devices is the only honest option.
        """
        if self.failure is not None:
            return []
        return [r for r in self.results if r.alerting]

    @property
    def monitored(self) -> list[DeviceResult]:
        return [r for r in self.results if r.skipped is None]


def credentials_from_env() -> CoveCredentials:
    return CoveCredentials(
        partner=env_str("COVE_PARTNER"),
        username=env_str("COVE_USERNAME"),
        password=env_secret("COVE_PASSWORD"),
        endpoint=env_str("COVE_ENDPOINT", "https://api.backup.management/jsonapi"),
    )


def run_check(
    config: Config,
    report_config: ReportConfig,
    *,
    credentials: CoveCredentials | None = None,
    now: datetime | None = None,
    report_last_sent: datetime | None = None,
) -> CheckOutcome:
    """Run one check. Never raises; failures come back on the outcome."""
    now = now or datetime.now(tz=timezone.utc)
    outcome = CheckOutcome(now=now)

    # Config first: a stale profile name or bad timezone makes everything after
    # it meaningless, and costs an API call to discover otherwise.
    try:
        config.validate()
        report_config.validate()
    except Exception as exc:
        outcome.failure = classify(exc)
        log.error("Configuration invalid: %s", exc)
        return outcome

    try:
        client = CoveClient(credentials or credentials_from_env())
        client.login()
        outcome.devices = fetch_devices(client)
    except Exception as exc:
        outcome.failure = classify(exc)
        log.error("Could not retrieve devices: %s", exc)
        return outcome

    # Scope is checked against the fleet that actually came back, so a profile
    # renamed in the console is caught before it can produce a silent all-clear.
    try:
        outcome.warnings = verify_scope(outcome.devices, config)
    except Exception as exc:
        outcome.failure = classify(exc)
        log.error("Scope invalid: %s", exc)
        return outcome

    try:
        outcome.results = evaluate_all(outcome.devices, config, now)
    except Exception as exc:
        outcome.failure = classify(exc)
        log.exception("Evaluation failed")
        return outcome

    outcome.report_due = is_report_due(now, config, report_config, report_last_sent)
    return outcome
