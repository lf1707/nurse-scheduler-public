"""Print the bootstrap-owner database URL used by production migrations."""

from __future__ import annotations

from os import environ
from urllib.parse import quote


def main() -> None:
    user = environ["POSTGRES_USER"]
    password = quote(environ["POSTGRES_PASSWORD"])
    database = environ["POSTGRES_DB"]
    print(f"postgresql+asyncpg://{user}:{password}@db:5432/{database}")


if __name__ == "__main__":
    main()
