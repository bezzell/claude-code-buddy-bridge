# claude-code-buddy-bridge

A host-side bridge that lets the [Claude Desktop Buddy](https://github.com/anthropics/claude-desktop-buddy) M5Stick react to **Claude Code CLI** sessions — Warp, native PowerShell, plain Terminal, anywhere — not just the Claude desktop app.

If you live in the CLI like most engineers do, the buddy stick is dark all day. This bridge lights it back up.

## What you get

- Live token counters from your CLI Claude Code sessions on the stick (`fed` bar, level-ups every 50K tokens)
- The "just finished a turn" celebrate animation when a Claude response wraps
- Real-time idle / busy state based on whether sessions are actively generating
- **Permission prompts on the stick** — approve or deny Bash (or any matcher you pick) from the device, with the buddy's full attention/heart/LED/beep flow

## How it works

```
Claude Code CLI                       Bridge (this repo)            M5Stick
─────────────────                     ──────────────────            ───────
writes JSONL transcripts  ─────────►  tails ~/.claude/projects/
in ~/.claude/projects/                │
                                      │  every 1s
                                      │  builds the heartbeat
                                      │  documented in upstream
                                      │  REFERENCE.md
                                      ▼
                                      Nordic UART BLE  ───────►  pet animates
                                                       ◄───────  A/B button decision
                                      writes decision
                                      back to hook
                                      ▲
PreToolUse hook  ─────────────────────┘
fires on each Bash call,
blocks until decision
```

The wire protocol is the one defined in the upstream firmware's [REFERENCE.md](https://github.com/anthropics/claude-desktop-buddy/blob/main/REFERENCE.md). This bridge just speaks it on behalf of CLI sessions.

## Requirements

- Python 3.10+
- A paired M5StickC Plus / M5StickC Plus 2 / M5StickS3 running the upstream firmware
- A host with BLE: Windows 11, macOS, or Linux with BlueZ
- Claude Code CLI 2.x (the hook protocol used here is the 2.x `hookSpecificOutput.permissionDecision` format)

## Install

```bash
git clone https://github.com/<you>/claude-code-buddy-bridge.git
cd claude-code-buddy-bridge
python3 -m pip install -r requirements.txt
```

## Pair the stick (one-time)

The firmware requires bonded BLE Secure Connections (it advertises `Claude-XXXX` where `XXXX` is the last two bytes of its BT MAC). The OS, not Python, has to do the passkey dance.

### Windows

1. **Disconnect the Claude desktop app** from the stick if it was previously paired with it (Developer → Hardware Buddy → Disconnect). The stick only accepts one BLE central at a time.
2. Open Settings → **Bluetooth & devices** → **Add device** → **Bluetooth**.
3. Pick the `Claude-XXXX` entry that shows up.
4. The stick's OLED will display a **6-digit passkey** and beep. Type it into the Windows dialog.
5. Wait for "Your device is ready to go."

### macOS

`bleak`'s `pair()` works natively on Core Bluetooth — the bridge will trigger the system prompt automatically the first time. Enter the passkey from the stick's OLED.

### Linux

Use `bluetoothctl` to pair before running the bridge:

```bash
bluetoothctl
> scan on
> pair <MAC>      # 6-digit passkey will appear on the stick
> trust <MAC>
> exit
```

## Run

```bash
python3 bridge.py
```

You should see `[ble] connected to Claude-XXXX` followed by `[tx]` heartbeat lines. The stick wakes from sleep state and starts showing live data.

### Add a shell alias

Drop the repo dir on your `PATH` and use the included shim:

- **Windows:** the repo includes `claudebuddy.cmd`. Add the repo dir to user `PATH` via Settings → Edit environment variables, then run `claudebuddy` from any shell.
- **macOS/Linux:** the `claudebuddy` script is on the repo root. `chmod +x claudebuddy && ln -s "$PWD/claudebuddy" ~/.local/bin/claudebuddy`.

## Hook up permission prompts (optional but the fun part)

The bridge can drive the buddy's full attention / approve-or-deny flow for CLI tool calls, via Claude Code's PreToolUse hook.

Add to your `~/.claude/settings.json` (`%USERPROFILE%\.claude\settings.json` on Windows):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /absolute/path/to/repo/hook.py"
          }
        ]
      }
    ]
  }
}
```

Windows users: use `py` instead of `python3` and double-escape backslashes in the path.

The `matcher` is regex over tool names — expand to `"Bash|Write|Edit"` etc. as you like. The hook checks if the bridge is currently running and only intercepts if so; if the bridge is down, your CLI permission flow stays exactly as it was.

Restart any open Claude Code sessions so they reload the hook config.

### Bypassing the stick (auto mode)

The hook automatically detects Claude Code's active `permission_mode` and skips the stick when Claude itself wouldn't have prompted you:

- `bypassPermissions` (i.e. `--dangerously-skip-permissions` or `/permissions bypass`) → skip
- `plan` (planning mode, no tools execute) → skip
- `default` and `acceptEdits` → prompt the stick as normal (because Bash still requires confirmation in `acceptEdits` — only file writes get auto-approved)

So if you just run `claude --dangerously-skip-permissions`, the stick will stay quiet without any additional configuration.

For cases the automatic detection doesn't cover (per-tool allowlists in `settings.json`, `/permissions` slash command additions, etc.), two manual opt-outs:

- **Per-shell:** set `CLAUDE_BUDDY_AUTO=1` in the terminal before running `claude`. Other terminals still use the stick.
  ```powershell
  $env:CLAUDE_BUDDY_AUTO = "1"   # PowerShell
  export CLAUDE_BUDDY_AUTO=1     # bash/zsh
  ```
- **Global:** create a `mute` file in the IPC dir. Delete it to re-enable.
  ```powershell
  ni "$env:LOCALAPPDATA\claude-buddy-bridge\mute" -Force   # Windows
  touch ~/.cache/claude-buddy-bridge/mute                  # Linux
  touch ~/Library/Caches/claude-buddy-bridge/mute          # macOS
  ```

When either is set, the hook exits immediately and Claude Code's normal permission flow proceeds.

### What you'll see

When Claude wants to run Bash:

1. Stick enters **attention** state — LED pulses, 1200 Hz beep, screen shows the tool name + a hint of the command
2. Press **A** to approve, **B** to deny
3. Approve in under 5s → **heart** animation + 2400 Hz tone
4. Deny → 600 Hz tone, Claude Code blocks the call with the reason "denied on Claude buddy stick"

## Configuration

Tunables at the top of `bridge.py`:

| Constant | Default | What it controls |
|---|---|---|
| `TICK_SECONDS` | `1.0` | how often we scan JSONL files for changes |
| `KEEPALIVE_SECONDS` | `10.0` | minimum send interval; matches the firmware's 30s liveness window |
| `RUNNING_WINDOW` | `5.0` | mtime freshness threshold for "session is generating right now" |
| `OPEN_WINDOW` | `3600.0` | mtime threshold for "session counts as recently open" |

`hook.py`:

| Constant | Default | What it controls |
|---|---|---|
| `WAIT_TIMEOUT` | `60.0` | how long the hook blocks waiting on the stick before falling back to normal CLI permission flow |
| `ALIVE_MAX_AGE` | `15.0` | how stale `bridge-alive` can be before the hook treats the bridge as down |

## Troubleshooting

### "The parameter is incorrect" (`WinError -2147024809`) on first write

WinRT quirk. The firmware marks its GATT characteristics `ESP_GATT_PERM_*_ENCRYPTED`, so writes need an encrypted ATT bearer — but `write_gatt_char(..., response=False)` doesn't always promote the link on Windows. The bridge works around this by:

1. Calling `start_notify` on the TX characteristic *before* the first write. The CCCD descriptor is also encrypted, so subscribing forces the bearer to encrypt.
2. Using `response=True` (Write Request, not Write Command) for the data writes.

Both are already in `bridge.py`. If you still see this error, confirm the stick is paired through OS Settings (not just connected) — see the [Pair the stick](#pair-the-stick-one-time) section.

### `pair() returned None` and writes still fail

On Windows, `BleakClient.pair()` only works for devices already in the OS device list. If yours isn't there, follow the OS pairing dance first; `pair()` is a belt-and-suspenders call after that.

### Stick stays at `P_IDLE` even when Claude is generating

The firmware's `P_BUSY` threshold is `running >= 3` simultaneous sessions. With a single Warp tab going, you'll mostly see idle ↔ celebrate cycles, not busy. That's by firmware design, not a bug.

### Bridge tokens count goes up but `lvl` / `fed` doesn't change

Make sure you're on the version that sends `tokens` as the **lifetime cumulative**, not a delta. Older drafts subtracted a baseline on every restart, which broke level progression. The firmware first-sight-latches the initial value (`stats.h:84`), so sending lifetime tokens is safe — deltas drive lvl/fed on every heartbeat.

### Mood doesn't change

Mood is computed locally on the stick from **approval response times** (`stats.h:142-158`), not from heartbeat fields. You only feed it data by responding to permission prompts on the stick — so it sits at the default tier until you install the PreToolUse hook and start answering.

### Stick goes to sleep when I kill the bridge

That's correct. The firmware's `dataConnected()` window is 30s — no heartbeat for that long and it switches to `P_SLEEP`. Restart the bridge to wake it.

## What this bridge does **not** do

- **Energy** is internal to the firmware (time-based drain, refills on face-down nap). Not in the heartbeat schema.
- Permission prompts from the **Claude desktop app** itself. Run the desktop app's own Hardware Buddy if you want that.
- It only reads JSONL transcripts. If something never makes it to JSONL (e.g. a session that crashed before its first assistant message), it won't show up.

## License

MIT. See [LICENSE](./LICENSE).

The upstream firmware and the wire protocol it speaks are Anthropic's work — see [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy) and its `REFERENCE.md`.

## Acknowledgements

The clever bits (the buddy itself, the protocol, the seven persona states, the deeply cursed factory of ASCII pets) are all upstream. This repo is just plumbing.
