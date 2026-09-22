"""Cadence tests.

These decide whether someone gets one email or twenty-four, and whether a
recovered device is ever mentioned again. Every rule is exercised by simulating
consecutive runs against a real state store rather than by asserting on a
single call.

Run:  python test_dispatch.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cove.detection import Config, evaluate_all
from cove.devices import DatasourceStatus, Device
from cove.dispatch import commit, is_daily_due, plan
from cove.health import CheckFailure, FailureKind
from cove.report import ReportConfig
from cove.runner import CheckOutcome
from cove.state import (
    META_FAILURE_LAST_SENT,
    META_REPORT_LAST_SENT,
    InMemoryStateStore,
    JsonFileStateStore,
)

EASTERN = ZoneInfo("America/New_York")
CONFIG = Config(threshold_hours=4.0, realert_hour=8)
# Reports off by default so device cadence can be tested in isolation.
NO_REPORT = ReportConfig(enabled=False)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" - {detail}" if detail else ""))
        _failures.append(name)


def at(local: str) -> datetime:
    """UTC instant from an Eastern wall-clock time."""
    return datetime.strptime(local, "%Y-%m-%d %H:%M").replace(
        tzinfo=EASTERN
    ).astimezone(timezone.utc)


def device(
    now: datetime,
    *,
    ages: dict[str, float | None],
    account_id: int = 1,
    profile: str = "1 hour RPO Server",
) -> Device:
    sources = [
        DatasourceStatus(
            code=code,
            last_success=None if hours is None else now - timedelta(hours=hours),
            session_status="Completed",
            in_flight=False,
        )
        for code, hours in ages.items()
    ]
    return Device(
        account_id=account_id, partner_id=1, name=f"dev{account_id}",
        computer_name=f"SERVER{account_id}", customer="Example Customer",
        os_type=2, os_version="Windows Server 2022",
        created=now - timedelta(days=100), profile=profile, profile_id=1,
        active_codes=list(ages), datasources=sources,
    )


def run(now: datetime, devices: list[Device], store, *, failure=None,
        config: Config = CONFIG, report: ReportConfig = NO_REPORT):
    """Simulate one run end to end: evaluate, plan, commit."""
    outcome = CheckOutcome(now=now, devices=devices)
    outcome.results = evaluate_all(devices, config, now)
    outcome.failure = failure
    p = plan(outcome, store, config, report)
    commit(p, store)
    return p


print("Daily-hour scheduling\n")

check("never sent -> due", is_daily_due(at("2026-09-22 03:00"), None, 8, "America/New_York"))
check(
    "sent yesterday, now before the hour -> not due",
    not is_daily_due(at("2026-09-22 06:00"), at("2026-09-21 14:00"), 8, "America/New_York"),
)
check(
    "sent yesterday, now after the hour -> due",
    is_daily_due(at("2026-09-22 08:30"), at("2026-09-21 14:00"), 8, "America/New_York"),
)
check(
    "already sent today -> not due",
    not is_daily_due(at("2026-09-22 20:00"), at("2026-09-22 08:00"), 8, "America/New_York"),
)
check(
    "missed the 08:00 run -> still sends later that day",
    is_daily_due(at("2026-09-22 15:00"), at("2026-09-20 08:00"), 8, "America/New_York"),
)

print("\nDevice alert cadence over consecutive runs\n")

store = InMemoryStateStore()
broken = {"D01": 0.5, "D10": 50.0}  # SQL dead, files fine

p = run(at("2026-09-21 17:00"), [device(at("2026-09-21 17:00"), ages=broken)], store)
check("first detection at 17:00 -> alerts immediately", len(p.alerts) == 1)
check("  flagged as a first alert, not a repeat", not p.alerts[0].repeat)

p = run(at("2026-09-21 18:00"), [device(at("2026-09-21 18:00"), ages=broken)], store)
check("18:00 same evening -> silent", len(p.alerts) == 0)

p = run(at("2026-09-22 06:00"), [device(at("2026-09-22 06:00"), ages=broken)], store)
check("06:00 next morning, before the hour -> still silent", len(p.alerts) == 0)

p = run(at("2026-09-22 08:00"), [device(at("2026-09-22 08:00"), ages=broken)], store)
check("08:00 next morning -> repeat alert", len(p.alerts) == 1)
check("  flagged as a repeat", p.alerts[0].repeat)

p = run(at("2026-09-22 12:00"), [device(at("2026-09-22 12:00"), ages=broken)], store)
check("midday after the repeat -> silent", len(p.alerts) == 0)

# Count everything sent across a full week of hourly runs.
store = InMemoryStateStore()
sent = 0
start = at("2026-09-21 17:00")
for hour in range(24 * 7):
    now = start + timedelta(hours=hour)
    p = run(now, [device(now, ages=broken)], store)
    sent += len(p.alerts)
check(
    "a week of hourly runs on one broken device -> 8 emails, not 168",
    sent == 8,
    f"sent {sent}",
)

print("\nRecovery\n")

store = InMemoryStateStore()
run(at("2026-09-21 17:00"), [device(at("2026-09-21 17:00"), ages=broken)], store)
now = at("2026-09-21 19:00")
p = run(now, [device(now, ages={"D01": 0.5, "D10": 0.5})], store)
check("device recovers -> one all-clear", len(p.recoveries) == 1)
check(
    "  all-clear knows when the outage started",
    p.recoveries[0].down_since == at("2026-09-21 17:00"),
    str(p.recoveries[0].down_since),
)
now = at("2026-09-21 20:00")
p = run(now, [device(now, ages={"D01": 0.5, "D10": 0.5})], store)
check("still healthy an hour later -> nothing", p.is_empty)
check("  state was cleared", store.all_devices() == [])

print("\nA failure that spreads\n")

store = InMemoryStateStore()
run(at("2026-09-21 17:00"), [device(at("2026-09-21 17:00"), ages={"D01": 0.5, "D10": 50.0})], store)
now = at("2026-09-21 18:00")
p = run(now, [device(now, ages={"D01": 50.0, "D10": 50.0})], store)
check(
    "a second data source fails -> alerts at once, not tomorrow",
    len(p.alerts) == 1,
    "the scope of the problem changed, so it is new information",
)
now = at("2026-09-21 19:00")
p = run(now, [device(now, ages={"D01": 50.0, "D10": 50.0})], store)
check("  but does not repeat while the set is unchanged", len(p.alerts) == 0)

# Shrinking is not news: still broken, still one email a day.
now = at("2026-09-21 20:00")
p = run(now, [device(now, ages={"D01": 0.5, "D10": 50.0})], store)
check("a source recovering while others fail -> silent", len(p.alerts) == 0)

print("\nMuted and deleted devices\n")

store = InMemoryStateStore()
run(at("2026-09-21 17:00"), [device(at("2026-09-21 17:00"), ages=broken)], store)
muted_config = Config(threshold_hours=4.0, ignore_profiles=["1 hour RPO Server"])
now = at("2026-09-21 18:00")
p = run(now, [device(now, ages=broken)], store, config=muted_config)
check("a failing device that gets muted -> no all-clear", len(p.recoveries) == 0)
check("  its state is dropped", store.all_devices() == [])

store = InMemoryStateStore()
run(at("2026-09-21 17:00"), [device(at("2026-09-21 17:00"), ages=broken)], store)
p = run(at("2026-09-21 18:00"), [], store)
check("a device deleted from Cove -> no all-clear", len(p.recoveries) == 0)
check("  its state is dropped", store.all_devices() == [])

print("\nCheck failures\n")

store = InMemoryStateStore()
auth = CheckFailure(FailureKind.AUTHENTICATION, "token rejected")
now = at("2026-09-21 17:00")
p = run(now, [device(now, ages=broken)], store, failure=auth)
check("a failed run emails the failure", p.failure is not None)
check("  and sends no device alerts", len(p.alerts) == 0)
check("  and no all-clears", len(p.recoveries) == 0)

now = at("2026-09-21 18:00")
p = run(now, [device(now, ages=broken)], store, failure=auth)
check("still failing an hour later -> no second email", p.failure is None)

now = at("2026-09-22 18:00")
p = run(now, [device(now, ages=broken)], store, failure=auth)
check("still failing a day later -> emails again", p.failure is not None)

now = at("2026-09-22 19:00")
p = run(now, [device(now, ages=broken)], store)
check("the check recovers -> says so", p.failure_recovered == "authentication")
check("  and the backlogged device alert goes out", len(p.alerts) == 1)
now = at("2026-09-22 20:00")
p = run(now, [device(now, ages=broken)], store)
check("  recovery is announced only once", p.failure_recovered is None)

print("\nState is not corrupted by a failed run\n")

store = InMemoryStateStore()
now = at("2026-09-21 17:00")
run(now, [device(now, ages=broken)], store)
before = store.get_device(1)
now = at("2026-09-22 09:00")
run(now, [], store, failure=auth)  # a failed run returns no devices at all
after = store.get_device(1)
check(
    "a failing run does not forget a device it could not see",
    after is not None and after.last_alerted == before.last_alerted,
    "otherwise an API outage would re-alert the whole fleet on recovery",
)

print("\nBlast radius\n")

store = InMemoryStateStore()
now = at("2026-09-21 17:00")
many = [device(now, ages=broken, account_id=i) for i in range(1, 31)]
p = run(now, many, store, config=Config(threshold_hours=4.0, max_emails_per_run=25))
check("30 alerts against a cap of 25 -> capped", p.capped)
p = run(at("2026-09-21 17:00"), many[:5], store := InMemoryStateStore(),
        config=Config(threshold_hours=4.0, max_emails_per_run=25))
check("5 alerts against the same cap -> not capped", not p.capped)

print("\nWeekly report cadence\n")

store = InMemoryStateStore()
weekly = ReportConfig(enabled=True, day="monday", hour=8)
now = at("2026-09-21 08:00")  # a Monday
p = run(now, [device(now, ages={"D01": 0.5})], store, report=weekly)
check("Monday 08:00 -> report sent", p.send_report)
now = at("2026-09-21 09:00")
p = run(now, [device(now, ages={"D01": 0.5})], store, report=weekly)
check("an hour later -> not sent again", not p.send_report)
now = at("2026-09-28 08:00")
p = run(now, [device(now, ages={"D01": 0.5})], store, report=weekly)
check("the following Monday -> sent again", p.send_report)

print("\nNothing is recorded until it is sent\n")

store = InMemoryStateStore()
now = at("2026-09-21 17:00")
outcome = CheckOutcome(now=now, devices=[device(now, ages=broken)])
outcome.results = evaluate_all(outcome.devices, CONFIG, now)
p = plan(outcome, store, CONFIG, NO_REPORT)
commit(p, store, sent=False)
check("a send that fails leaves no state behind", store.all_devices() == [])
commit(p, store, sent=True)
check("  and is recorded once it succeeds", len(store.all_devices()) == 1)

print("\nJSON file store\n")

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "state.json"
    store = JsonFileStateStore(path)
    now = at("2026-09-21 17:00")
    run(now, [device(now, ages=broken)], store)

    reloaded = JsonFileStateStore(path)
    state = reloaded.get_device(1)
    check("state survives a restart", state is not None)
    check(
        "  timestamps round-trip as UTC",
        state is not None and state.last_alerted == at("2026-09-21 17:00"),
        str(state.last_alerted if state else None),
    )
    check(
        "  failing sources round-trip",
        state is not None and state.failing_sources == ["D10"],
        str(state.failing_sources if state else None),
    )

    now = at("2026-09-21 18:00")
    p = run(now, [device(now, ages=broken)], reloaded)
    check("  cadence honours state loaded from disk", len(p.alerts) == 0)

    bad = Path(tmp) / "corrupt.json"
    bad.write_text("{not json at all", encoding="utf-8")
    check("a corrupt state file starts empty rather than crashing",
          JsonFileStateStore(bad).all_devices() == [])

print("\nRendering\n")

import cove.deliver as deliver_module  # noqa: E402
from cove.deliver import DeliveryResult, deliver, plan_for_delivered, render  # noqa: E402
from cove.notify import EmailDeliveryError, SmtpConfig  # noqa: E402

SMTP = SmtpConfig(host="h", from_address="a@b.c", to_addresses=["d@e.f"])


def planned(now, devices, store, *, failure=None, config=CONFIG, report=NO_REPORT):
    """Plan without committing, plus the outcome needed to render it."""
    outcome = CheckOutcome(now=now, devices=devices)
    outcome.results = evaluate_all(devices, config, now)
    outcome.failure = failure
    return plan(outcome, store, config, report), outcome


now = at("2026-09-21 17:00")
p, outcome = planned(now, [device(now, ages=broken)], InMemoryStateStore())
messages = render(p, outcome, CONFIG, NO_REPORT)
check("one failing device renders one alert", len(messages) == 1)
check("  tagged as an alert", messages[0].kind == "alert")

p, outcome = planned(
    now, [device(now, ages=broken)], InMemoryStateStore(),
    failure=CheckFailure(FailureKind.AUTHENTICATION, "token rejected"),
)
messages = render(p, outcome, CONFIG, NO_REPORT)
check("a failed run renders only the failure", [m.kind for m in messages] == ["failure"])

store = InMemoryStateStore()
many = [device(now, ages=broken, account_id=i) for i in range(1, 31)]
capped_config = Config(threshold_hours=4.0, max_emails_per_run=25)
p, outcome = planned(now, many, store, config=capped_config)
messages = render(p, outcome, capped_config, NO_REPORT)
check("30 alerts render as one summary", [m.kind for m in messages] == ["summary"])
check("  summary names every device", messages[0].body.count("SERVER") >= 30)

print("\nPartial delivery is not recorded as sent\n")


class FakeSend:
    """Stands in for SMTP; fails for subjects containing `fail_on`."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.sent: list[str] = []

    def __call__(self, config, message) -> None:
        subject = message["Subject"]
        if self.fail_on and self.fail_on in subject:
            raise EmailDeliveryError(f"refused: {subject}")
        self.sent.append(subject)


