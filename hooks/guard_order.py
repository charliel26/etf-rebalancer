#!/usr/bin/env python3
"""PreToolUse hook for every mcp__robinhood-trading__* tool (STRATEGY.md §11).

Deny-only and fail-closed: a refusal is printed as a JSON deny decision and the
process exits with code 2 (the only exit code Claude Code treats as blocking).
Any error, including a failed import, is also a refusal. Allowed calls exit 0
without a decision, so normal permission rules (e.g. "ask") still apply.
Registered with "onFailure": "block" so a crash or timeout also blocks.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _deny(reason: str) -> int:
    body = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"rebalancer guard: {reason}",
        }
    }
    print(json.dumps(body))
    print(f"rebalancer guard: {reason}", file=sys.stderr)
    return 2


def main() -> int:
    try:
        project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[1])
        sys.path.insert(0, str(project_dir / "src"))
        from rebalancer.guard import hook_output, pre_tool_use

        payload = json.load(sys.stdin)
        code, out = hook_output(pre_tool_use(payload, project_dir, os.environ))
        if out:
            print(out)
            print(json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"], file=sys.stderr)
        return code
    except BaseException as exc:
        return _deny(f"guard error, failing closed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
