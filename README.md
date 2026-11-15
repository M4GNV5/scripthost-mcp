# scripthost-mcp

An MCP server that lets an LLM **write, deploy, test, schedule and supervise Python projects** on
your own machine. For example, a home server with access to smart-home devices, or a box that hosts
small web services.

You describe what you want ("poll the heat pump every 5 minutes and push the values to MQTT"). The
LLM searches for an existing project, writes or updates the code, runs it once to test it, and then
deploys it as a cron job or a supervised service.

## Features

| | |
|---|---|
| **Projects** | Source files + mandatory description, numbered **revisions**, rollback |
| **Search** | `search_projects` over name, description and source code |
| **Dependencies** | `requirements` → per-project virtualenv (built with `uv` when available) |
| **Test runs** | `run_once` with arguments/env overrides, timeout, output returned inline |
| **Services** | `start_service` / `stop_service`, restart policy `no` / `on-failure` / `always` with exponential backoff, auto-start on boot |
| **Cron** | 5-field cron expressions, optional per-run timeout, overlapping runs are skipped |
| **Logs** | stdout+stderr captured per run, `get_logs` with tail + regex filter, retention per project |
| **Files** | persistent working dir per project: `list_files`, `read_file` (text or base64), `write_work_file` |
| **Secrets** | defined by you in a `.env` file; the LLM only sees names, attaches them to projects, and gets values masked in logs/files |
| **Auth** | OIDC resource server (built for **Authelia**), static bearer token, or none |

### Tools

`search_projects`, `list_projects`, `get_project`, `list_revisions`, `list_secrets`, `create_project`,
`update_project`, `rollback_project`, `delete_project`, `run_once`, `start_service`,
`stop_service`, `restart_service`, `stop_run`, `get_status`, `list_runs`, `get_logs`,
`list_files`, `read_file`, `write_work_file`, `delete_work_file`

### Runtime environment of a script

- cwd = persistent working dir (`$SCRIPTHOST_WORK_DIR`); source is in `$SCRIPTHOST_SRC_DIR` (on `PYTHONPATH`)
- `entrypoint` is a file (`main.py`) or a module name (run with `python -m`)
- env = project `env` + per-run overrides + attached secrets, plus `SCRIPTHOST_PROJECT`, `SCRIPTHOST_REVISION`.
  Scripts do **not** inherit the server's own environment.
- each run is its own process group; stop = SIGTERM, then SIGKILL after 10 s

## Running

```bash
cd deploy
mkdir -p data && sudo chown 1000:1000 data   # container runs as uid 1000
cp secrets.env.example data/secrets.env && chmod 600 data/secrets.env   # then fill in
docker compose up -d --build
```

Or without Docker: `pip install . && SCRIPTHOST_DATA_DIR=./data scripthost-mcp`.

Data lives in `/data` (`scripthost.db` + `projects/<name>/{rev,work,venv,logs}`).

### Secrets

Put them in `/data/secrets.env` (path configurable via `SCRIPTHOST_SECRETS_FILE`), dotenv syntax:

```dotenv
# Home Assistant long-lived token
HA_TOKEN=eyJhbGciOi...
export MQTT_PASSWORD='p@ss word'
```

- The file is re-read whenever it changes. New values apply to the **next run**; restart a
  service to pick them up.
- The LLM calls `list_secrets` (names + which projects use them) and attaches them with
  `create_project(..., secrets=["HA_TOKEN"])`. Each one becomes an env var of the same name. Only
  attached secrets are injected, and a run's `env` overrides cannot shadow them.
- Every value of 4+ characters is replaced with `***NAME***` in everything the tools return: logs,
  `run_once` output, `read_file` text, source shown by `get_project`.
- A run fails with a clear error if an attached secret is missing from the file.

Masking only prevents *accidental* exposure. The LLM writes the code, so it could still encode a
value (base64, reversed, split across lines) and read it back. Scripts also run as the same user as
the server, so they could read `secrets.env` directly. Separating these needs the per-project
sandbox from the roadmap.

### Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `SCRIPTHOST_DATA_DIR` | `/data` | |
| `SCRIPTHOST_HOST` / `SCRIPTHOST_PORT` | `0.0.0.0` / `8080` | |
| `SCRIPTHOST_PUBLIC_URL` | `http://localhost:8080/mcp` | URL clients use for the MCP endpoint; advertised in OAuth metadata |
| `SCRIPTHOST_AUTH_MODE` | `oidc` | `oidc`, `token` or `none` |
| `SCRIPTHOST_STATIC_TOKEN` | | bearer token for `token` mode |
| `SCRIPTHOST_OIDC_ISSUER` | | e.g. `https://auth.home.lan` |
| `SCRIPTHOST_OIDC_REQUIRED_SCOPES` | | advertised to clients and required in tokens, e.g. `openid profile groups` |
| `SCRIPTHOST_OIDC_REQUIRED_GROUPS` | | user must be in one of these groups (checked via userinfo) |
| `SCRIPTHOST_OIDC_ALLOWED_CLIENTS` | | only accept tokens issued to these client ids |
| `SCRIPTHOST_OIDC_AUDIENCE` | | if set, token `aud` must contain it |
| `SCRIPTHOST_OIDC_INTROSPECTION_CLIENT_ID` / `_SECRET` | | needed for opaque (non-JWT) access tokens |
| `SCRIPTHOST_SECRETS_FILE` | `<data_dir>/secrets.env` | operator-defined secrets (see above) |
| `SCRIPTHOST_LOG_RETENTION_RUNS` | `50` | finished runs (and their logs) kept per project |
| `SCRIPTHOST_DEFAULT_TEST_TIMEOUT` | `120` | default `run_once` timeout in seconds |

