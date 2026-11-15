"""Persistent state: SQLite metadata plus a directory tree per project.

Layout under data_dir:
    scripthost.db
    projects/<name>/
        rev/<n>/        immutable source snapshot of revision n
        work/           persistent working directory (cwd of every run)
        venv/           virtualenv built from the current revision's requirements.txt
        logs/<run>.log  combined stdout+stderr of each run
        logs/install.log
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
RESTART_POLICIES = ("no", "on-failure", "always")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    entrypoint TEXT NOT NULL,
    args TEXT NOT NULL DEFAULT '[]',
    env TEXT NOT NULL DEFAULT '{}',
    secrets TEXT NOT NULL DEFAULT '[]',
    restart_policy TEXT NOT NULL DEFAULT 'no',
    cron TEXT,
    cron_timeout INTEGER,
    service_enabled INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    project TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at REAL NOT NULL,
    message TEXT,
    PRIMARY KEY (project, revision)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    revision INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    exit_code INTEGER,
    pid INTEGER,
    note TEXT
);
CREATE INDEX IF NOT EXISTS runs_project ON runs(project, id);
"""


class StoreError(ToolError):
    """User-facing error (bad input, missing project, ...); shown to the LLM verbatim."""


@dataclass
class Project:
    name: str
    description: str
    entrypoint: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)  # names of operator-provided secrets
    restart_policy: str = "no"
    cron: str | None = None
    cron_timeout: int | None = None
    service_enabled: bool = False
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Project":
        return cls(
            name=row["name"],
            description=row["description"],
            entrypoint=row["entrypoint"],
            args=json.loads(row["args"]),
            env=json.loads(row["env"]),
            secrets=json.loads(row["secrets"]),
            restart_policy=row["restart_policy"],
            cron=row["cron"],
            cron_timeout=row["cron_timeout"],
            service_enabled=bool(row["service_enabled"]),
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def public_dict(self) -> dict[str, Any]:
        """Representation handed to the LLM (secrets by name only)."""
        return {
            "name": self.name,
            "description": self.description,
            "entrypoint": self.entrypoint,
            "args": self.args,
            "env": self.env,
            "secrets": self.secrets,
            "restart_policy": self.restart_policy,
            "cron": self.cron,
            "cron_timeout": self.cron_timeout,
            "service_enabled": self.service_enabled,
            "revision": self.revision,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def safe_relpath(path: str) -> PurePosixPath:
    """Validate a project-relative path (no absolute paths, no '..')."""
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or not p.parts or any(part in ("..", "") for part in p.parts):
        raise StoreError(f"invalid path {path!r}: must be relative and must not contain '..'")
    if p == PurePosixPath("."):
        return p
    return PurePosixPath(*[part for part in p.parts if part != "."])


def resolve_inside(root: Path, rel: str) -> Path:
    if rel in ("", "."):
        return root
    target = (root / safe_relpath(rel)).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise StoreError(f"path {rel!r} escapes the project directory")
    return target


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.projects_dir = data_dir / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(data_dir / "scripthost.db", check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        columns = {r["name"] for r in self._db.execute("PRAGMA table_info(projects)")}
        if "secrets" not in columns:  # databases created by v0.1
            self._db.execute("ALTER TABLE projects ADD COLUMN secrets TEXT NOT NULL DEFAULT '[]'")

    # ---------------------------------------------------------------- paths
    def project_dir(self, name: str) -> Path:
        return self.projects_dir / name

    def rev_dir(self, name: str, revision: int) -> Path:
        return self.project_dir(name) / "rev" / str(revision)

    def work_dir(self, name: str) -> Path:
        return self.project_dir(name) / "work"

    def venv_dir(self, name: str) -> Path:
        return self.project_dir(name) / "venv"

    def log_dir(self, name: str) -> Path:
        return self.project_dir(name) / "logs"

    def run_log(self, name: str, run_id: int) -> Path:
        return self.log_dir(name) / f"{run_id}.log"

    # ---------------------------------------------------------------- sql helpers
    def _q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _x(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    # ---------------------------------------------------------------- projects
    def get(self, name: str) -> Project:
        rows = self._q("SELECT * FROM projects WHERE name=?", (name,))
        if not rows:
            raise StoreError(f"project {name!r} does not exist")
        return Project.from_row(rows[0])

    def exists(self, name: str) -> bool:
        return bool(self._q("SELECT 1 FROM projects WHERE name=?", (name,)))

    def all(self) -> list[Project]:
        return [Project.from_row(r) for r in self._q("SELECT * FROM projects ORDER BY name")]

    def create(self, project: Project, files: dict[str, str], message: str | None) -> Project:
        if not NAME_RE.match(project.name):
            raise StoreError("name must match [a-z0-9][a-z0-9_-]{0,62}")
        if self.exists(project.name):
            raise StoreError(f"project {project.name!r} already exists; use update_project")
        validate_policy(project.restart_policy)
        now = time.time()
        project.created_at = project.updated_at = now
        project.revision = 1
        rev = self.rev_dir(project.name, 1)
        try:
            _write_files(rev, files, base=None, delete=[])
            self._check_entrypoint(rev, project.entrypoint)
            self.work_dir(project.name).mkdir(parents=True, exist_ok=True)
            self.log_dir(project.name).mkdir(parents=True, exist_ok=True)
        except Exception:
            shutil.rmtree(self.project_dir(project.name), ignore_errors=True)
            raise
        self._save(project, insert=True)
        self._x(
            "INSERT INTO revisions(project, revision, created_at, message) VALUES (?,?,?,?)",
            (project.name, 1, now, message or "initial version"),
        )
        return project

    def new_revision(
        self,
        name: str,
        files: dict[str, str],
        delete: list[str],
        replace_all: bool,
        message: str | None,
        base_revision: int | None = None,
    ) -> int:
        project = self.get(name)
        base = self.rev_dir(name, base_revision or project.revision)
        if not base.exists():
            raise StoreError(f"revision {base_revision} of {name!r} does not exist")
        new_rev = project.revision + 1
        target = self.rev_dir(name, new_rev)
        if target.exists():
            shutil.rmtree(target)
        try:
            _write_files(target, files, base=None if replace_all else base, delete=delete)
            self._check_entrypoint(target, project.entrypoint)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        self._x(
            "INSERT INTO revisions(project, revision, created_at, message) VALUES (?,?,?,?)",
            (name, new_rev, time.time(), message),
        )
        project.revision = new_rev
        self.save(project)
        return new_rev

    def _check_entrypoint(self, rev: Path, entrypoint: str) -> None:
        if entrypoint.endswith(".py") and not resolve_inside(rev, entrypoint).is_file():
            raise StoreError(f"entrypoint {entrypoint!r} is not one of the project's files")

    def save(self, project: Project) -> None:
        validate_policy(project.restart_policy)
        project.updated_at = time.time()
        self._save(project, insert=False)

    def _save(self, p: Project, insert: bool) -> None:
        values = (
            p.description, p.entrypoint, json.dumps(p.args), json.dumps(p.env), json.dumps(p.secrets),
            p.restart_policy, p.cron, p.cron_timeout, int(p.service_enabled), p.revision,
            p.created_at, p.updated_at, p.name,
        )
        if insert:
            self._x(
                "INSERT INTO projects(description, entrypoint, args, env, secrets, restart_policy, cron,"
                " cron_timeout, service_enabled, revision, created_at, updated_at, name)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
        else:
            self._x(
                "UPDATE projects SET description=?, entrypoint=?, args=?, env=?, secrets=?, restart_policy=?,"
                " cron=?, cron_timeout=?, service_enabled=?, revision=?, created_at=?, updated_at=? WHERE name=?",
                values,
            )

    def delete(self, name: str) -> None:
        self.get(name)
        self._x("DELETE FROM projects WHERE name=?", (name,))
        self._x("DELETE FROM revisions WHERE project=?", (name,))
        self._x("DELETE FROM runs WHERE project=?", (name,))
        shutil.rmtree(self.project_dir(name), ignore_errors=True)

    def revisions(self, name: str) -> list[dict[str, Any]]:
        self.get(name)
        rows = self._q("SELECT * FROM revisions WHERE project=? ORDER BY revision DESC", (name,))
        return [{"revision": r["revision"], "created_at": _iso(r["created_at"]), "message": r["message"]} for r in rows]

    def source_files(self, name: str, revision: int) -> list[Path]:
        rev = self.rev_dir(name, revision)
        if not rev.exists():
            raise StoreError(f"revision {revision} of {name!r} does not exist")
        return sorted(p for p in rev.rglob("*") if p.is_file())

    # ---------------------------------------------------------------- runs
    def run_start(self, name: str, revision: int, trigger: str, note: str | None = None) -> int:
        cur = self._x(
            "INSERT INTO runs(project, revision, trigger, status, started_at, note) VALUES (?,?,?,?,?,?)",
            (name, revision, trigger, "starting", time.time(), note),
        )
        return int(cur.lastrowid)

    def run_update(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def run_get(self, run_id: int) -> dict[str, Any]:
        rows = self._q("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            raise StoreError(f"run {run_id} does not exist")
        return _run_dict(rows[0])

    def runs(self, name: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM runs WHERE project=? ORDER BY id DESC LIMIT ?", (name, limit))
        return [_run_dict(r) for r in rows]

    def latest_run(self, name: str, trigger: str | None = None) -> dict[str, Any] | None:
        if trigger:
            rows = self._q("SELECT * FROM runs WHERE project=? AND trigger=? ORDER BY id DESC LIMIT 1", (name, trigger))
        else:
            rows = self._q("SELECT * FROM runs WHERE project=? ORDER BY id DESC LIMIT 1", (name,))
        return _run_dict(rows[0]) if rows else None

    def mark_orphaned_runs(self) -> int:
        cur = self._x(
            "UPDATE runs SET status='lost', ended_at=?, note='server restarted while running'"
            " WHERE status IN ('starting','running')",
            (time.time(),),
        )
        return cur.rowcount

    def prune_runs(self, name: str, keep: int) -> None:
        rows = self._q(
            "SELECT id FROM runs WHERE project=? AND status NOT IN ('starting','running') ORDER BY id DESC LIMIT -1 OFFSET ?",
            (name, keep),
        )
        for r in rows:
            self.run_log(name, r["id"]).unlink(missing_ok=True)
            self._x("DELETE FROM runs WHERE id=?", (r["id"],))


def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
    started, ended = row["started_at"], row["ended_at"]
    return {
        "run_id": row["id"],
        "project": row["project"],
        "revision": row["revision"],
        "trigger": row["trigger"],
        "status": row["status"],
        "exit_code": row["exit_code"],
        "started_at": _iso(started),
        "ended_at": _iso(ended),
        "duration_s": round((ended or time.time()) - started, 2),
        "note": row["note"],
    }


def validate_policy(policy: str) -> None:
    if policy not in RESTART_POLICIES:
        raise StoreError(f"restart_policy must be one of {RESTART_POLICIES}")


def _write_files(target: Path, files: dict[str, str], base: Path | None, delete: list[str]) -> None:
    if base is not None:
        shutil.copytree(base, target)
    else:
        target.mkdir(parents=True, exist_ok=False)
    for rel in delete:
        p = resolve_inside(target, rel)
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        else:
            raise StoreError(f"cannot delete {rel!r}: no such file")
    for rel, content in files.items():
        p = resolve_inside(target, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if not any(target.rglob("*")):
        raise StoreError("a project needs at least one file")
