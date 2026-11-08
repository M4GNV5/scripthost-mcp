"""MCP tool definitions and the HTTP entry point."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import OIDCTokenVerifier, StaticTokenMiddleware
from .config import Config
from .engine import Engine
from .store import Project, Store, StoreError, resolve_inside, validate_policy

log = logging.getLogger("scripthost")

INSTRUCTIONS = """\
scripthost runs Python projects on the operator's own machine (e.g. a home server with access to
smart-home devices on the LAN). Each project is a set of source files plus a description, kept in
numbered revisions.

Workflow:
1. search_projects first - reuse or update an existing project instead of creating a duplicate.
2. create_project / update_project with a precise description (what it does, inputs, outputs,
   which devices/services it talks to). Dependencies go in `requirements` (pip syntax).
3. run_once to test. Check exit code and output; iterate with update_project.
4. Then make it permanent: start_service for long-running processes (with restart_policy), or set
   `cron` for periodic jobs.

Runtime conventions for scripts:
- cwd is a persistent per-project working directory (also in $SCRIPTHOST_WORK_DIR); store state,
  caches and output files there. Source lives read-only in $SCRIPTHOST_SRC_DIR (on PYTHONPATH).
- Log to stdout/stderr (it is captured per run). Exit non-zero on failure.
- Put credentials in `secret_env` (never in source files); they are exposed as env vars.
- Services that serve HTTP should read the port from an env var such as PORT.
"""

FilesArg = Annotated[
    dict[str, str] | None,
    Field(description="Map of relative file path -> full UTF-8 file content, e.g. {'main.py': '...'}"),
]


def build(config: Config) -> tuple[MCPServer, Store, Engine]:
    store = Store(config.data_dir)
    engine = Engine(store, config)

    kwargs: dict[str, Any] = {}
    if config.auth_mode == "oidc":
        kwargs["token_verifier"] = OIDCTokenVerifier(config)
        kwargs["auth"] = AuthSettings(
            issuer_url=config.oidc_issuer,
            resource_server_url=config.public_url,
            required_scopes=config.oidc_required_scopes or None,
            validate_token_resource=False,  # audience is checked by the verifier (SCRIPTHOST_OIDC_AUDIENCE)
        )
    mcp = MCPServer(name="scripthost", instructions=INSTRUCTIONS, **kwargs)

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

    # ------------------------------------------------------------------ helpers
    def summary(p: Project) -> dict[str, Any]:
        return {
            "name": p.name,
            "description": p.description,
            "revision": p.revision,
            "restart_policy": p.restart_policy,
            "cron": p.cron,
            "service": engine.service_status(p.name)["state"],
            "updated_at": p.public_dict()["updated_at"],
        }

    def status(p: Project) -> dict[str, Any]:
        nxt = engine.next_cron_time(p.name)
        return {
            "service": engine.service_status(p.name),
            "service_enabled": p.service_enabled,
            "next_cron_run": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(nxt)) if nxt else None,
            "active_runs": [{"run_id": r.run_id, "trigger": r.trigger, "pid": r.proc.pid} for r in engine.active_runs(p.name)],
            "recent_runs": store.runs(p.name, 5),
        }

    def merge_env(current: dict[str, str], changes: dict[str, str | None] | None) -> dict[str, str]:
        out = dict(current)
        for k, v in (changes or {}).items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                raise StoreError(f"invalid environment variable name {k!r}")
            if v is None:
                out.pop(k, None)
            else:
                out[k] = str(v)
        return out

    def with_requirements(files: dict[str, str] | None, requirements: list[str] | None) -> dict[str, str]:
        files = dict(files or {})
        if requirements is not None:
            files["requirements.txt"] = "\n".join(requirements) + ("\n" if requirements else "")
        return files

    async def install_report(p: Project) -> dict[str, Any]:
        res = await engine.ensure_venv(p)
        if not res["ok"]:
            return {"dependencies": "INSTALL FAILED - fix requirements before running", "install_output": res["output"]}
        return {"dependencies": "installed" if res["changed"] else "unchanged"}

    def tail_file(path: Path, lines: int, grep: str | None) -> tuple[str, bool]:
        if not path.exists():
            return "", False
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if grep:
                pattern = re.compile(grep)
                fh.seek(max(0, size - 20_000_000))
                matched = [ln for ln in fh.read().decode(errors="replace").splitlines() if pattern.search(ln)]
                selected, truncated = matched[-lines:], len(matched) > lines
            else:
                chunk = min(size, max(lines * 400, 64_000))
                fh.seek(size - chunk)
                all_lines = fh.read().decode(errors="replace").splitlines()
                if chunk < size:
                    all_lines = all_lines[1:]  # first line is probably partial
                selected, truncated = all_lines[-lines:], chunk < size or len(all_lines) > lines
        text = "\n".join(selected)
        if len(text) > config.max_read_bytes:
            text, truncated = "…" + text[-config.max_read_bytes:], True
        return text, truncated

    def area_root(name: str, area: str, revision: int | None = None) -> Path:
        p = store.get(name)
        if area == "work":
            return store.work_dir(name)
        if area == "src":
            return store.rev_dir(name, revision or p.revision)
        if area == "logs":
            return store.log_dir(name)
        raise StoreError("area must be 'work', 'src' or 'logs'")

    # ------------------------------------------------------------------ discovery
    @mcp.tool(annotations=read_only)
    def search_projects(
        query: Annotated[str, Field(description="Keywords, e.g. 'heat pump mqtt' or 'backup nextcloud'")],
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search existing projects by name, description and source code. Use before creating a new project."""
        terms = [t for t in re.split(r"\W+", query.lower()) if t]
        if not terms:
            raise StoreError("query must contain at least one word")
        hits = []
        for p in store.all():
            name, desc = p.name.lower(), p.description.lower()
            source = ""
            for f in store.source_files(p.name, p.revision):
                if f.stat().st_size < 200_000:
                    source += f"\n{f.name.lower()}\n" + f.read_text(errors="ignore").lower()
            score = sum(5 * (t in name) + 3 * desc.count(t) + min(source.count(t), 5) for t in terms)
            if score:
                hits.append((score, p))
        hits.sort(key=lambda h: -h[0])
        return {"results": [summary(p) | {"score": s} for s, p in hits[:limit]]}

    @mcp.tool(annotations=read_only)
    def list_projects() -> dict[str, Any]:
        """List all projects with their description and current runtime state."""
        return {"projects": [summary(p) for p in store.all()]}

    @mcp.tool(annotations=read_only)
    def get_project(
        name: str,
        revision: Annotated[int | None, Field(description="Revision to show; default is the current one")] = None,
        include_source: bool = True,
    ) -> dict[str, Any]:
        """Show a project's configuration, runtime status and (by default) its full source code."""
        p = store.get(name)
        rev = revision or p.revision
        root = store.rev_dir(name, rev)
        files: dict[str, Any] = {}
        budget = config.max_read_bytes
        for f in store.source_files(name, rev):
            rel = str(f.relative_to(root))
            size = f.stat().st_size
            if include_source and size <= budget:
                try:
                    files[rel] = f.read_text(encoding="utf-8")
                    budget -= size
                    continue
                except UnicodeDecodeError:
                    pass
            files[rel] = f"<{size} bytes; use read_file(area='src') to read>"
        return {"project": p.public_dict(), "shown_revision": rev, "files": files, "status": status(p)}

    @mcp.tool(annotations=read_only)
    def list_revisions(name: str) -> dict[str, Any]:
        """List the source revisions of a project (newest first) with their change messages."""
        return {"revisions": store.revisions(name)}

    # ------------------------------------------------------------------ authoring
    @mcp.tool()
    async def create_project(
        name: Annotated[str, Field(description="Unique id: lowercase letters, digits, '-' and '_'")],
        description: Annotated[str, Field(description="What it does, inputs/outputs, devices/services it uses. Used for search.")],
        files: FilesArg,
        entrypoint: Annotated[str, Field(description="File to run (e.g. 'main.py') or a module name to run with -m")] = "main.py",
        requirements: Annotated[list[str] | None, Field(description="pip requirements, e.g. ['requests>=2', 'paho-mqtt']")] = None,
        args: Annotated[list[str] | None, Field(description="Default command-line arguments")] = None,
        env: Annotated[dict[str, str] | None, Field(description="Non-secret environment variables")] = None,
        secret_env: Annotated[dict[str, str] | None, Field(description="Secret env vars (tokens, passwords); never shown again")] = None,
        restart_policy: Literal["no", "on-failure", "always"] = "no",
        cron: Annotated[str | None, Field(description="5-field cron schedule, e.g. '*/10 * * * *'")] = None,
        cron_timeout: Annotated[int | None, Field(description="Kill cron runs after this many seconds")] = None,
        start_service: Annotated[bool, Field(description="Start as long-running service now (test with run_once first!)")] = False,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Create a new project from source files. Installs dependencies into its own virtualenv."""
        if cron:
            engine.validate_cron(cron)
        p = Project(
            name=name, description=description.strip(), entrypoint=entrypoint, args=args or [],
            env=merge_env({}, env), secret_env=merge_env({}, secret_env), restart_policy=restart_policy,
            cron=cron or None, cron_timeout=cron_timeout,
        )
        if not p.description:
            raise StoreError("description must not be empty")
        store.create(p, with_requirements(files, requirements), message)
        report = await install_report(p)
        engine.reschedule(p)
        if start_service and "FAILED" not in report["dependencies"]:
            await engine.start_service(name)
        p = store.get(name)
        return {"created": p.public_dict()} | report | {"status": status(p)}

    @mcp.tool()
    async def update_project(
        name: str,
        description: str | None = None,
        files: FilesArg = None,
        delete_files: Annotated[list[str] | None, Field(description="Relative paths to remove")] = None,
        replace_all_files: Annotated[bool, Field(description="If true, `files` is the complete new file set")] = False,
        requirements: Annotated[list[str] | None, Field(description="Replaces requirements.txt")] = None,
        entrypoint: str | None = None,
        args: list[str] | None = None,
        env: Annotated[dict[str, str | None] | None, Field(description="Merged into env; null value deletes a key")] = None,
        secret_env: Annotated[dict[str, str | None] | None, Field(description="Merged into secret env; null deletes")] = None,
        restart_policy: Literal["no", "on-failure", "always"] | None = None,
        cron: Annotated[str | None, Field(description="New cron schedule; empty string removes it")] = None,
        cron_timeout: int | None = None,
        message: Annotated[str | None, Field(description="Short change note for the revision history")] = None,
        restart_if_running: bool = True,
    ) -> dict[str, Any]:
        """Change a project's code (creates a new revision) and/or its configuration. Running services are restarted."""
        p = store.get(name)
        new_files = with_requirements(files, requirements)
        code_changed = bool(new_files or delete_files or replace_all_files)
        if entrypoint is not None:
            p.entrypoint = entrypoint
        if description is not None:
            if not description.strip():
                raise StoreError("description must not be empty")
            p.description = description.strip()
        if args is not None:
            p.args = args
        p.env = merge_env(p.env, env)
        p.secret_env = merge_env(p.secret_env, secret_env)
        if restart_policy is not None:
            validate_policy(restart_policy)
            p.restart_policy = restart_policy
        if cron is not None:
            if cron:
                engine.validate_cron(cron)
            p.cron = cron or None
        if cron_timeout is not None:
            p.cron_timeout = cron_timeout or None
        if entrypoint is not None and not code_changed:
            store._check_entrypoint(store.rev_dir(name, p.revision), p.entrypoint)
        if code_changed:
            # validates files + entrypoint first; only then persist the metadata changes
            old_entry = store.get(name).entrypoint
            store._x("UPDATE projects SET entrypoint=? WHERE name=?", (p.entrypoint, name))
            try:
                p.revision = store.new_revision(name, new_files, delete_files or [], replace_all_files, message)
            except Exception:
                store._x("UPDATE projects SET entrypoint=? WHERE name=?", (old_entry, name))
                raise
        store.save(p)
        p = store.get(name)
        report = await install_report(p)
        engine.reschedule(p)
        restarted = False
        runtime_changed = code_changed or env or secret_env or args is not None or entrypoint is not None
        if restart_if_running and runtime_changed and engine.service_status(name)["state"] in ("running", "backoff"):
            if "FAILED" not in report["dependencies"]:
                await engine.restart_service(name)
                restarted = True
        return {"project": p.public_dict(), "service_restarted": restarted} | report

    @mcp.tool()
    async def rollback_project(name: str, revision: int, message: str | None = None) -> dict[str, Any]:
        """Create a new revision whose code is identical to an older revision (restarts a running service)."""
        store.get(name)
        new_rev = store.new_revision(name, {}, [], False, message or f"rollback to revision {revision}", base_revision=revision)
        p = store.get(name)
        report = await install_report(p)
        if engine.service_status(name)["state"] in ("running", "backoff"):
            await engine.restart_service(name)
        return {"revision": new_rev} | report

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def delete_project(
        name: str,
        confirm: Annotated[bool, Field(description="Must be true. Deletes code, all revisions, working dir and logs.")],
    ) -> dict[str, Any]:
        """Permanently delete a project and all of its data."""
        if not confirm:
            raise StoreError("set confirm=true to delete")
        store.get(name)
        await engine.stop_service(name, persist=False)
        for run in engine.active_runs(name):
            await engine.stop_run(run.run_id)
        p = store.get(name)
        p.cron = None
        engine.reschedule(p)
        store.delete(name)
        return {"deleted": name}

    # ------------------------------------------------------------------ running
    @mcp.tool()
    async def run_once(
        name: str,
        args: Annotated[list[str] | None, Field(description="Override the default arguments for this run")] = None,
        env: Annotated[dict[str, str] | None, Field(description="Extra env vars for this run only")] = None,
        timeout: Annotated[int | None, Field(description="Seconds before the run is killed (default 120; 0 = none)")] = None,
        wait: Annotated[bool, Field(description="Wait for completion and return output; false returns run_id immediately")] = True,
        tail_lines: int = 200,
    ) -> dict[str, Any]:
        """Run the current revision once (for testing or ad-hoc jobs). Uses the persistent working dir."""
        t = config.default_test_timeout if timeout is None else timeout
        run = await engine.launch(name, "manual", args=args, env=env, timeout=t or None)
        if not wait:
            return {"run_id": run.run_id, "status": "running"}
        try:
            await asyncio.wait_for(run.done.wait(), (t or 3600) + 30)
        except asyncio.TimeoutError:
            return {"run_id": run.run_id, "status": "running", "note": "still running; use get_logs"}
        output, truncated = tail_file(store.run_log(name, run.run_id), tail_lines, None)
        return store.run_get(run.run_id) | {"output": output, "output_truncated": truncated}

    @mcp.tool()
    async def start_service(name: str) -> dict[str, Any]:
        """Start the project as a supervised long-running process (also auto-started when the server boots).
        Restarts follow the project's restart_policy, with exponential backoff."""
        await engine.start_service(name)
        await asyncio.sleep(1.5)
        p = store.get(name)
        return {"status": status(p)}

    @mcp.tool()
    async def stop_service(name: str) -> dict[str, Any]:
        """Stop a running service and disable auto-start."""
        stopped = await engine.stop_service(name)
        return {"was_running": stopped, "status": status(store.get(name))}

    @mcp.tool()
    async def restart_service(name: str) -> dict[str, Any]:
        """Restart a service (starts it if stopped)."""
        await engine.restart_service(name)
        await asyncio.sleep(1.5)
        return {"status": status(store.get(name))}

    @mcp.tool()
    async def stop_run(run_id: int) -> dict[str, Any]:
        """Terminate a specific active run (manual, cron or service run)."""
        stopped = await engine.stop_run(run_id)
        return {"stopped": stopped} | store.run_get(run_id)

    @mcp.tool(annotations=read_only)
    def get_status(name: str | None = None) -> dict[str, Any]:
        """Runtime status of one project, or an overview of all projects if no name is given."""
        if name:
            return {"project": name} | status(store.get(name))
        return {"projects": [summary(p) | {"next_cron_run": status(p)["next_cron_run"]} for p in store.all()]}

    @mcp.tool(annotations=read_only)
    def list_runs(name: str, limit: int = 20) -> dict[str, Any]:
        """List recent runs of a project (newest first) with trigger, status and exit code."""
        store.get(name)
        return {"runs": store.runs(name, limit)}

    @mcp.tool(annotations=read_only)
    def get_logs(
        name: str,
        run_id: Annotated[int | None, Field(description="Specific run; default is the most recent run")] = None,
        tail_lines: int = 200,
        grep: Annotated[str | None, Field(description="Only return lines matching this regex")] = None,
        install_log: Annotated[bool, Field(description="Return the dependency installation log instead")] = False,
    ) -> dict[str, Any]:
        """Read captured stdout/stderr of a run (or of the latest run)."""
        store.get(name)
        if install_log:
            text, truncated = tail_file(store.log_dir(name) / "install.log", tail_lines, grep)
            return {"log": text, "truncated": truncated}
        run = store.run_get(run_id) if run_id else store.latest_run(name)
        if not run or run["project"] != name:
            raise StoreError(f"no run found for {name!r}")
        text, truncated = tail_file(store.run_log(name, run["run_id"]), tail_lines, grep)
        return run | {"log": text, "truncated": truncated}

    # ------------------------------------------------------------------ files
    @mcp.tool(annotations=read_only)
    def list_files(
        name: str,
        path: str = ".",
        area: Annotated[Literal["work", "src", "logs"], Field(description="work = persistent working dir, src = source code")] = "work",
        revision: int | None = None,
    ) -> dict[str, Any]:
        """List files (recursively, max 1000) in a project's working dir, source or log directory."""
        root = area_root(name, area, revision)
        base = resolve_inside(root, path)
        if not base.exists():
            raise StoreError(f"{path!r} does not exist")
        entries = []
        for f in sorted(base.rglob("*")) if base.is_dir() else [base]:
            if len(entries) >= 1000:
                break
            st = f.stat()
            entries.append({
                "path": str(f.relative_to(root)),
                "type": "dir" if f.is_dir() else "file",
                "size": st.st_size if f.is_file() else None,
                "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
            })
        return {"area": area, "files": entries, "truncated": len(entries) >= 1000}

    @mcp.tool(annotations=read_only)
    def read_file(
        name: str,
        path: str,
        area: Literal["work", "src", "logs"] = "work",
        offset: int = 0,
        max_bytes: int | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Read a file from a project's working dir (outputs, state), source or logs. Binary files are returned as base64."""
        f = resolve_inside(area_root(name, area, revision), path)
        if not f.is_file():
            raise StoreError(f"{path!r} is not a file")
        limit = min(max_bytes or config.max_read_bytes, config.max_read_bytes)
        size = f.stat().st_size
        with open(f, "rb") as fh:
            fh.seek(offset)
            data = fh.read(limit)
        result: dict[str, Any] = {"path": path, "size": size, "offset": offset, "returned_bytes": len(data),
                                  "truncated": offset + len(data) < size}
        try:
            result["content"] = data.decode("utf-8")
            result["encoding"] = "utf-8"
        except UnicodeDecodeError:
            result["content"] = base64.b64encode(data).decode()
            result["encoding"] = "base64"
        return result

    @mcp.tool()
    def write_work_file(
        name: str,
        path: str,
        content: str,
        encoding: Literal["utf-8", "base64"] = "utf-8",
    ) -> dict[str, Any]:
        """Write an input/config/data file into the project's persistent working dir (not the source; use update_project for code)."""
        f = resolve_inside(store.work_dir(store.get(name).name), path)
        f.parent.mkdir(parents=True, exist_ok=True)
        data = base64.b64decode(content) if encoding == "base64" else content.encode()
        f.write_bytes(data)
        return {"path": path, "size": len(data)}

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    def delete_work_file(name: str, path: str) -> dict[str, Any]:
        """Delete a file or directory from the project's working dir."""
        f = resolve_inside(store.work_dir(store.get(name).name), path)
        if f == store.work_dir(name).resolve():
            raise StoreError("refusing to delete the working dir itself")
        if f.is_dir():
            shutil.rmtree(f)
        elif f.exists():
            f.unlink()
        else:
            raise StoreError(f"{path!r} does not exist")
        return {"deleted": path}

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "projects": len(store.all()), "active_runs": len(engine.active)})

    return mcp, store, engine


