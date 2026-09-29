"""Redis-backed login failure rate limiting."""

from __future__ import annotations

import hashlib
import ipaddress
import time

import redis.asyncio as aioredis
from redis.exceptions import RedisError
from starlette.requests import Request

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("app.security.rate_limit")


class RateLimitUnavailableError(Exception):
    """Raised when the login rate-limit store cannot be reached."""


def client_ip(request: Request) -> str:
    """Return the direct client address without trusting client-supplied chains."""
    if settings.TRUST_PROXY_FOR_CLIENT_IP:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # The supported nginx deployment replaces this header with the
            # direct peer address. Rightmost is the value appended by the
            # nearest trusted proxy in a longer trusted chain.
            candidate = forwarded_for.split(",")[-1].strip()
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass
    return request.client.host if request.client else "unknown"


def email_fingerprint(email: str) -> str:
    """Avoid putting login emails in logs or Redis keys."""
    return hashlib.sha256(email.lower().encode()).hexdigest()[:16]


def _keys(ip: str, fingerprint: str) -> tuple[str, str]:
    return (
        f"login_failure:ip:{ip}",
        f"login_failure:ip_email:{ip}:{fingerprint}",
    )


def _audit_dedup_key(ip: str, fingerprint: str, epoch: int) -> str:
    return f"audit:auth_rate_limited:{ip}:{fingerprint}:{epoch // 60}"


async def should_audit_rate_limited_login(ip: str, email: str) -> bool:
    """Reserve one audit write per IP/fingerprint minute.

    Audit persistence is best-effort: a Redis outage must not interfere with
    the fail-closed login 429 response.
    """
    fingerprint = email_fingerprint(email)
    key = _audit_dedup_key(ip, fingerprint, int(time.time()))
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        return bool(await client.set(key, "1", nx=True, ex=60))
    except (RedisError, OSError) as exc:
        logger.warning(
            "auth.rate_limit_audit_dedup_unavailable",
            service="redis",
            error=str(exc),
        )
        return False
    finally:
        if client is not None:
            await client.aclose()


async def _get_counts(ip_key: str, pair_key: str) -> tuple[int, int]:
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        ip_count, pair_count = await client.mget(ip_key, pair_key)
        return int(ip_count or 0), int(pair_count or 0)
    except (RedisError, OSError) as exc:
        logger.warning(
            "login.rate_limit_unavailable",
            service="redis",
            error=str(exc),
        )
        raise RateLimitUnavailableError from exc
    finally:
        if client is not None:
            await client.aclose()


async def login_is_blocked(ip: str, email: str) -> bool:
    ip_key, pair_key = _keys(ip, email_fingerprint(email))
    counts = await _get_counts(ip_key, pair_key)
    ip_count, pair_count = counts
    return (
        ip_count >= settings.LOGIN_MAX_FAILURES_PER_IP
        or pair_count >= settings.LOGIN_MAX_FAILURES_PER_EMAIL_IP
    )


async def record_login_failure(ip: str, email: str) -> None:
    ip_key, pair_key = _keys(ip, email_fingerprint(email))
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pipe = client.pipeline(transaction=False)
        pipe.incr(ip_key)
        pipe.incr(pair_key)
        pipe.expire(ip_key, settings.LOGIN_FAILURE_WINDOW_SECONDS)
        pipe.expire(pair_key, settings.LOGIN_FAILURE_WINDOW_SECONDS)
        await pipe.execute()
    except (RedisError, OSError) as exc:
        logger.warning(
            "login.rate_limit_write_failed",
            service="redis",
            error=str(exc),
        )
        raise RateLimitUnavailableError from exc
    finally:
        if client is not None:
            await client.aclose()


async def clear_login_failures(ip: str, email: str) -> None:
    ip_key, pair_key = _keys(ip, email_fingerprint(email))
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await client.delete(pair_key)
    except (RedisError, OSError) as exc:
        logger.warning(
            "login.rate_limit_clear_failed",
            service="redis",
            error=str(exc),
        )
        raise RateLimitUnavailableError from exc
    finally:
        if client is not None:
            await client.aclose()


async def consume_tenant_application_quota(ip: str, email: str) -> bool:
    """Atomically consume one public-application submission quota slot.

    The quota is deliberately consumed before database and email work. If Redis
    is unavailable, submission fails closed just like login rate limiting.
    """

    now = int(time.time())
    hour_bucket = now // 3_600
    day_bucket = now // 86_400
    ip_key = f"tenant_application:ip:{ip}:{hour_bucket}"
    email_key = f"tenant_application:email:{email_fingerprint(email)}:{day_bucket}"
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        allowed = await client.eval(
            """
            local ip_count = tonumber(redis.call('GET', KEYS[1]) or '0')
            local email_count = tonumber(redis.call('GET', KEYS[2]) or '0')
            if ip_count >= tonumber(ARGV[1]) or email_count >= tonumber(ARGV[2]) then
                return 0
            end
            redis.call('INCR', KEYS[1])
            redis.call('EXPIRE', KEYS[1], ARGV[3])
            redis.call('INCR', KEYS[2])
            redis.call('EXPIRE', KEYS[2], ARGV[4])
            return 1
            """,
            2,
            ip_key,
            email_key,
            settings.TENANT_APPLICATION_MAX_PER_IP_PER_HOUR,
            settings.TENANT_APPLICATION_MAX_PER_EMAIL_PER_DAY,
            3_600 - (now % 3_600) + 60,
            86_400 - (now % 86_400) + 60,
        )
        return bool(allowed)
    except (RedisError, OSError) as exc:
        logger.warning(
            "tenant_application.rate_limit_unavailable",
            service="redis",
            error=str(exc),
        )
        raise RateLimitUnavailableError from exc
    finally:
        if client is not None:
            await client.aclose()
