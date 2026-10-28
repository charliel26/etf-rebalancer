"""Armed manifest: SHA-256 of every file that decides or guards an order.

`rebalancer arm` (interactive terminal only) records the hashes in
state/armed.json. The guard refuses every order while any protected file
differs from what was armed, so a change to code, config, hooks, settings,
the agent's procedure or the locked dependencies needs a human to re-arm
before it can trade. This makes tampering visible; it cannot stop a process
with your shell rights from rewriting files (and a rewritten guard would not
run this check), which is why .claude/settings.json also denies Claude's
file tools on these paths.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

PROTECTED_GLOBS = (
    "config/*.yaml",
    "hooks/*.py",
    "src/rebalancer/**/*.py",
    ".claude/settings.json",
    ".claude/commands/*.md",
    "pyproject.toml",
    "uv.lock",
)
ARMED_FILE = "armed.json"


def compute_manifest(project_dir: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for pattern in PROTECTED_GLOBS:
        for path in sorted(project_dir.glob(pattern)):
            if path.is_file():
                rel = path.relative_to(project_dir).as_posix()
                manifest[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def write_armed(project_dir: Path, state_dir: Path, now: datetime) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / ARMED_FILE
    document = {"armed_at": now.isoformat(), "files": compute_manifest(project_dir)}
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return target


def verify_armed(project_dir: Path, state_dir: Path) -> list[str]:
    """Problems that should stop trading; empty when everything matches."""
    target = state_dir / ARMED_FILE
    if not target.exists():
        return ["not armed: run `rebalancer arm` in a terminal"]
    try:
        armed = json.loads(target.read_text())["files"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"armed manifest unreadable: {exc}"]
    current = compute_manifest(project_dir)
    problems = [f"changed since arming: {p}" for p in sorted(current) if p in armed and armed[p] != current[p]]
    problems += [f"new since arming: {p}" for p in sorted(set(current) - set(armed))]
    problems += [f"missing since arming: {p}" for p in sorted(set(armed) - set(current))]
    return problems
