"""Bearer-token verification against an OIDC provider (built for Authelia), or a static token.

The MCP server is only an OAuth *resource server*: clients obtain tokens from the provider
(found via /.well-known/oauth-protected-resource, served by the MCP SDK) and send them here.

Verification:
  * JWT access tokens (Authelia: `access_token_signed_response_alg: RS256` on the client)
    are checked locally against the provider's JWKS.
  * Opaque tokens (Authelia's default) are checked via the introspection endpoint, which
    requires a confidential client for this server (SCRIPTHOST_OIDC_INTROSPECTION_*).
  * Optionally the user's groups (userinfo `groups` claim) must intersect REQUIRED_GROUPS.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any

import httpx
import jwt
from mcp.server.auth.provider import AccessToken
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Config

log = logging.getLogger("scripthost.auth")

CACHE_SECONDS = 60


class OIDCTokenVerifier:
    def __init__(self, config: Config, http: httpx.AsyncClient | None = None):
        if not config.oidc_issuer:
            raise ValueError("SCRIPTHOST_OIDC_ISSUER is required when SCRIPTHOST_AUTH_MODE=oidc")
        self.config = config
        self.issuer = config.oidc_issuer.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=10)
        self._discovery: dict[str, Any] | None = None
        self._jwks: jwt.PyJWKSet | None = None
        self._jwks_fetched = 0.0
        self._cache: dict[str, tuple[float, AccessToken | None]] = {}
        self._lock = asyncio.Lock()

    async def discovery(self) -> dict[str, Any]:
        if self._discovery is None:
            resp = await self.http.get(f"{self.issuer}/.well-known/openid-configuration")
            resp.raise_for_status()
            self._discovery = resp.json()
        return self._discovery

    async def verify_token(self, token: str) -> AccessToken | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(key)
        now = time.time()
        if cached and cached[0] > now:
            return cached[1]
        try:
            result = await self._verify(token)
        except Exception as exc:  # noqa: BLE001 - any failure means "not authenticated"
            log.warning("token verification error: %s", exc)
            result = None
        expiry = now + CACHE_SECONDS
        if result and result.expires_at:
            expiry = min(expiry, result.expires_at)
        self._cache[key] = (expiry, result)
        if len(self._cache) > 1000:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        return result

    async def _verify(self, token: str) -> AccessToken | None:
        claims = await (self._verify_jwt(token) if token.count(".") == 2 else self._introspect(token))
        if claims is None:
            return None
        if claims.get("iss") and claims["iss"].rstrip("/") != self.issuer:
            log.info("rejected token from issuer %s", claims.get("iss"))
            return None
        if self.config.oidc_audience:
            aud = claims.get("aud") or []
            aud = [aud] if isinstance(aud, str) else aud
            if self.config.oidc_audience not in aud:
                log.info("rejected token: audience %s does not contain %s", aud, self.config.oidc_audience)
                return None
        client_id = str(claims.get("client_id") or claims.get("azp") or "unknown")
        if self.config.oidc_allowed_clients and client_id not in self.config.oidc_allowed_clients:
            log.info("rejected token issued to client %s", client_id)
            return None
        scopes = (claims.get("scope") or "").split() if isinstance(claims.get("scope"), str) else list(claims.get("scp") or [])
        if self.config.oidc_required_groups:
            groups = await self._groups(token)
            if not set(groups) & set(self.config.oidc_required_groups):
                log.info("rejected token for %s: not in %s", claims.get("sub"), self.config.oidc_required_groups)
                return None
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(claims["exp"]) if claims.get("exp") else None,
            subject=claims.get("sub"),
            claims={k: v for k, v in claims.items() if k in ("iss", "aud", "username", "sub")},
        )

    async def _verify_jwt(self, token: str) -> dict[str, Any] | None:
        header = jwt.get_unverified_header(token)
        key = await self._signing_key(header.get("kid"))
        return jwt.decode(
            token,
            key=key,
            algorithms=[header.get("alg", "RS256")] if header.get("alg") in ("RS256", "ES256", "PS256", "RS512", "ES512") else ["RS256"],
            options={"verify_aud": False, "require": ["exp"]},
            leeway=30,
        )

    async def _signing_key(self, kid: str | None) -> Any:
        async with self._lock:
            for attempt in range(2):
                if self._jwks is None or attempt == 1 or time.time() - self._jwks_fetched > 3600:
                    disc = await self.discovery()
                    resp = await self.http.get(disc["jwks_uri"])
                    resp.raise_for_status()
                    self._jwks = jwt.PyJWKSet.from_dict(resp.json())
                    self._jwks_fetched = time.time()
                for k in self._jwks.keys:
                    if kid is None or k.key_id == kid:
                        return k.key
            raise ValueError(f"no signing key with kid {kid!r}")

    async def _introspect(self, token: str) -> dict[str, Any] | None:
        cid, secret = self.config.oidc_introspection_client_id, self.config.oidc_introspection_client_secret
        if not (cid and secret):
            raise ValueError("opaque token received but SCRIPTHOST_OIDC_INTROSPECTION_CLIENT_ID/SECRET are not set")
        disc = await self.discovery()
        endpoint = disc.get("introspection_endpoint")
        if not endpoint:
            raise ValueError("provider does not advertise an introspection_endpoint")
        resp = await self.http.post(endpoint, data={"token": token, "token_type_hint": "access_token"}, auth=(cid, secret))
        resp.raise_for_status()
        data = resp.json()
        if not data.get("active"):
            return None
        if data.get("exp") and data["exp"] < time.time():
            return None
        data.setdefault("iss", self.issuer)
        return data

    async def _groups(self, token: str) -> list[str]:
        disc = await self.discovery()
        resp = await self.http.get(disc["userinfo_endpoint"], headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            log.info("userinfo request failed: %s", resp.status_code)
            return []
        groups = resp.json().get("groups") or []
        return [groups] if isinstance(groups, str) else list(groups)


class StaticTokenMiddleware:
    """Minimal ASGI middleware for SCRIPTHOST_AUTH_MODE=token (LAN testing, scripts)."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            header = dict(scope.get("headers") or []).get(b"authorization", b"")
            supplied = header[7:] if header.lower().startswith(b"bearer ") else b""
            if not hmac.compare_digest(supplied, self.token):
                await JSONResponse({"error": "invalid_token"}, status_code=401,
                                   headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})(scope, receive, send)
                return
        await self.app(scope, receive, send)
