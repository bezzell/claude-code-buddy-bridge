"""
Claude Desktop Buddy bridge for the Claude Code CLI (Warp/PowerShell/anywhere).

Tails ~/.claude/projects/**/*.jsonl, aggregates output-token usage and
session activity, and pushes the documented heartbeat JSON over Nordic
UART BLE to an M5Stick running the buddy firmware.

Schema and UUIDs come straight from REFERENCE.md in the firmware repo.

Run:
    py -m pip install -r requirements.txt
    py bridge.py

Make sure the Claude desktop app's Hardware Buddy window is disconnected
first — the stick only pairs with one BLE central at a time.
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

# Windows defaults stdout to cp1252; force UTF-8 so non-ASCII in logs
# never raises UnicodeEncodeError mid-loop.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # central → device (write)
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device → central (notify)

NAME_PREFIX = "Claude"
PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _ipc_base() -> Path:
    """OS-appropriate cache dir for the bridge<->hook sidecar files."""
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


# IPC directory the PreToolUse hook writes to. Layout:
#   prompts/<id>.json     hook -> bridge   (pending decision)
#   decisions/<id>.json   bridge -> hook   (user's choice)
#   bridge-alive          bridge touches every tick; hook skips if stale
IPC_DIR = _ipc_base()
PROMPTS_DIR = IPC_DIR / "prompts"
DECISIONS_DIR = IPC_DIR / "decisions"
ALIVE_FILE = IPC_DIR / "bridge-alive"

TICK_SECONDS = 1.0
KEEPALIVE_SECONDS = 10.0
RUNNING_WINDOW = 5.0    # session counted "running" if mtime within this many seconds
OPEN_WINDOW = 3600.0    # session counted in "total" if mtime within this many seconds
WRITE_CHUNK = 180       # safe sub-MTU size for fragmented writes


class JsonlAggregator:
    """Tails every JSONL under PROJECTS_DIR and builds the heartbeat payload."""

    def __init__(self, root: Path):
        self.root = root
        self.offsets: dict[Path, int] = {}
        self.session_tokens: dict[str, int] = {}   # session_id → cumulative output tokens
        self.session_mtime: dict[str, float] = {}  # session_id → last activity epoch
        self.today_tokens = 0
        self.today_date = dt.date.today()
        self.lifetime_tokens = 0
        self.recent_entries: list[tuple[float, str]] = []  # (ts, label), newest first

    def _reset_today_if_needed(self):
        today = dt.date.today()
        if today != self.today_date:
            self.today_date = today
            self.today_tokens = 0

    def scan(self):
        """Read any new bytes from every JSONL file, update state."""
        self._reset_today_if_needed()
        if not self.root.exists():
            return
        midnight = dt.datetime.combine(self.today_date, dt.time.min).timestamp()
        for path in self.root.glob("*/*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            offset = self.offsets.get(path, 0)
            if stat.st_size < offset:
                # File was truncated or rotated; start over.
                offset = 0
            if stat.st_size == offset:
                continue
            try:
                with path.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                    new_offset = f.tell()
            except OSError:
                continue
            # Only count full lines; stash partial line by rewinding the offset.
            last_nl = chunk.rfind(b"\n")
            if last_nl == -1:
                continue
            consumed = chunk[: last_nl + 1]
            self.offsets[path] = offset + len(consumed)
            for raw in consumed.splitlines():
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                self._ingest(obj, midnight)

    def _ingest(self, obj: dict, midnight: float):
        if obj.get("type") != "assistant":
            return
        msg = obj.get("message") or {}
        usage = msg.get("usage") or {}
        out = int(usage.get("output_tokens") or 0)
        if out <= 0:
            return
        session_id = obj.get("sessionId") or "unknown"
        ts_iso = obj.get("timestamp")
        ts = _parse_iso(ts_iso) if ts_iso else time.time()

        self.session_tokens[session_id] = self.session_tokens.get(session_id, 0) + out
        self.session_mtime[session_id] = max(self.session_mtime.get(session_id, 0.0), ts)
        self.lifetime_tokens += out
        if ts >= midnight:
            self.today_tokens += out

        label = _label_for(msg, obj)
        if label:
            self.recent_entries.append((ts, label))
            # Keep the newest 12; we'll publish the top few.
            self.recent_entries.sort(key=lambda x: x[0], reverse=True)
            del self.recent_entries[12:]

    def snapshot(self) -> dict:
        now = time.time()
        running = sum(1 for ts in self.session_mtime.values() if now - ts <= RUNNING_WINDOW)
        total = sum(1 for ts in self.session_mtime.values() if now - ts <= OPEN_WINDOW)
        entries = [
            f"{dt.datetime.fromtimestamp(ts).strftime('%H:%M')} {label}"
            for ts, label in self.recent_entries[:4]
        ]
        msg = (
            f"running: {running}" if running
            else f"idle ({total} recent)" if total
            else "idle"
        )
        # Send the lifetime cumulative — firmware first-sight-latches the
        # initial value (stats.h:84) so we don't credit history as deltas;
        # subsequent ticks drive lvl (every 50K tokens) and fed (every 5K).
        return {
            "total": total,
            "running": running,
            "waiting": 0,  # CLI permission prompts don't surface in JSONL until resolved
            "msg": msg,
            "entries": entries,
            "tokens": self.lifetime_tokens,
            "tokens_today": self.today_tokens,
        }


class PromptStore:
    """File-based IPC with the PreToolUse hook.

    The hook drops a JSON file into prompts/ with a unique id, tool name,
    and hint. The bridge picks the oldest, ships it to the stick, then
    waits for the stick's notify to come back; once decided, writes the
    decision into decisions/<id>.json which the blocking hook reads.
    """

    def __init__(self):
        PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
        IPC_DIR.mkdir(parents=True, exist_ok=True)
        # Drop any stale prompts left from a previous run — they'd never
        # resolve and would block fresh hook invocations forever.
        for p in PROMPTS_DIR.glob("*.json"):
            try: p.unlink()
            except OSError: pass
        for p in DECISIONS_DIR.glob("*.json"):
            try: p.unlink()
            except OSError: pass
        self.active_id: str | None = None
        self.active_payload: dict | None = None

    def touch_alive(self):
        try:
            ALIVE_FILE.touch()
        except OSError:
            pass

    def pick_next(self) -> dict | None:
        """Return the oldest pending prompt, or None. Caches the active one
        so we keep showing it until resolved (the file isn't deleted until
        a decision is written)."""
        if self.active_payload is not None:
            return self.active_payload
        pending = sorted(PROMPTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for p in pending:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # corrupt or vanished — drop it
                try: p.unlink()
                except OSError: pass
                continue
            self.active_id = data.get("id") or p.stem
            self.active_payload = {
                "id": self.active_id,
                "tool": str(data.get("tool", ""))[:19],
                "hint": str(data.get("hint", ""))[:43],
            }
            return self.active_payload
        return None

    def resolve(self, prompt_id: str, decision: str):
        """Write the decision and drop the prompt file."""
        if not prompt_id:
            return
        DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
        out = DECISIONS_DIR / f"{prompt_id}.json"
        try:
            out.write_text(
                json.dumps({"decision": decision, "ts": time.time()}),
                encoding="utf-8",
            )
        except OSError as e:
            print(f"[prompt] failed to write decision for {prompt_id}: {e}")
        # Delete the prompt so we move on to the next one (if any).
        prompt_file = PROMPTS_DIR / f"{prompt_id}.json"
        try: prompt_file.unlink()
        except OSError: pass
        if self.active_id == prompt_id:
            self.active_id = None
            self.active_payload = None


def _parse_iso(s: str) -> float:
    # "2026-05-21T23:18:15.933Z" → epoch seconds
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return time.time()


def _label_for(msg: dict, obj: dict) -> str | None:
    content = msg.get("content") or []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name = block.get("name") or "tool"
            inp = block.get("input") or {}
            hint = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
            hint = str(hint).splitlines()[0][:40] if hint else ""
            return f"{name} {hint}".strip()
        if block.get("type") == "text":
            text = (block.get("text") or "").strip().splitlines()
            if text:
                return text[0][:50]
    return None


async def find_stick(timeout: float = 8.0) -> BLEDevice | None:
    print(f"[scan] looking for '{NAME_PREFIX}-*' for {timeout:.0f}s ...")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=False)
    for d in devices:
        if d.name and d.name.startswith(NAME_PREFIX):
            print(f"[scan] found {d.name} ({d.address})")
            return d
    print("[scan] no matching device. is the stick awake? press a button.")
    return None


async def write_payload(client: BleakClient, payload: dict):
    line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    # response=True forces ATT Write Request (vs Write Command). On WinRT
    # this path properly carries the encrypted-link protection level for
    # bonded peripherals; response=False sometimes drops it and the
    # peripheral rejects the write with E_INVALIDARG / "parameter incorrect".
    for i in range(0, len(line), WRITE_CHUNK):
        await client.write_gatt_char(NUS_RX, line[i : i + WRITE_CHUNK], response=True)


async def session(device: BLEDevice, agg: JsonlAggregator, prompts: PromptStore):
    async with BleakClient(device) as client:
        print(f"[ble] connected to {device.name}")

        # The stick advertises GATT chars with ESP_GATT_PERM_*_ENCRYPTED and
        # ESP_LE_AUTH_REQ_SC_MITM_BOND, so the link must be bonded before
        # the first write. Windows shows a system toast for passkey entry;
        # the 6-digit code is on the stick's OLED.
        try:
            ok = await client.pair(protection_level=3)
            print(f"[ble] pair() returned {ok}")
        except Exception as e:
            print(f"[ble] pair() failed (ok if already bonded at OS level): {e}")

        # Notify handler: the stick sends decisions as newline-terminated
        # JSON over TX. Notifications fragment at MTU, so reassemble.
        notify_buf = bytearray()

        def on_notify(_h, data: bytes):
            notify_buf.extend(data)
            while True:
                nl = notify_buf.find(b"\n")
                if nl < 0:
                    return
                line = bytes(notify_buf[:nl])
                del notify_buf[: nl + 1]
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    print(f"[rx] non-JSON: {line!r}")
                    continue
                if obj.get("cmd") == "permission":
                    pid = obj.get("id") or ""
                    decision = obj.get("decision") or ""
                    print(f"[rx] permission id={pid} decision={decision}")
                    prompts.resolve(pid, decision)
                else:
                    print(f"[rx] {obj}")

        # Subscribing also writes the CCCD descriptor (firmware marks it
        # WRITE_ENCRYPTED), which forces WinRT to promote the ATT link to
        # an encrypted bearer — required before any later RX write succeeds.
        try:
            await client.start_notify(NUS_TX, on_notify)
            print("[ble] notify subscribed (link should now be encrypted)")
        except Exception as e:
            print(f"[ble] start_notify failed: {e}")
            return

        # RTC sync. data.h:77-84 reads {"time":[epoch_sec, tz_offset_sec]}
        # where local = epoch + offset. Without this dataRtcValid() stays
        # false and clock-dependent UI may show a 2000 epoch.
        local_offset = int(dt.datetime.now().astimezone().utcoffset().total_seconds())
        try:
            await write_payload(client, {"time": [int(time.time()), local_offset]})
            print(f"[bridge] sent time sync (utc offset {local_offset}s)")
        except Exception as e:
            print(f"[ble] time sync write failed: {e}")
            return

        agg.scan()  # prime
        print(f"[bridge] lifetime {agg.lifetime_tokens} tokens, today {agg.today_tokens}")

        last_payload: dict | None = None
        last_sent = 0.0
        last_running = 0
        while client.is_connected:
            agg.scan()
            prompts.touch_alive()
            payload = agg.snapshot()
            # data.h:89 reads {"completed": bool}. main.cpp's derive() maps
            # recentlyCompleted=true to P_CELEBRATE — that's the "turn just
            # finished" animation. Fire it once when running drops to zero.
            completed = last_running > 0 and payload["running"] == 0
            payload["completed"] = completed
            last_running = payload["running"]
            # Pending permission prompt from the hook? Inject it.
            pending = prompts.pick_next()
            if pending is not None:
                payload["prompt"] = pending
                payload["waiting"] = 1
                payload["msg"] = f"approve: {pending['tool']}"[:23]
            now = time.time()
            changed = payload != last_payload
            keepalive_due = (now - last_sent) >= KEEPALIVE_SECONDS
            if changed or keepalive_due:
                try:
                    await write_payload(client, payload)
                except Exception as e:
                    print(f"[ble] write failed: {e}")
                    if "-2147024809" in str(e) or "parameter is incorrect" in str(e).lower():
                        print("[ble] this usually means the BLE link isn't bonded.")
                        print("[ble] pair the stick through Windows Settings -> Bluetooth & devices ->")
                        print("[ble]   Add device -> 'Claude-XXXX'. enter the 6-digit code from the")
                        print("[ble]   stick's OLED. then re-run this script.")
                    break
                last_payload = payload
                last_sent = now
                if changed:
                    tag = " CELEBRATE" if payload.get("completed") else ""
                    print(f"[tx] {payload['msg']} | tokens={payload['tokens']} today={payload['tokens_today']} entries={len(payload['entries'])}{tag}")
            await asyncio.sleep(TICK_SECONDS)
        print("[ble] disconnected")


async def main_loop():
    if not PROJECTS_DIR.exists():
        raise SystemExit(f"projects dir not found: {PROJECTS_DIR}")
    agg = JsonlAggregator(PROJECTS_DIR)
    prompts = PromptStore()
    print(f"[bridge] IPC dir: {IPC_DIR}")
    while True:
        device = await find_stick()
        if not device:
            await asyncio.sleep(5)
            continue
        try:
            await session(device, agg, prompts)
        except Exception as e:
            print(f"[ble] session error: {e}")
        print("[bridge] reconnecting in 3s ...")
        await asyncio.sleep(3)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--scan-only", action="store_true", help="just list nearby BLE devices and exit")
    return p.parse_args()


async def scan_only():
    print("[scan] 8s discovery ...")
    devices = await BleakScanner.discover(timeout=8.0, return_adv=False)
    for d in devices:
        print(f"  {d.address}  {d.name!r}")


if __name__ == "__main__":
    args = parse_args()
    try:
        if args.scan_only:
            asyncio.run(scan_only())
        else:
            asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\n[bridge] bye")
