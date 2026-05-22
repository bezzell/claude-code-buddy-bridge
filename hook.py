"""
Claude Code PreToolUse hook -> Claude Desktop Buddy bridge.

Wired up in ~/.claude/settings.json under hooks.PreToolUse (see the
README at the bottom of this folder). For each invocation:

  1. Read tool_name + tool_input from stdin (Claude Code hook format).
  2. Write a prompt sidecar that the bridge picks up and ships to the
     stick over BLE.
  3. Block until the stick sends back a decision (or we time out).
  4. Emit Claude Code's permissionDecision JSON so the tool either runs
     or is blocked without a second prompt in the terminal.

If the bridge isn't running, exit 0 immediately so Claude Code falls
back to its normal permission flow — the stick is opt-in, not a hard
dependency.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path


def _ipc_base() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Caches")
    else:
        base = (
            os.environ.get("XDG_CACHE_HOME")
            or os.environ.get("XDG_RUNTIME_DIR")
            or str(Path.home() / ".cache")
        )
    return Path(base) / "claude-buddy-bridge"


IPC_DIR = _ipc_base()
PROMPTS_DIR = IPC_DIR / "prompts"
DECISIONS_DIR = IPC_DIR / "decisions"
ALIVE_FILE = IPC_DIR / "bridge-alive"

ALIVE_MAX_AGE = 15.0     # bridge touches every tick (~1s); 15s is plenty of slack
WAIT_TIMEOUT = 60.0      # how long to block before falling through to normal flow
POLL_INTERVAL = 0.1


def _bridge_alive() -> bool:
    try:
        return (time.time() - ALIVE_FILE.stat().st_mtime) <= ALIVE_MAX_AGE
    except OSError:
        return False


def _read_input() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def _hint_for(tool: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    # Pick the most useful single field per tool. Truncated by the bridge.
    for key in ("command", "file_path", "path", "pattern", "url"):
        v = inp.get(key)
        if v:
            return str(v).splitlines()[0]
    return ""


def _emit(decision: str, reason: str):
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))
    sys.stdout.flush()


def main():
    hook_input = _read_input()
    tool = hook_input.get("tool_name") or "tool"
    tool_input = hook_input.get("tool_input") or {}

    # Auto-detect "no prompt needed" modes. Claude Code passes the active
    # permission_mode in every hook invocation; if it's a mode where Claude
    # itself wouldn't ask the user, there's nothing to forward to the stick.
    #   bypassPermissions: --dangerously-skip-permissions or `/permissions bypass`
    #   plan: read-only planning, no tools should run
    # `acceptEdits` deliberately falls through — it auto-approves file
    # writes/edits but Bash still requires confirmation, so for our Bash
    # matcher the stick should still get involved.
    permission_mode = hook_input.get("permission_mode")
    if permission_mode in ("bypassPermissions", "plan"):
        sys.exit(0)

    # Manual bypass paths for cases the permission_mode check doesn't cover
    # (per-tool allowlists, settings.json rules, etc.).
    if os.environ.get("CLAUDE_BUDDY_AUTO"):
        # Per-shell: set CLAUDE_BUDDY_AUTO=1 before running claude.
        sys.exit(0)
    if (IPC_DIR / "mute").exists():
        # Global: touch <IPC_DIR>/mute to silence; delete to re-enable.
        sys.exit(0)

    if not _bridge_alive():
        # Bridge not running — let Claude Code handle this normally.
        sys.exit(0)

    prompt_id = uuid.uuid4().hex[:16]
    payload = {
        "id": prompt_id,
        "tool": tool,
        "hint": _hint_for(tool, tool_input),
        "created_at": time.time(),
    }
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (PROMPTS_DIR / f"{prompt_id}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    except OSError as e:
        print(f"[hook] could not write prompt: {e}", file=sys.stderr)
        sys.exit(0)

    deadline = time.time() + WAIT_TIMEOUT
    decision_file = DECISIONS_DIR / f"{prompt_id}.json"
    while time.time() < deadline:
        if decision_file.exists():
            try:
                obj = json.loads(decision_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                obj = {}
            try: decision_file.unlink()
            except OSError: pass
            d = obj.get("decision", "")
            if d in ("once", "always", "allow", "approve"):
                _emit("allow", f"approved on Claude buddy stick ({tool})")
                return
            if d in ("deny", "no", "block"):
                _emit("deny", f"denied on Claude buddy stick ({tool})")
                return
            # Unknown decision string — fall back to Claude Code's own flow.
            sys.exit(0)
        time.sleep(POLL_INTERVAL)

    # Timed out waiting on the stick — give up on this one and let
    # Claude Code handle it normally. Clean up the prompt file.
    try: (PROMPTS_DIR / f"{prompt_id}.json").unlink()
    except OSError: pass
    print(f"[hook] no response from stick after {WAIT_TIMEOUT:.0f}s; falling through", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