_real_send = deliver_module.send
try:
    store = InMemoryStateStore()
    now = at("2026-09-21 17:00")
    devices = [device(now, ages=broken, account_id=1), device(now, ages=broken, account_id=2)]
    p, outcome = planned(now, devices, store)
    messages = render(p, outcome, CONFIG, NO_REPORT)

    deliver_module.send = FakeSend(fail_on="SERVER2")
    result = deliver(messages, SMTP)
    check("one of two alerts fails to send", len(result.sent) == 1 and len(result.failed) == 1)

    commit(plan_for_delivered(p, result), store)
    check(
        "  only the delivered device is recorded",
        store.get_device(1) is not None and store.get_device(2) is None,
        f"device1={store.get_device(1) is not None} device2={store.get_device(2) is not None}",
    )

    # Next run: the undelivered one must alert again, the delivered one must not.
    deliver_module.send = FakeSend()
    now = at("2026-09-21 18:00")
    devices = [device(now, ages=broken, account_id=1), device(now, ages=broken, account_id=2)]
    p, outcome = planned(now, devices, store)
    check(
        "  the undelivered alert is retried next run",
        [m.result.device.account_id for m in p.alerts] == [2],
        str([m.result.device.account_id for m in p.alerts]),
    )

    # A failed report must not mark the week as reported.
    store = InMemoryStateStore()
    weekly = ReportConfig(enabled=True, day="monday", hour=8)
    now = at("2026-09-21 08:00")
    p, outcome = planned(now, [device(now, ages={"D01": 0.5})], store, report=weekly)
    messages = render(p, outcome, CONFIG, weekly)
    deliver_module.send = FakeSend(fail_on="Weekly")
    result = deliver(messages, SMTP)
    commit(plan_for_delivered(p, result), store)
    check(
        "a failed weekly report is not recorded as sent",
        store.get_meta(META_REPORT_LAST_SENT) is None,
    )
    now = at("2026-09-21 09:00")
    p, _ = planned(now, [device(now, ages={"D01": 0.5})], store, report=weekly)
    check("  so it retries on the next run", p.send_report)

    # The capped summary stands in for every alert it covers.
    store = InMemoryStateStore()
    now = at("2026-09-21 17:00")
    p, outcome = planned(now, many, store, config=capped_config)
    messages = render(p, outcome, capped_config, NO_REPORT)
    deliver_module.send = FakeSend()
    result = deliver(messages, SMTP)
    commit(plan_for_delivered(p, result), store)
    check(
        "a delivered summary records all 30 devices",
        len(store.all_devices()) == 30,
        f"{len(store.all_devices())} recorded",
    )

    # One bad recipient must not stop the rest.
    store = InMemoryStateStore()
    now = at("2026-09-21 17:00")
    devices = [device(now, ages=broken, account_id=i) for i in (1, 2, 3)]
    p, outcome = planned(now, devices, store)
    messages = render(p, outcome, CONFIG, NO_REPORT)
    fake = FakeSend(fail_on="SERVER2")
    deliver_module.send = fake
    result = deliver(messages, SMTP)
    check(
        "a failure mid-list does not stop later messages",
        len(fake.sent) == 2 and len(result.failed) == 1,
        f"sent {len(fake.sent)}, failed {len(result.failed)}",
    )
finally:
    deliver_module.send = _real_send

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All dispatch tests pass.")
