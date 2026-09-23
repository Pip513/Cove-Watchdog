"""Settings tests: the parsing rules, and five files that must agree.

A setting lives in five places - the code, the setup script, the portal paste
file, the README reference and .env.example. They drifted once already: seven
settings went missing from two of them and nothing noticed. This fails the
moment they disagree, on names or on defaults.

Defaults are read straight from the code's calls into cove/env.py, which is
why those defaults must be literals.

Run:  python test_settings.py
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import sys
from pathlib import Path

from cove.env import env_bool, env_float, env_int, env_secret, env_str
from cove.errors import CoveConfigError

ROOT = Path(__file__).resolve().parent

#: Read only by local runs; deliberately absent from the Azure-facing files.
LOCAL_ONLY = {"WATCHDOG_STATE_PATH"}

HELPERS = {"env_str", "env_int", "env_float", "env_bool", "env_secret"}

#: A variable no real setting uses, so parsing tests cannot clobber anything.
PROBE = "COVE_WATCHDOG_TEST_PROBE"

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" - {detail}" if detail else ""))
        _failures.append(name)


class env_set:
    """Temporarily set (or with None, unset) environment variables."""

    def __init__(self, **values: str | None) -> None:
        self.values = values
        self.saved: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def __exit__(self, *exc) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def raises_config_error(fn) -> str | None:
    """The error message if fn raises CoveConfigError, else None."""
    try:
        fn()
    except CoveConfigError as exc:
        return str(exc)
    return None


# =========================================================================
print("Blank means default\n")

for label, value in (("unset", None), ("empty", ""), ("whitespace", "   ")):
    with env_set(**{PROBE: value}):
        check(f"env_str, {label} -> default", env_str(PROBE, "d") == "d")
        check(f"env_int, {label} -> default", env_int(PROBE, 8) == 8)
        check(f"env_float, {label} -> default", env_float(PROBE, 4.0) == 4.0)
        check(f"env_bool, {label} -> default true", env_bool(PROBE, True) is True)
        check(f"env_bool, {label} -> default false", env_bool(PROBE, False) is False)
        check(f"env_secret, {label} -> empty", env_secret(PROBE) == "")

print("\nValues that are set\n")

with env_set(**{PROBE: "  hello  "}):
    check("env_str strips surrounding space", env_str(PROBE, "d") == "hello")
with env_set(**{PROBE: "  pa ss  "}):
    check("env_secret is used exactly as entered", env_secret(PROBE) == "  pa ss  ")
with env_set(**{PROBE: " 12 "}):
    check("env_int parses, ignoring spaces", env_int(PROBE, 8) == 12)
with env_set(**{PROBE: "0.5"}):
    check("env_float accepts decimals", env_float(PROBE, 4.0) == 0.5)

for truthy in ("true", "TRUE", "Yes", "on", "1"):
    with env_set(**{PROBE: truthy}):
        check(f"env_bool {truthy!r} -> true", env_bool(PROBE, False) is True)
for falsy in ("false", "no", "0", "ture"):
    with env_set(**{PROBE: falsy}):
        check(f"env_bool {falsy!r} -> false", env_bool(PROBE, True) is False)

print("\nMalformed values fail loudly, naming the setting\n")

with env_set(**{PROBE: "eight"}):
    msg = raises_config_error(lambda: env_int(PROBE, 8))
    check("env_int 'eight' raises", msg is not None)
    check("  message names the setting", msg is not None and PROBE in msg, msg or "")
with env_set(**{PROBE: "four"}):
    check("env_float 'four' raises", raises_config_error(lambda: env_float(PROBE, 4.0)) is not None)

print("\nThe one exception: blank subject prefix means no prefix\n")

with env_set(**{PROBE: None}):
    check("unset -> default", env_str(PROBE, "[Cove]", blank_is_empty=True) == "[Cove]")
with env_set(**{PROBE: ""}):
    check("blank -> empty, not default", env_str(PROBE, "[Cove]", blank_is_empty=True) == "")

print("\nThe regressions this rule fixed\n")

from cove.notify import SmtpConfig  # noqa: E402
from cove.report import ReportConfig  # noqa: E402

with env_set(REPORT_ENABLED=""):
    check(
        "blank REPORT_ENABLED keeps the weekly report ON",
        ReportConfig.from_env().enabled is True,
        "it used to read as false and silently stop the heartbeat",
    )
with env_set(ALERT_SUBJECT_PREFIX=""):
    check("blank ALERT_SUBJECT_PREFIX -> no prefix", SmtpConfig.from_env().subject_prefix == "")

try:
    import azure.functions  # noqa: F401
    have_functions = True
except ImportError:
    have_functions = False

if have_functions:
    with env_set(WATCHDOG_SCHEDULE=""):
        import function_app

        importlib.reload(function_app)
        check(
            "blank WATCHDOG_SCHEDULE still gives the Function a schedule",
            function_app.SCHEDULE == "0 0 * * * *",
            f"got {function_app.SCHEDULE!r}; an empty schedule stops the Function loading",
        )
    importlib.reload(function_app)
else:
    print("  skip  WATCHDOG_SCHEDULE check - azure-functions not installed")


# =========================================================================
# Reading the five sources
# =========================================================================


def code_settings() -> tuple[dict[str, object], list[str]]:
    """Every setting the code reads, with its default, plus any problems."""
    found: dict[str, object] = {}
    problems: list[str] = []
    files = sorted((ROOT / "cove").glob("*.py")) + [
        p for p in sorted(ROOT.glob("*.py")) if not p.name.startswith("test_")
    ]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if func not in HELPERS:
                continue
            first = node.args[0] if node.args else None
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            setting = first.value

            if func == "env_secret":
                default: object = ""
            else:
                expr = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "default"), None
                )
                if expr is None:
                    default = ""
                else:
                    try:
                        default = ast.literal_eval(expr)
                    except ValueError:
                        problems.append(
                            f"{setting} in {path.name}: default is not a literal, so "
                            "it cannot be checked against the other files"
                        )
                        continue

            if setting in found and norm(found[setting]) != norm(default):
                problems.append(
                    f"{setting} is read twice with different defaults: "
                    f"{found[setting]!r} and {default!r}"
                )
            found[setting] = default
    return found, problems


def raw_reads() -> list[str]:
    """Places that read one of our settings without going through cove/env.py."""
    pattern = re.compile(
        r"""os\.(?:getenv\(|environ\.get\(|environ\[)\s*["']([A-Z][A-Z0-9_]*)["']"""
    )
    hits = []
    files = sorted((ROOT / "cove").glob("*.py")) + [
        p for p in sorted(ROOT.glob("*.py")) if not p.name.startswith("test_")
    ]
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in pattern.finditer(line):
                hits.append(f"{path.name}:{number} {match.group(1)}")
    return hits


def script_settings() -> tuple[dict[str, str], set[str]]:
    text = (ROOT / "scripts" / "setup-app-settings.ps1").read_text(encoding="utf-8")
    start = text.index("$settings = [ordered]@{")
    block = text[start : text.index("\n}", start)]
    settings = dict(re.findall(r'^\s*"([A-Z][A-Z0-9_]*)"\s*=\s*"([^"]*)"', block, re.M))
    required_block = re.search(r"\$required\s*=\s*@\((.*?)\)", text, re.S)
    required = set(re.findall(r'"([A-Z][A-Z0-9_]*)"', required_block.group(1)))
    return settings, required


def txt_settings() -> dict[str, str]:
    text = (ROOT / "azure-app-settings.txt").read_text(encoding="utf-8")
    marker = "-- COPY BELOW THIS LINE --"
    block = text.split(marker, 1)[1].split("\n", 1)[1]
    entries = json.loads("[" + block.strip().lstrip(",") + "]")
    return {e["name"]: e["value"] for e in entries}


def readme_settings() -> tuple[dict[str, str], set[str]]:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text[text.index("\n## Configuration\n") : text.index("\n## Development\n")]
    rows = re.findall(r"^\| `([A-Z][A-Z0-9_]*)` \| ([^|]*) \| ([^|]*) \|", section, re.M)
    defaults, required = {}, set()
    for name, req, cell in rows:
        cell = cell.strip()
        if cell in ("—", "-") or cell.startswith("*("):
            value = ""
        else:
            match = re.search(r"`([^`]*)`", cell)
            value = match.group(1) if match else cell
        defaults[name] = value
        if "Yes" in req:
            required.add(name)
    return defaults, required


def env_example_settings() -> dict[str, str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, re.M))


def norm(value: object) -> object:
    """Compare "4", 4 and 4.0 as equal, and True with "true"."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        return text.lower() if text.lower() in ("true", "false") else text


# =========================================================================
print("\nEvery setting is read through cove/env.py\n")

hits = raw_reads()
check("no setting read with os.getenv or os.environ directly", not hits, "; ".join(hits))

code, code_problems = code_settings()
for problem in code_problems:
    check("code defaults are checkable", False, problem)
if not code_problems:
    check("every code default is a literal, read consistently", True)

# =========================================================================
print("\nThe five sources name the same settings\n")

script, script_required = script_settings()
txt = txt_settings()
readme, readme_required = readme_settings()
env_example = env_example_settings()

everywhere = set(code)
azure = everywhere - LOCAL_ONLY


def same_names(label: str, got: set[str], want: set[str]) -> None:
    missing, extra = sorted(want - got), sorted(got - want)
    detail = "; ".join(filter(None, [
        f"missing {missing}" if missing else "",
        f"not read by the code {extra}" if extra else "",
    ]))
    check(f"{label} ({len(got)})", not missing and not extra, detail)


check(f"the code reads settings at all ({len(code)})", len(code) > 20)
same_names("setup script", set(script), azure)
same_names("azure-app-settings.txt", set(txt), azure)
same_names("README reference", set(readme), everywhere)
same_names(".env.example", set(env_example), everywhere)

# =========================================================================
print("\nThe five sources agree on every default\n")


def same_defaults(label: str, source: dict[str, str], names: set[str]) -> None:
    wrong = [
        f"{n}: code {code[n]!r}, {label} {source[n]!r}"
        for n in sorted(names)
        if n in source and norm(source[n]) != norm(code[n])
    ]
    check(f"{label} matches the code", not wrong, "; ".join(wrong))


same_defaults("setup script", script, azure)
same_defaults("azure-app-settings.txt", txt, azure)
same_defaults("README reference", readme, everywhere)
same_defaults(".env.example", env_example, everywhere)
check(
    "azure-app-settings.txt was regenerated from the script, not hand-edited",
    txt == script,
    "values differ - rerun the script with -OutputJson and replace the block",
)

# =========================================================================
print("\nRequired settings agree\n")

check(
    "README's required settings match the script's",
    readme_required == script_required,
    f"README {sorted(readme_required)} vs script {sorted(script_required)}",
)

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All settings tests pass.")
