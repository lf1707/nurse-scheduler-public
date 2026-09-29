"""Small email transport abstraction without adding a runtime dependency."""

from __future__ import annotations

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("app.email")


class EmailDeliveryError(RuntimeError):
    """Raised when a transactional email cannot be sent."""


@dataclass(frozen=True, slots=True)
class EmailMessageContent:
    to_email: str
    subject: str
    body: str


def _smtp_deliver(message: EmailMessage) -> None:
    if settings.SMTP_STARTTLS:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as client:
            client.starttls()
            if settings.SMTP_USERNAME:
                client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            client.send_message(message)
        return

    with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as client:
        if settings.SMTP_USERNAME:
            client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        client.send_message(message)


async def send_email(content: EmailMessageContent) -> None:
    """Send through configured SMTP, or log in non-production/log mode."""

    if settings.EMAIL_TRANSPORT == "log":
        logger.info(
            "email.delivery",
            to=content.to_email,
            subject=content.subject,
            body=content.body,
        )
        return

    message = EmailMessage()
    message["From"] = settings.SMTP_FROM
    message["To"] = content.to_email
    message["Subject"] = content.subject
    message.set_content(content.body)
    try:
        await asyncio.to_thread(_smtp_deliver, message)
    except Exception as exc:
        logger.warning(
            "email.delivery_failed",
            to=content.to_email,
            subject=content.subject,
            error=str(exc),
        )
        raise EmailDeliveryError("Email delivery failed") from exc
