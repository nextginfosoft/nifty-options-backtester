"""Judge the JSON from /api/health for the outside uptime check. Reads it on stdin; exit 0 = healthy, 1 = a problem.

    curl -fsS https://niftysell.nextginfosoft.com/api/health | python scripts/check_live_health.py
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List


def problems(h: Dict[str, Any]) -> List[str]:
    found: List[str] = []
    if h.get("status") != "ok" or h.get("state") != "ready":
        found.append(f"app state is {h.get('state')!r}, not 'ready'")
    if h.get("ledger_ok") is False:
        found.append("the ledger cannot be read")
    sch = h.get("scheduler") or {}
    if sch.get("stalled"):
        found.append(f"the auto trader loop has stalled (last tick {sch.get('auto_tick_age_s')} s ago)")
    if (h.get("backup") or {}).get("stale"):
        found.append("the daily ledger backup is overdue")
    if "healthy" not in h:
        found.append("the server is running an old version that does not report liveness")
    elif h["healthy"] is False and not found:
        found.append("the app reports itself unhealthy")
    return found


def main() -> int:
    try:
        health = json.load(sys.stdin)
    except ValueError:
        print("UNHEALTHY: /api/health did not return JSON")
        return 1
    found = problems(health if isinstance(health, dict) else {})
    if found:
        print("UNHEALTHY: " + "; ".join(found))
        return 1
    print("healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
