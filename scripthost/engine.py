"""Process execution: virtualenvs, runs, service supervision and cron scheduling."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from croniter import croniter

from .config import Config
from .secrets import Secrets
from .store import Project, Store, StoreError

log = logging.getLogger("scripthost.engine")

STOP_GRACE_SECONDS = 10
MIN_HEALTHY_SECONDS = 30  # a service run longer than this resets the restart backoff
MAX_BACKOFF_SECONDS = 300


@dataclass
class ActiveRun:
    run_id: int
    project: str
    trigger: str
    proc: asyncio.subprocess.Process
    started: float
    done: asyncio.Event = field(default_factory=asyncio.Event)
    stop_requested: bool = False
    timed_out: bool = False


@dataclass
class ServiceState:
    task: asyncio.Task | None = None
    sleeper: asyncio.Task | None = None
    stopping: bool = False
    restarts: int = 0
    last_exit: int | None = None
    backoff_until: float | None = None
    state: str = "stopped"  # running | backoff | stopped | exited | failed


class Engine:
    def __init__(self, store: Store, config: Config, secrets: Secrets):
        self.store = store
        self.config = config
        self.secrets = secrets
        self.active: dict[int, ActiveRun] = {}
        self.services: dict[str, ServiceState] = {}
        self._venv_locks: dict[str, asyncio.Lock] = {}
        self._cron_task: asyncio.Task | None = None
        self._next_cron: dict[str, float] = {}
        self.uv = shutil.which("uv")

    # ================================================================ lifecycle
    async def startup(self) -> None:
        lost = self.store.mark_orphaned_runs()
        if lost:
            log.warning("marked %d runs from a previous server process as lost", lost)
        for project in self.store.all():
            if project.service_enabled:
                await self.start_service(project.name)
        self._cron_task = asyncio.create_task(self._cron_loop(), name="cron")

    async def shutdown(self) -> None:
        if self._cron_task:
            self._cron_task.cancel()
        names = list(self.services)
        await asyncio.gather(*(self.stop_service(n, persist=False) for n in names), return_exceptions=True)
        await asyncio.gather(*(self._terminate(r) for r in list(self.active.values())), return_exceptions=True)

    # ================================================================ environments
    def _requirements_text(self, project: Project) -> str:
        req = self.store.rev_dir(project.name, project.revision) / "requirements.txt"
        return req.read_text(encoding="utf-8") if req.exists() else ""

    async def ensure_venv(self, project: Project) -> dict[str, Any]:
        """Build/refresh the project's venv if requirements.txt changed. Returns install info."""
        lock = self._venv_locks.setdefault(project.name, asyncio.Lock())
        async with lock:
            venv = self.store.venv_dir(project.name)
            reqs = self._requirements_text(project)
            digest = hashlib.sha256(f"{sys.version}\n{reqs}".encode()).hexdigest()
            marker = venv / ".scripthost-requirements.sha256"
            if marker.exists() and marker.read_text() == digest and (venv / "bin" / "python").exists():
                return {"changed": False, "ok": True}

            install_log = self.store.log_dir(project.name) / "install.log"
            install_log.parent.mkdir(parents=True, exist_ok=True)
            # Build in place: venv scripts embed absolute paths, so a build-then-rename would break them.
            tmp = venv
            shutil.rmtree(tmp, ignore_errors=True)
            if self.uv:
                cmds = [[self.uv, "venv", "--quiet", "--python", sys.executable, str(tmp)]]
                if reqs.strip():
                    cmds.append([self.uv, "pip", "install", "--python", str(tmp / "bin" / "python"),
                                 "-r", str(self.store.rev_dir(project.name, project.revision) / "requirements.txt")])
            else:
                cmds = [[sys.executable, "-m", "venv", str(tmp)]]
                if reqs.strip():
                    cmds.append([str(tmp / "bin" / "python"), "-m", "pip", "install", "--disable-pip-version-check",
                                 "-r", str(self.store.rev_dir(project.name, project.revision) / "requirements.txt")])
            output = []
            for cmd in cmds:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    cwd=self.store.project_dir(project.name),
                )
                out, _ = await proc.communicate()
                output.append(f"$ {' '.join(cmd)}\n{out.decode(errors='replace')}")
                if proc.returncode != 0:
                    install_log.write_text("\n".join(output))
                    shutil.rmtree(tmp, ignore_errors=True)
                    return {"changed": True, "ok": False, "output": _tail_text("\n".join(output), 4000)}
            install_log.write_text("\n".join(output))
            marker.write_text(digest)
            return {"changed": True, "ok": True}

    # ================================================================ running
    def _command(self, project: Project, args: list[str] | None) -> list[str]:
        python = str(self.store.venv_dir(project.name) / "bin" / "python")
        src = self.store.rev_dir(project.name, project.revision)
        argv = project.args if args is None else args
        if project.entrypoint.endswith(".py"):
            return [python, "-u", str(src / project.entrypoint), *argv]
        return [python, "-u", "-m", project.entrypoint, *argv]

    def _environment(self, project: Project, extra: dict[str, str] | None, workdir: Path) -> dict[str, str]:
        src = self.store.rev_dir(project.name, project.revision)
        env = {
            "PATH": f"{self.store.venv_dir(project.name) / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(workdir),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TZ": os.environ.get("TZ", ""),
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(src),
            "VIRTUAL_ENV": str(self.store.venv_dir(project.name)),
            "SCRIPTHOST_PROJECT": project.name,
            "SCRIPTHOST_REVISION": str(project.revision),
            "SCRIPTHOST_SRC_DIR": str(src),
            "SCRIPTHOST_WORK_DIR": str(workdir),
        }
        env.update(project.env)
        if extra:
            env.update(extra)
        env.update(self._secret_values(project))  # secrets win, so a run can't shadow them
        return {k: v for k, v in env.items() if v is not None}

    def _secret_values(self, project: Project) -> dict[str, str]:
        try:
            return self.secrets.resolve(project.secrets)
        except KeyError as exc:
            raise StoreError(
                f"secret(s) {exc.args[0]} not defined in {self.secrets.path}; "
                "ask the operator to add them or remove them from the project"
            ) from None

    async def launch(
        self,
        name: str,
        trigger: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        workdir: Path | None = None,
        timeout: float | None = None,
    ) -> ActiveRun:
        project = self.store.get(name)
        self._secret_values(project)
        venv = await self.ensure_venv(project)
        run_id = self.store.run_start(name, project.revision, trigger)
        log_path = self.store.run_log(name, run_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not venv["ok"]:
            log_path.write_text("dependency installation failed:\n" + venv["output"])
            self.store.run_update(run_id, status="failed", ended_at=time.time(), note="dependency installation failed")
            raise StoreError(f"dependency installation failed (run {run_id}):\n{venv['output']}")

        workdir = workdir or self.store.work_dir(name)
        workdir.mkdir(parents=True, exist_ok=True)
        cmd = self._command(project, args)
        with open(log_path, "ab", buffering=0) as fh:
            header = f"=== run {run_id} | {trigger} | revision {project.revision} | {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            fh.write(header.encode())
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=workdir, env=self._environment(project, env, workdir),
                    stdin=asyncio.subprocess.DEVNULL, stdout=fh, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                fh.write(f"failed to start: {exc}\n".encode())
                self.store.run_update(run_id, status="failed", ended_at=time.time(), note=str(exc))
                raise StoreError(f"failed to start: {exc}") from exc
        self.store.run_update(run_id, status="running", pid=proc.pid)
        run = ActiveRun(run_id=run_id, project=name, trigger=trigger, proc=proc, started=time.time())
        self.active[run_id] = run
        asyncio.create_task(self._watch(run, timeout), name=f"watch-{run_id}")
        return run

    async def _watch(self, run: ActiveRun, timeout: float | None) -> None:
        try:
            if timeout:
                try:
                    await asyncio.wait_for(run.proc.wait(), timeout)
                except asyncio.TimeoutError:
                    run.timed_out = True
                    await self._terminate(run)
            code = await run.proc.wait()
        finally:
            code = run.proc.returncode
            if run.timed_out:
                status = "timeout"
            elif run.stop_requested:
                status = "stopped"
            else:
                status = "succeeded" if code == 0 else "failed"
            footer = f"=== run {run.run_id} ended: {status}, exit code {code}\n"
            with contextlib.suppress(OSError):
                with open(self.store.run_log(run.project, run.run_id), "a") as fh:
                    fh.write(footer)
            self.store.run_update(run.run_id, status=status, exit_code=code, ended_at=time.time())
            self.active.pop(run.run_id, None)
            run.done.set()
            with contextlib.suppress(Exception):
                self.store.prune_runs(run.project, self.config.log_retention_runs)

    async def _terminate(self, run: ActiveRun) -> None:
        if run.proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(run.proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(run.proc.wait(), STOP_GRACE_SECONDS)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(run.proc.pid, signal.SIGKILL)
            await run.proc.wait()

    async def stop_run(self, run_id: int) -> bool:
        run = self.active.get(run_id)
        if not run:
            return False
        run.stop_requested = True
        await self._terminate(run)
        await run.done.wait()
        return True

    def active_runs(self, name: str) -> list[ActiveRun]:
        return [r for r in self.active.values() if r.project == name]

    # ================================================================ services
    async def start_service(self, name: str) -> None:
        project = self.store.get(name)
        state = self.services.get(name)
        if state and state.task and not state.task.done():
            return
        if not project.service_enabled:
            project.service_enabled = True
            self.store.save(project)
        state = ServiceState()
        self.services[name] = state
        state.task = asyncio.create_task(self._supervise(name, state), name=f"service-{name}")

    async def stop_service(self, name: str, persist: bool = True) -> bool:
        if persist:
            project = self.store.get(name)
            if project.service_enabled:
                project.service_enabled = False
                self.store.save(project)
        state = self.services.get(name)
        if not state or not state.task or state.task.done():
            return False
        state.stopping = True
        # Cooperative stop: never cancel the supervisor mid-launch (that could orphan a process).
        for run in self.active_runs(name):
            if run.trigger == "service":
                run.stop_requested = True
                await self._terminate(run)
        if state.sleeper and not state.sleeper.done():
            state.sleeper.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await state.task
        state.state = "stopped"
        return True

    async def restart_service(self, name: str) -> None:
        await self.stop_service(name, persist=False)
        await self.start_service(name)

    async def _supervise(self, name: str, state: ServiceState) -> None:
        backoff = 1.0
        while not state.stopping:
            try:
                run = await self.launch(name, "service")
            except StoreError as exc:
                log.error("service %s failed to launch: %s", name, exc)
                state.state = "failed"
                code = None
                runtime = 0.0
            else:
                if state.stopping:  # stop requested while we were launching
                    run.stop_requested = True
                    await self._terminate(run)
                    await run.done.wait()
                    break
                state.state = "running"
                await run.done.wait()
                code = run.proc.returncode
                runtime = time.time() - run.started
            if state.stopping:
                break
            state.last_exit = code
            policy = self.store.get(name).restart_policy
            should_restart = policy == "always" or (policy == "on-failure" and code != 0)
            if not should_restart:
                state.state = "exited" if code == 0 else "failed"
                break
            backoff = 1.0 if runtime >= MIN_HEALTHY_SECONDS else min(backoff * 2, MAX_BACKOFF_SECONDS)
            state.restarts += 1
            state.state = "backoff"
            state.backoff_until = time.time() + backoff
            state.sleeper = asyncio.create_task(asyncio.sleep(backoff))
            with contextlib.suppress(asyncio.CancelledError):
                await state.sleeper
            state.backoff_until = None

    def service_status(self, name: str) -> dict[str, Any]:
        state = self.services.get(name)
        if not state:
            return {"state": "stopped"}
        out: dict[str, Any] = {"state": state.state, "restarts": state.restarts, "last_exit_code": state.last_exit}
        if state.backoff_until:
            out["restarting_in_s"] = round(state.backoff_until - time.time(), 1)
        current = [r for r in self.active_runs(name) if r.trigger == "service"]
        if current:
            out["run_id"] = current[0].run_id
            out["pid"] = current[0].proc.pid
            out["uptime_s"] = round(time.time() - current[0].started, 1)
        return out

    # ================================================================ cron
    @staticmethod
    def validate_cron(expr: str) -> None:
        if not croniter.is_valid(expr):
            raise StoreError(f"invalid cron expression {expr!r} (expected 5 fields, e.g. '*/15 * * * *')")

    def next_cron_time(self, name: str) -> float | None:
        return self._next_cron.get(name)

    def reschedule(self, project: Project) -> None:
        if project.cron:
            self._next_cron[project.name] = croniter(project.cron, time.time()).get_next(float)
        else:
            self._next_cron.pop(project.name, None)

    async def _cron_loop(self) -> None:
        for project in self.store.all():
            self.reschedule(project)
        while True:
            await asyncio.sleep(1)
            now = time.time()
            for name, due in list(self._next_cron.items()):
                if due > now:
                    continue
                try:
                    project = self.store.get(name)
                except StoreError:
                    self._next_cron.pop(name, None)
                    continue
                self.reschedule(project)
                if any(r.trigger in ("cron", "service") for r in self.active_runs(name)):
                    log.info("cron run of %s skipped: previous run still active", name)
                    continue
                try:
                    await self.launch(name, "cron", timeout=project.cron_timeout)
                except Exception as exc:  # noqa: BLE001
                    log.error("cron run of %s failed to start: %s", name, exc)


def _tail_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]
