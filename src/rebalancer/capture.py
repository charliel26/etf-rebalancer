"""PostToolUse capture of Robinhood MCP responses (STRATEGY.md §10.2).

Every robinhood-trading tool response is written verbatim to
state/raw/<New York date>/, so the snapshot is built by code from what the
broker returned, never from numbers the model typed. Order placements also
update the ledger line the guard claimed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .broker.mcp_payload import decode, find_order_id
from .ledger import Ledger

PREFIX = "mcp__robinhood-trading__"
NEW_YORK = ZoneInfo("America/New_York")


def raw_dir_for(state_dir: Path, now: datetime) -> Path:
    return state_dir / "raw" / now.astimezone(NEW_YORK).date().isoformat()


def capture(payload: Mapping[str, Any], state_dir: Path, now: datetime) -> Path | None:
    tool_name = str(payload.get("tool_name", ""))
    if not tool_name.startswith(PREFIX):
        return None
    short = tool_name[len(PREFIX) :]
    event = str(payload.get("hook_event_name", ""))
    tool_use_id = str(payload.get("tool_use_id") or "unknown")
    out_dir = raw_dir_for(state_dir, now)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", tool_use_id)[-24:] or "unknown"
    path = out_dir / f"{now.astimezone(NEW_YORK):%H%M%S%f}_{short}_{safe_id}.json"
    record = {
        "captured_at": now.isoformat(),
        "event": event,
        "tool": short,
        "tool_use_id": tool_use_id,
        "tool_input": payload.get("tool_input"),
        "tool_response": payload.get("tool_response"),
        "error": payload.get("error"),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True, default=str))
    tmp.replace(path)

    if short == "place_equity_order":
        with Ledger(state_dir / "ledger.sqlite3") as ledger:
            if event == "PostToolUse":
                order_id = find_order_id(decode(payload.get("tool_response")))
                ledger.mark_submitted(tool_use_id, order_id, now)
            else:
                ledger.mark_submit_failed(tool_use_id, str(payload.get("error")), now)
    return path
