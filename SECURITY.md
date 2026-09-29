# Security Policy

## Reporting a vulnerability

Do not open a public issue for a security vulnerability.

Use GitHub's private vulnerability reporting for this repository. Include a
clear description, affected component, reproduction steps, impact assessment,
and any minimal proof of concept. Please do not include credentials, customer
data, or requests to test systems you do not own.

## Supported versions

Security fixes are made against the latest `main` branch. There is currently no
guarantee of backports to older source snapshots.

## Security model

- PostgreSQL row-level security is part of the tenant-isolation boundary.
- Redis backs authentication throttling and asynchronous task coordination.
- Secrets belong in environment configuration, never in source control.
- Audit exports are encrypted with a configured passphrase.
- Production deployments must use TLS, strong unique secrets, a non-superuser
  application database role, and regularly patched infrastructure.

This source distribution does not ship an operational deployment
configuration. Operators are responsible for hardening the environment in
which they run the application.
