"""Fetching and modelling devices from EnumerateAccountStatistics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .client import CoveClient
from .datasources import (
    AGGREGATE_CODE,
    DATASOURCES,
    FIELD_LAST_SESSION,
    FIELD_LAST_STATUS,
    FIELD_LAST_SUCCESS,
    OS_SERVER,
    datasource_name,
    parse_active_datasources,
    status_name,
)
from .errors import CoveApiError, CoveDataError

PAGE_SIZE = 500

# Device-level columns. I78 tells us which datasources are actually active,
# which is what we evaluate against.
CONTEXT_COLUMNS: dict[str, str] = {
    "I0": "device_id",
    "I1": "name",
    "I18": "computer_name",
    "I8": "customer",
    "I32": "os_type",
    "I16": "os_version",
    "I78": "active_datasources",
    "I4": "created",
    "I54": "profile_id",
    "I56": "profile",
}

# Columns without which no judgement can be made. If any device is missing one
# of these, the response is not trustworthy and we must not report all-clear.
CRITICAL_COLUMNS = ("I1", "I32", "I78", "I4")


def _to_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _to_datetime(raw: Any) -> datetime | None:
    """Cove returns Unix epoch seconds as strings. 0 and empty mean 'never'."""
    ts = _to_int(raw)
    if ts is None or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@dataclass
class DatasourceStatus:
    """One active backup plugin on one device."""

    code: str
    last_success: datetime | None
    session_status: str
    in_flight: bool

    @property
    def name(self) -> str:
        return datasource_name(self.code)

    def age_hours(self, now: datetime) -> float | None:
        if self.last_success is None:
            return None
        return (now - self.last_success).total_seconds() / 3600.0


@dataclass
class Device:
    account_id: int | None
    partner_id: int | None
    name: str
    computer_name: str
    customer: str
    os_type: int | None
    os_version: str
    created: datetime | None
    profile: str
    profile_id: int | None
    active_codes: list[str] = field(default_factory=list)
    datasources: list[DatasourceStatus] = field(default_factory=list)

    @property
    def is_server(self) -> bool:
        return self.os_type == OS_SERVER

    @property
    def label(self) -> str:
        return self.computer_name or self.name or f"account {self.account_id}"

    def age_hours(self, now: datetime) -> float | None:
        """Hours since this device was created."""
        if self.created is None:
            return None
        return (now - self.created).total_seconds() / 3600.0


def _flatten(raw: dict) -> dict[str, str]:
    """Settings is a list of single-key objects; collapse it to a dict."""
    out: dict[str, str] = {}
    for entry in raw.get("Settings") or []:
        if isinstance(entry, dict):
            out.update(entry)
    return out


def build_columns() -> list[str]:
    """Every column we request.

    We cannot know a device's active datasources before fetching it, so we ask
    for every real datasource up front and evaluate only the ones each device's
    own I78 reports as active. The aggregate D09 is deliberately excluded: it
    reports the most recent success across sources, so a healthy plugin hides a
    dead one.
    """
    columns = list(CONTEXT_COLUMNS)
    for code in DATASOURCES:
        if code == AGGREGATE_CODE:
            continue
        columns.append(f"{code}{FIELD_LAST_SUCCESS}")
        columns.append(f"{code}{FIELD_LAST_STATUS}")
        columns.append(f"{code}{FIELD_LAST_SESSION}")
    return columns


def _build_device(raw: dict, settings: dict[str, str]) -> Device:
    active = [c for c in parse_active_datasources(settings.get("I78")) if c != AGGREGATE_CODE]

    datasources = []
    for code in active:
        last_session_raw = settings.get(f"{code}{FIELD_LAST_SESSION}")
        datasources.append(
            DatasourceStatus(
                code=code,
                last_success=_to_datetime(settings.get(f"{code}{FIELD_LAST_SUCCESS}")),
                session_status=status_name(settings.get(f"{code}{FIELD_LAST_STATUS}")),
                # F15 is blank exactly while a session is running on this source.
                in_flight=_to_datetime(last_session_raw) is None,
            )
        )

    return Device(
        account_id=raw.get("AccountId"),
        partner_id=raw.get("PartnerId"),
        name=settings.get("I1") or "",
        computer_name=settings.get("I18") or "",
        customer=settings.get("I8") or "",
        os_type=_to_int(settings.get("I32")),
        os_version=settings.get("I16") or "",
        created=_to_datetime(settings.get("I4")),
        profile=settings.get("I56") or "",
        profile_id=_to_int(settings.get("I54")),
        active_codes=active,
        datasources=datasources,
    )


def fetch_devices(client: CoveClient, partner_id: int | None = None) -> list[Device]:
    """Page through every device visible to this partner, including children.

    Raises CoveApiError if the response fails the integrity check - a malformed
    column code returns no error and no data, which would otherwise read as
    "nothing has ever backed up" and alert on the entire fleet.
    """
    partner_id = partner_id if partner_id is not None else client.partner_id
    if partner_id is None:
        raise CoveApiError("No partner id available; call login() first.")

    columns = build_columns()
    devices: list[Device] = []
    any_success_column = False
    start = 0

    while True:
        batch = client.call(
            "EnumerateAccountStatistics",
            {
                "query": {
                    "PartnerId": partner_id,
                    "StartRecordNumber": start,
                    "RecordsCount": PAGE_SIZE,
                    "SelectionMode": "Merged",
                    "Columns": columns,
                }
            },
        ) or []

        for raw in batch:
            settings = _flatten(raw)

            missing = [c for c in CRITICAL_COLUMNS if c not in settings]
            if missing:
                raise CoveDataError(
                    f"Device {raw.get('AccountId')} is missing required columns "
                    f"{', '.join(missing)}. Refusing to evaluate an incomplete "
                    "response - a bad column code returns no error and no data."
                )

            if any(k.endswith(FIELD_LAST_SUCCESS) for k in settings):
                any_success_column = True

            devices.append(_build_device(raw, settings))

        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE

    if devices and not any_success_column:
        raise CoveDataError(
            "No device returned any last-success column. This means the column "
            "codes are wrong, not that nothing has backed up. Refusing to report."
        )

    return devices
