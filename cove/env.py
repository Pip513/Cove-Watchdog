"""Reading settings from the environment.

Every setting is read through here, so one rule holds everywhere: **a setting
that is blank is treated exactly as if it were absent, and the default
applies.**

That matters because clearing a value is the natural way to "reset" it in the
Azure portal. Reading blank as an empty string broke that in ways ranging from
loud to silent: numbers failed to parse, a blank REPORT_ENABLED quietly turned
off the weekly report whose whole job is to catch silent failure, and a blank
WATCHDOG_SCHEDULE left the timer with no schedule, so the Function never loaded.

Values that are set but malformed still fail - loudly, naming the setting.

The one deliberate exception is marked at its call site: ALERT_SUBJECT_PREFIX,
where blank is a real choice meaning "no prefix".
"""

from __future__ import annotations

import os

from .errors import CoveConfigError

#: Accepted as true, case-insensitively. Anything else that is set reads false.
TRUE_VALUES = ("1", "true", "yes", "on")


def _raw(name: str) -> str | None:
    """The setting's value, or None when it is unset or blank."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value


def env_str(name: str, default: str = "", *, blank_is_empty: bool = False) -> str:
    """A text setting, with surrounding whitespace removed.

    `blank_is_empty` is for the rare setting where blank is itself a meaningful
    choice rather than "use the default". Unset still gives the default.
    """
    if blank_is_empty:
        value = os.getenv(name)
        return default if value is None else value.strip()
    value = _raw(name)
    return default if value is None else value.strip()


def env_secret(name: str) -> str:
    """A credential, used exactly as entered - never stripped. Blank is empty."""
    value = _raw(name)
    return "" if value is None else value


def env_int(name: str, default: int) -> int:
    """A whole number. Malformed values raise, naming the setting."""
    value = _raw(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        raise CoveConfigError(
            f"{name} must be a whole number, got {value.strip()!r}."
        ) from None


def env_float(name: str, default: float) -> float:
    """A number, decimals allowed. Malformed values raise, naming the setting."""
    value = _raw(name)
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError:
        raise CoveConfigError(
            f"{name} must be a number, got {value.strip()!r}."
        ) from None


def env_bool(name: str, default: bool) -> bool:
    """True for 1/true/yes/on in any case. Anything else that is set is false."""
    value = _raw(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES
