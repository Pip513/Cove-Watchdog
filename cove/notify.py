"""SMTP delivery.

Deliberately generic: works with a hosted sender such as SMTP2GO, an internal
relay that accepts unauthenticated mail, or anything else that speaks SMTP.
Security mode, credentials and certificate verification are all configurable
because those are exactly the things that differ between providers.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from .errors import CoveConfigError, CoveError

log = logging.getLogger(__name__)


class EmailDeliveryError(CoveError):
    """The message could not be handed to the SMTP server."""


# Port 25 is blocked outbound on most Azure compute. 587 (STARTTLS) or 2525
# are the usual choices; SMTP2GO accepts both.
SECURITY_MODES = ("starttls", "ssl", "none")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _split_addresses(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = raw.replace(";", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    #: "starttls" (587, most common), "ssl" (465, implicit TLS), or "none"
    #: (an internal relay on 25/2525 that does not offer TLS).
    security: str = "starttls"
    #: Leave blank for a relay that accepts unauthenticated mail.
    username: str = ""
    password: str = field(default="", repr=False)
    #: Only disable for an internal relay with a self-signed certificate.
    verify_cert: bool = True
    timeout: int = 30

    from_address: str = ""
    from_name: str = "Cove Backup Watchdog"
    to_addresses: list[str] = field(default_factory=list)
    subject_prefix: str = "[Cove]"

    @classmethod
    def from_env(cls) -> "SmtpConfig":
        return cls(
            host=os.getenv("SMTP_HOST", "").strip(),
            port=int(os.getenv("SMTP_PORT", "587")),
            security=os.getenv("SMTP_SECURITY", "starttls").strip().lower(),
            username=os.getenv("SMTP_USERNAME", "").strip(),
            password=os.getenv("SMTP_PASSWORD", ""),
            verify_cert=_env_bool("SMTP_VERIFY_CERT", True),
            timeout=int(os.getenv("SMTP_TIMEOUT", "30")),
            from_address=os.getenv("ALERT_FROM", "").strip(),
            from_name=os.getenv("ALERT_FROM_NAME", "Cove Backup Watchdog").strip(),
            to_addresses=_split_addresses(os.getenv("ALERT_TO")),
            subject_prefix=os.getenv("ALERT_SUBJECT_PREFIX", "[Cove]").strip(),
        )

    def validate(self) -> None:
        problems: list[str] = []
        if not self.host:
            problems.append("SMTP_HOST is not set")
        if not self.from_address:
            problems.append("ALERT_FROM is not set")
        if not self.to_addresses:
            problems.append("ALERT_TO is not set")
        if self.security not in SECURITY_MODES:
            problems.append(
                f"SMTP_SECURITY must be one of {', '.join(SECURITY_MODES)}, "
                f"got {self.security!r}"
            )
        if self.username and not self.password:
            problems.append("SMTP_USERNAME is set but SMTP_PASSWORD is empty")
        if problems:
            raise CoveConfigError("; ".join(problems))

    @property
    def uses_auth(self) -> bool:
        return bool(self.username)


def build_message(
    config: SmtpConfig, subject: str, body: str, *, to: list[str] | None = None
) -> EmailMessage:
    """Build a plain-text message.

    Text only, by design: it renders identically everywhere, survives ticket
    system ingestion without markup artefacts, and keeps the alert scannable.
    """
    message = EmailMessage()
    prefix = f"{config.subject_prefix} " if config.subject_prefix else ""
    message["Subject"] = f"{prefix}{subject}"
    message["From"] = formataddr((config.from_name or None, config.from_address))
    message["To"] = ", ".join(to or config.to_addresses)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    return message


def send(config: SmtpConfig, message: EmailMessage) -> None:
    """Deliver one message, raising EmailDeliveryError on any failure."""
    config.validate()

    context: ssl.SSLContext | None = None
    if config.security in ("starttls", "ssl"):
        context = ssl.create_default_context()
        if not config.verify_cert:
            # Only for an internal relay with a self-signed certificate.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            log.warning(
                "SMTP certificate verification is disabled (SMTP_VERIFY_CERT)."
            )

    try:
        if config.security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                config.host, config.port, timeout=config.timeout, context=context
            )
        else:
            server = smtplib.SMTP(config.host, config.port, timeout=config.timeout)

        with server:
            server.ehlo()
            if config.security == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if config.uses_auth:
                server.login(config.username, config.password)
            server.send_message(message)

    except smtplib.SMTPAuthenticationError as exc:
        raise EmailDeliveryError(
            f"SMTP authentication rejected by {config.host}:{config.port}. "
            f"Check SMTP_USERNAME/SMTP_PASSWORD. ({exc})"
        ) from exc
    except smtplib.SMTPException as exc:
        raise EmailDeliveryError(
            f"SMTP error talking to {config.host}:{config.port}: {exc}"
        ) from exc
    except (OSError, ssl.SSLError) as exc:
        hint = ""
        if config.port == 25:
            hint = (
                " Port 25 is blocked outbound on most Azure compute - "
                "use 587 with STARTTLS, or 2525."
            )
        raise EmailDeliveryError(
            f"Could not connect to {config.host}:{config.port}: {exc}.{hint}"
        ) from exc