## Authentication with Authelia

scripthost is only an OAuth **resource server**. It serves
`/.well-known/oauth-protected-resource`, which points MCP clients at Authelia. It then checks every
bearer token: JWTs are verified locally against Authelia's JWKS, and opaque tokens through
Authelia's introspection endpoint. It also checks issuer, client id, scopes and group membership.

Authelia has no dynamic client registration, so the MCP client is pre-registered. The setup below
is for **Claude Code** on your LAN. It uses a public client with PKCE and a fixed localhost
callback port.

`configuration.yml` (Authelia 4.38+ syntax; adapt to your version):

```yaml
identity_providers:
  oidc:
    clients:
      - client_id: 'claude-code'
        client_name: 'Claude Code → scripthost'
        public: true
        authorization_policy: 'two_factor'
        require_pkce: true
        pkce_challenge_method: 'S256'
        redirect_uris:
          - 'http://localhost:33418/callback'
        scopes: ['openid', 'profile', 'groups', 'offline_access']
        grant_types: ['authorization_code', 'refresh_token']
        response_types: ['code']
        token_endpoint_auth_method: 'none'
        # JWT access tokens, verified locally against Authelia's JWKS (Authelia's default is
        # opaque tokens, which would need a separate introspection client)
        access_token_signed_response_alg: 'RS256'
        userinfo_signed_response_alg: 'none'
```

<details><summary>Alternative: opaque tokens + introspection client</summary>

Leave out `access_token_signed_response_alg` and add a confidential client for scripthost. Then set
`SCRIPTHOST_OIDC_INTROSPECTION_CLIENT_ID` and `SCRIPTHOST_OIDC_INTROSPECTION_CLIENT_SECRET`:

```yaml
      - client_id: 'scripthost'
        client_name: 'scripthost introspection'
        client_secret: '$pbkdf2-sha512$310000$...'   # authelia crypto hash generate pbkdf2 --random
        public: false
        authorization_policy: 'one_factor'
        grant_types: ['client_credentials']
        token_endpoint_auth_method: 'client_secret_basic'
```
</details>

Create a group such as `scripthost-admins` for the users who may use it, and set
`SCRIPTHOST_OIDC_REQUIRED_GROUPS` to it.

Then in Claude Code:

```bash
claude mcp add --transport http --scope user \
  --client-id claude-code --callback-port 33418 \
  scripthost https://scripthost.home.lan/mcp
# then inside Claude Code: /mcp → scripthost → Authenticate
```

Put scripthost behind your existing reverse proxy with TLS. **Don't** put Authelia's forward-auth
in front of `/mcp`: that would redirect API calls to a login page. The server checks tokens itself.
`/.well-known/oauth-protected-resource*` and `/healthz` must be reachable without auth.

> **LAN-only note:** claude.ai and the Claude mobile/desktop *connectors* connect from
> Anthropic's cloud, so they need a publicly reachable endpoint (and Authelia token endpoint).
> Claude Code runs locally and works LAN-only. For quick experiments, use
> `SCRIPTHOST_AUTH_MODE=token` with
> `claude mcp add --transport http scripthost http://host:8080/mcp --header "Authorization: Bearer <token>"`.

## Security model

This server executes code an LLM writes, on a machine inside your network. Treat access to it like
root-ish shell access to that box:

- Always run with auth enabled. Prefer the Docker image: scripts run as an unprivileged user
  inside the container, not as root on the host.
- `network_mode: host` gives scripts full LAN access, which is the point for smart-home use, but
  also means a bad script can reach everything the host can.
- Secret values never pass through the LLM, but any script can read `secrets.env` and the
  database, since it runs as the server's user (see *Secrets*).

## Development

```bash
pip install -e '.[test]'
pytest
```

## Roadmap ideas

- Optional per-project container sandbox (Docker/podman) with network and resource limits
- Per-project resource limits (memory/CPU) for plain subprocesses
- Reverse-proxy integration for services that serve HTTP (auto-assign ports + hostnames)
- Notifications on service crash loops / failed cron runs (MQTT, ntfy, Home Assistant)
- Resource subscriptions for live log streaming
