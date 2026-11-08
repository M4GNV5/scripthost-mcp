import json
import time

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from scripthost.auth import OIDCTokenVerifier, StaticTokenMiddleware
from scripthost.config import Config
from scripthost.server import build

ISSUER = "https://auth.home.lan"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())) | {"kid": "k1", "use": "sig", "alg": "RS256"}


def provider(groups=("scripthost-admins",)):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks.json",
                "introspection_endpoint": f"{ISSUER}/api/oidc/introspection",
                "userinfo_endpoint": f"{ISSUER}/api/oidc/userinfo",
            })
        if path == "/jwks.json":
            return httpx.Response(200, json={"keys": [JWK]})
        if path == "/api/oidc/introspection":
            assert request.headers["authorization"].startswith("Basic ")
            token = dict(httpx.QueryParams(request.content.decode()))["token"]
            if token == "opaque-good":
                return httpx.Response(200, json={"active": True, "client_id": "claude-code", "sub": "u1",
                                                 "scope": "openid groups", "exp": int(time.time()) + 300,
                                                 "aud": ["https://scripthost.home.lan/mcp"]})
            return httpx.Response(200, json={"active": False})
        if path == "/api/oidc/userinfo":
            return httpx.Response(200, json={"sub": "u1", "groups": list(groups)})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def cfg(**kw):
    base = dict(auth_mode="oidc", oidc_issuer=ISSUER, oidc_introspection_client_id="scripthost",
                oidc_introspection_client_secret="x", oidc_required_groups=["scripthost-admins"],
                oidc_audience="https://scripthost.home.lan/mcp")
    return Config(**(base | kw))


def make_jwt(**claims):
    payload = {"iss": ISSUER, "sub": "u1", "client_id": "claude-code", "aud": ["https://scripthost.home.lan/mcp"],
               "exp": int(time.time()) + 300, "scope": "openid groups"} | claims
    return jwt.encode(payload, KEY, algorithm="RS256", headers={"kid": "k1"})


async def test_opaque_token_introspection_and_groups():
    v = OIDCTokenVerifier(cfg(), http=provider())
    tok = await v.verify_token("opaque-good")
    assert tok and tok.subject == "u1" and tok.client_id == "claude-code"
    assert await v.verify_token("opaque-bad") is None


async def test_allowed_clients():
    v = OIDCTokenVerifier(cfg(oidc_allowed_clients=["other"]), http=provider())
    assert await v.verify_token("opaque-good") is None
    v = OIDCTokenVerifier(cfg(oidc_allowed_clients=["claude-code"]), http=provider())
    assert await v.verify_token("opaque-good")


async def test_group_membership_required():
    v = OIDCTokenVerifier(cfg(), http=provider(groups=("family",)))
    assert await v.verify_token("opaque-good") is None


async def test_jwt_tokens():
    v = OIDCTokenVerifier(cfg(), http=provider())
    assert await v.verify_token(make_jwt())
    assert await v.verify_token(make_jwt(aud=["something-else"])) is None
    assert await v.verify_token(make_jwt(iss="https://evil")) is None
    assert await v.verify_token(make_jwt(exp=int(time.time()) - 600)) is None
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({"iss": ISSUER, "exp": int(time.time()) + 60}, other, algorithm="RS256", headers={"kid": "k1"})
    assert await v.verify_token(forged) is None


def test_http_requires_token_and_serves_metadata(tmp_path):
    config = cfg(data_dir=tmp_path, public_url="https://scripthost.home.lan/mcp",
                 oidc_required_scopes=["openid", "groups"])
    mcp, *_ = build(config)
    with TestClient(mcp.streamable_http_app(host="0.0.0.0")) as http:
        r = http.post("/mcp", json={})
        assert r.status_code == 401
        assert "resource_metadata" in r.headers["www-authenticate"]
        meta = http.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["authorization_servers"] == [ISSUER + "/"] or meta["authorization_servers"] == [ISSUER]
        assert meta["scopes_supported"] == ["openid", "groups"], meta
        assert http.get("/healthz").status_code == 200


def test_static_token_middleware(tmp_path):
    config = Config(data_dir=tmp_path, auth_mode="token", static_token="t0ken")
    mcp, *_ = build(config)
    app = StaticTokenMiddleware(mcp.streamable_http_app(host="0.0.0.0"), "t0ken")
    with TestClient(app) as http:
        assert http.get("/healthz").status_code == 401
        assert http.get("/healthz", headers={"Authorization": "Bearer t0ken"}).status_code == 200
