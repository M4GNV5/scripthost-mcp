"""Configuration, read from environment variables (prefix SCRIPTHOST_)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(f"SCRIPTHOST_{name}")
    return value if value not in (None, "") else default


def _list(name: str) -> list[str]:
    raw = _env(name, "") or ""
    return [item.strip() for item in raw.replace(",", " ").split() if item.strip()]


@dataclass
class Config:
    data_dir: Path = Path("/data")
    host: str = "0.0.0.0"
    port: int = 8080
    # Public URL of the MCP endpoint as clients see it, e.g. https://scripthost.home.lan/mcp
    public_url: str = "http://localhost:8080/mcp"

    # auth_mode: "oidc" (Authelia or any OIDC provider), "token" (static bearer), "none"
    auth_mode: str = "oidc"
    static_token: str | None = None

    oidc_issuer: str | None = None
    # Confidential client used by *this server* to call the introspection endpoint
    oidc_introspection_client_id: str | None = None
    oidc_introspection_client_secret: str | None = None
    # If set, the token's audience must contain this value
    oidc_audience: str | None = None
    # If set, the user (via userinfo "groups" claim) must be in one of these groups
    oidc_required_groups: list[str] = field(default_factory=list)
    oidc_required_scopes: list[str] = field(default_factory=list)
    # If set, only tokens issued to these OAuth client ids are accepted (e.g. "claude-code")
    oidc_allowed_clients: list[str] = field(default_factory=list)
    # Extra Host header values accepted (DNS-rebinding protection); empty = derived from public_url
    allowed_hosts: list[str] = field(default_factory=list)

    # dotenv file with operator-provided secrets; default <data_dir>/secrets.env
    secrets_file: Path | None = None

    log_retention_runs: int = 50
    default_test_timeout: int = 120
    max_read_bytes: int = 200_000

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            data_dir=Path(_env("DATA_DIR", "/data")),
            host=_env("HOST", "0.0.0.0"),
            port=int(_env("PORT", "8080")),
            public_url=_env("PUBLIC_URL", "http://localhost:8080/mcp"),
            auth_mode=(_env("AUTH_MODE", "oidc") or "oidc").lower(),
            static_token=_env("STATIC_TOKEN"),
            oidc_issuer=_env("OIDC_ISSUER"),
            oidc_introspection_client_id=_env("OIDC_INTROSPECTION_CLIENT_ID"),
            oidc_introspection_client_secret=_env("OIDC_INTROSPECTION_CLIENT_SECRET"),
            oidc_audience=_env("OIDC_AUDIENCE"),
            oidc_required_groups=_list("OIDC_REQUIRED_GROUPS"),
            oidc_required_scopes=_list("OIDC_REQUIRED_SCOPES"),
            oidc_allowed_clients=_list("OIDC_ALLOWED_CLIENTS"),
            allowed_hosts=_list("ALLOWED_HOSTS"),
            secrets_file=Path(_env("SECRETS_FILE")) if _env("SECRETS_FILE") else None,
            log_retention_runs=int(_env("LOG_RETENTION_RUNS", "50")),
            default_test_timeout=int(_env("DEFAULT_TEST_TIMEOUT", "120")),
            max_read_bytes=int(_env("MAX_READ_BYTES", "200000")),
        )
