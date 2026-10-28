#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure hook for mcp__robinhood-trading__* tools.

Saves each response verbatim under state/raw/<date>/ and records order
submissions in the ledger. It never blocks (the call has already happened);
errors are appended to state/hook_errors.log.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[1])
    state_dir = project_dir / "state"
    try:
        sys.path.insert(0, str(project_dir / "src"))
        from rebalancer.capture import capture

        capture(json.load(sys.stdin), state_dir, datetime.now(UTC))
    except BaseException:  # noqa: BLE001 - never block, but leave a trace
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            with (state_dir / "hook_errors.log").open("a") as log:
                log.write(f"{datetime.now(UTC).isoformat()} capture_mcp\n{traceback.format_exc()}\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