async def serve(config: Config) -> None:
    import uvicorn

    mcp, _store, engine = build(config)
    app = mcp.streamable_http_app(host=config.host)
    if config.auth_mode == "token":
        if not config.static_token:
            raise SystemExit("SCRIPTHOST_STATIC_TOKEN is required when SCRIPTHOST_AUTH_MODE=token")
        app = StaticTokenMiddleware(app, config.static_token)
    elif config.auth_mode == "none":
        log.warning("AUTH DISABLED - anyone who can reach this port can run code on this machine")
    elif config.auth_mode != "oidc":
        raise SystemExit(f"unknown SCRIPTHOST_AUTH_MODE {config.auth_mode!r}")

    server = uvicorn.Server(uvicorn.Config(EngineLifespan(app, engine), host=config.host, port=config.port,
                                           log_level="info", proxy_headers=True, forwarded_allow_ips="*"))
    await server.serve()


class EngineLifespan:
    """Start/stop the engine inside the ASGI lifespan, so supervised processes are stopped on SIGTERM
    before uvicorn exits (uvicorn re-raises the signal after shutdown, skipping code after serve())."""

    def __init__(self, app, engine: Engine):
        self.app, self.engine = app, engine

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            return await self.app(scope, receive, send)

        async def wrapped_receive():
            message = await receive()
            if message["type"] == "lifespan.startup":
                await self.engine.startup()
            elif message["type"] == "lifespan.shutdown":
                await self.engine.shutdown()
            return message

        await self.app(scope, wrapped_receive, send)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve(Config.from_env()))


if __name__ == "__main__":
    main()
