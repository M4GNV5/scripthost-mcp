"""Operator-provided secrets from a dotenv file.

Secrets are defined by the operator in a .env-style file (default /data/secrets.env); the LLM
can only list their *names* and attach them to projects. Values are injected into a run's
environment and masked in anything the tools return.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

log = logging.getLogger("scripthost.secrets")

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
MIN_MASK_LENGTH = 4  # shorter values would mangle unrelated output


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = LINE_RE.match(line)
        if not m:
            log.warning("secrets file line %d ignored (expected KEY=value)", lineno)
            continue
        key, raw = m.groups()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            value = raw[1:-1]
            if raw[0] == '"':
                value = value.replace("\\n", "\n").replace('\\"', '"')
        else:
            value = re.sub(r"\s+#.*$", "", raw)  # inline comment after unquoted value
        values[key] = value
    return values


class Secrets:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._values: dict[str, str] = {}
        self._mask_re: re.Pattern | None = None

    def _load(self) -> dict[str, str]:
        """(Re)read the file if it changed; edits take effect on the next run without a restart."""
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                self._mtime, self._values, self._mask_re = None, {}, None
                return self._values
            if mtime != self._mtime:
                self._values = parse_dotenv(self.path.read_text(encoding="utf-8"))
                self._mtime = mtime
                maskable = sorted({v for v in self._values.values() if len(v) >= MIN_MASK_LENGTH}, key=len, reverse=True)
                self._mask_re = re.compile("|".join(map(re.escape, maskable))) if maskable else None
                self._names_by_value = {v: k for k, v in self._values.items()}
                log.info("loaded %d secrets from %s", len(self._values), self.path)
            return self._values

    def names(self) -> list[str]:
        return sorted(self._load())

    def resolve(self, names: list[str]) -> dict[str, str]:
        values = self._load()
        missing = [n for n in names if n not in values]
        if missing:
            raise KeyError(", ".join(missing))
        return {n: values[n] for n in names}

    def mask(self, text: str) -> str:
        self._load()
        if not self._mask_re or not text:
            return text
        return self._mask_re.sub(lambda m: f"***{self._names_by_value.get(m.group(0), 'SECRET')}***", text)
