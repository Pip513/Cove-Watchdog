"""Phase 1 - verify Cove API authentication.

Read-only. Logs in, reports who we are and what role we hold, then proves the
visa chain works by making a second call with the visa from the first.

Run:  python check_auth.py
"""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

from cove import CoveClient, CoveCredentials, CoveError

# Set to logging.INFO to see the client's own progress messages.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def _ok(msg: str) -> None:
    print(f"  [ok]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [warn] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def main() -> int:
    load_dotenv()

    creds = CoveCredentials(
        partner=os.getenv("COVE_PARTNER", ""),
        username=os.getenv("COVE_USERNAME", ""),
        password=os.getenv("COVE_PASSWORD", ""),
        endpoint=os.getenv("COVE_ENDPOINT", "https://api.backup.management/jsonapi"),
    )

    print("Cove API authentication check")
    print(f"  endpoint: {creds.endpoint}")
    print(f"  partner:  {creds.partner or '(not set)'}")
    print(f"  username: {creds.username or '(not set)'}")
    print()

    try:
        client = CoveClient(creds)
    except CoveError as exc:
        _fail(str(exc))
        return 2

    # --- 1. Login -----------------------------------------------------------
    print("1. Login")
    try:
        user = client.login()
    except CoveError as exc:
        _fail(str(exc))
        payload = getattr(exc, "payload", None)
        if payload:
            print(f"\n  Raw response (record this - the error shape is undocumented):")
            print(f"  {payload}")
        print(
            "\n  Check: partner name matches the console exactly, COVE_USERNAME is\n"
            "  the API user's login name, and COVE_PASSWORD is the token issued at\n"
            "  creation (shown only once - if lost, delete the user and recreate)."
        )
        return 1

    _ok(f"authenticated as {user.get('Name')}")
    print(f"         user id:    {client.user_id}")
    print(f"         partner id: {client.partner_id}")
    print(f"         role id:    {client.role_id}")
    print(f"         full name:  {user.get('FullName') or '(none)'}")
    print(f"         title:      {user.get('Title') or '(none)'}")

    # --- 2. Account posture -------------------------------------------------
    print("\n2. Account posture")
    flags = user.get("Flags") or []
    print(f"         flags: {', '.join(flags) if flags else '(none)'}")

    # Console API users are marked NonInteractive and do not carry
    # AllowApiAuthentication; that flag belongs to regular users granted API
    # access. Either is a valid way in - NonInteractive is the stronger one.
    if "NonInteractive" in flags:
        _ok("NonInteractive - dedicated API user, cannot log into the console")
    elif "AllowApiAuthentication" in flags:
        _warn(
            "This is a regular console user with API access, not a dedicated API "
            "user. It can also log into the console. Prefer Management > Users > "
            "API Users for an unattended job."
        )
    else:
        _warn(
            "Neither NonInteractive nor AllowApiAuthentication is set, yet login "
            "succeeded. Worth confirming how this account is provisioned."
        )

    if "SecurityOfficer" in flags:
        _warn(
            "SecurityOfficer is set. This account can generate recovery "
            "passphrases, which a read-only monitor has no need for."
        )

    twofa = user.get("TwoFactorAuthenticationStatus")
    if twofa == "Enabled":
        _warn(
            "Two-factor authentication is Enabled on this account. It did not "
            "block this login, but an unattended job is safer on a dedicated "
            "API user."
        )
    else:
        _ok(f"two-factor status: {twofa or 'Undefined'}")

    # --- 3. Visa chain ------------------------------------------------------
    print("\n3. Visa chain (second call reusing the visa from login)")
    try:
        partner = client.call(
            "GetPartnerInfoById", {"partnerId": client.partner_id}
        )
    except CoveError as exc:
        _fail(f"second call failed: {exc}")
        print("  The visa did not carry over. Login works but the chain does not.")
        return 1

    if isinstance(partner, dict):
        _ok("visa chain works")
        print(f"         partner name:  {partner.get('Name')}")
        print(f"         partner level: {partner.get('Level')}")
        print(f"         partner state: {partner.get('State')}")
    else:
        _warn(f"unexpected GetPartnerInfoById payload: {type(partner).__name__}")

    print(f"         visa valid for another ~{client.visa_seconds_remaining / 60:.1f} min")

    # --- 4. Role name -------------------------------------------------------
    print("\n4. Role")
    try:
        roles = client.call("EnumerateUserRoles")
        by_id = {r.get("Id"): r.get("Name") for r in roles or [] if isinstance(r, dict)}
        role_name = by_id.get(client.role_id)
        if role_name:
            _ok(f"role id {client.role_id} = {role_name!r}")
            # Operator is the lowest system role that includes Profiles: View,
            # which the ignore-profile check needs. It also carries Remote
            # Actions and M365 datasource edit/delete, which this job does not.
            if role_name.strip().lower() == "operator":
                _warn(
                    "Operator also grants Remote Actions and M365 datasource "
                    "edit/delete, neither of which this job uses."
                )
        else:
            _warn(f"role id {client.role_id} not found in the roles list")

        print(f"         roles visible to this account: {len(by_id)}")
    except CoveError as exc:
        _warn(f"could not enumerate roles ({exc}). Not fatal - we only need device reads.")

    print("\nAuthentication verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
