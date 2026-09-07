"""Roborock MCP server.

A thin Model Context Protocol server that lets an agent (Hermes, etc.) control a
Roborock vacuum. It wraps `python-roborock`, which talks to the robot directly
over your LAN when reachable and falls back to Roborock's cloud MQTT otherwise —
no rooting, no local-server stack required.

Setup:
    1.  pip install python-roborock mcp
    2.  python authenticate.py          # one-time, caches roborock_user_data.json
    3.  export ROBOROCK_USERNAME="you@example.com"
        # optional, if you own more than one vacuum:
        export ROBOROCK_DEVICE="Living room"     # matches on name or duid
    4.  Register this file as an MCP server (stdio transport), e.g. in your
        agent/registry config:
            command: python
            args: ["/path/to/roborock_mcp_server.py"]

All action tools return immediately after issuing the command. Cleaning runs for
tens of minutes; the agent should poll get_status() to track progress rather than
expecting an action tool to block until the robot is done.

Tested against python-roborock 5.36.x.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from roborock import RoborockCommand, UserData
from roborock.devices.device_manager import UserParams, create_device_manager

# --------------------------------------------------------------------------- #
# Configuration (via environment)
# --------------------------------------------------------------------------- #
USER_DATA_PATH = Path(os.environ.get("ROBOROCK_USER_DATA", "roborock_user_data.json"))
USERNAME = os.environ.get("ROBOROCK_USERNAME", "")
DEVICE_SELECTOR = os.environ.get("ROBOROCK_DEVICE", "")  # match by name substring or exact duid

mcp = FastMCP("roborock")

# --------------------------------------------------------------------------- #
# Connection management: connect once, reuse, reconnect on drop.
# --------------------------------------------------------------------------- #
_device: Any = None
_manager: Any = None
_manager_loop: asyncio.AbstractEventLoop | None = None
_lock = asyncio.Lock()


async def _get_device() -> Any:
    """Return a connected RoborockDevice, establishing the connection if needed."""
    global _device, _manager, _manager_loop
    current_loop = asyncio.get_running_loop()
    async with _lock:
        if _device is not None:
            if _manager_loop is not current_loop:
                raise RuntimeError(
                    "The cached Roborock connection belongs to a closed or different "
                    "event loop. Keep requests on one long-lived loop, or call "
                    "close_connection() before closing the original loop."
                )
            # The library reconnects dropped links in the background on its own;
            # re-running discovery here would leak a second MQTT session.
            return _device

        if not USER_DATA_PATH.exists():
            raise RuntimeError(
                f"No cached credentials at {USER_DATA_PATH}. Run authenticate.py first."
            )
        if not USERNAME:
            raise RuntimeError("Set the ROBOROCK_USERNAME environment variable.")

        user_data = UserData.from_dict(json.loads(USER_DATA_PATH.read_text()))
        params = UserParams(username=USERNAME, user_data=user_data)

        _manager = await create_device_manager(params)
        _manager_loop = current_loop
        devices = await _manager.get_devices()

        vacuums = [d for d in devices if getattr(d, "v1_properties", None)]
        if not vacuums:
            raise RuntimeError("No V1-protocol Roborock vacuum found on this account.")

        if DEVICE_SELECTOR:
            matches = [
                d
                for d in vacuums
                if DEVICE_SELECTOR == d.duid or DEVICE_SELECTOR.lower() in (d.name or "").lower()
            ]
            _device = matches[0] if matches else vacuums[0]
        else:
            _device = vacuums[0]

        # The device manager already connects each device during discovery and
        # keeps reconnecting in the background; calling connect() again raises
        # "Already connected". Just wait briefly for the link to come up.
        for _ in range(20):
            if _device.is_connected:
                break
            await asyncio.sleep(0.5)
        return _device


async def close_connection() -> None:
    """Close cached device connections used by one-shot scripts."""
    global _device, _manager, _manager_loop
    current_loop = asyncio.get_running_loop()
    async with _lock:
        manager = _manager
        manager_loop = _manager_loop
        _device = None
        _manager = None
        _manager_loop = None
    if manager is not None and manager_loop is current_loop:
        await manager.close()


async def _send(command: RoborockCommand, params: Any | None = None) -> Any:
    """Issue a single V1 command through the device's command trait."""
    device = await _get_device()
    return await device.v1_properties.command.send(command, params)


def _jsonable(obj: Any) -> Any:
    """Best-effort conversion of roborock container objects into plain JSON types."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, "as_dict"):
        try:
            return obj.as_dict()
        except Exception:
            pass
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(x) for x in obj]
    return str(obj)


# --------------------------------------------------------------------------- #
# Read tools
# --------------------------------------------------------------------------- #
@mcp.tool()
async def get_status() -> dict:
    """Current vacuum state: activity (cleaning / docked / paused / returning),
    battery %, selected fan power, area and time of the current job, and any error
    code. Poll this to track a running clean.

    Also includes fan_speed_name and fan_speed_mapping — the {code: name} table of
    suction levels valid for THIS model, which are the values set_fan_power accepts."""
    device = await _get_device()
    status = device.v1_properties.status
    await status.refresh()
    result: dict = {"status": _jsonable(status)}
    try:
        result["fan_speed_name"] = status.fan_speed_name
        result["fan_speed_mapping"] = status.fan_speed_mapping
    except Exception:
        pass  # feature table unavailable for this product; raw status still stands
    return result


@mcp.tool()
async def list_rooms() -> dict:
    """List the cleanable rooms on the robot's current map, with human-readable
    names joined from your Roborock account.

    Pass the segment_id values to clean_rooms()."""
    device = await _get_device()
    rooms = device.v1_properties.rooms
    await rooms.refresh()
    return {
        "rooms": [
            {"segment_id": r.segment_id, "name": r.name} for r in (rooms.rooms or [])
        ]
    }


@mcp.tool()
async def get_consumables() -> dict:
    """Remaining life / elapsed use (in seconds) for the main brush, side brush,
    filter, sensors and similar wear parts."""
    return {"consumables": _jsonable(await _send(RoborockCommand.GET_CONSUMABLE))}


@mcp.tool()
async def get_clean_history(limit: int = 10) -> dict:
    """Cleaning-history summary plus detailed recent records.

    Args:
        limit: Number of recent records to load (1..50).

    Roborock's summary response contains record IDs, not the corresponding
    timestamps, completion state, area, or finish reason.  Load each requested
    record explicitly so callers never need to infer those details from an ID.
    """
    limit = max(1, min(int(limit), 50))
    device = await _get_device()
    summary = device.v1_properties.clean_summary
    await summary.refresh()

    record_ids = list(summary.records or [])[:limit]
    records: list[dict] = []
    for record_id in record_ids:
        try:
            if (
                summary.last_clean_record is not None
                and record_id == (summary.records or [None])[0]
            ):
                record = summary.last_clean_record
            else:
                record = await summary.get_clean_record(record_id)

            payload = _jsonable(record)
            if not isinstance(payload, dict):
                payload = {"raw": payload}
            payload["record_id"] = record_id

            begin = getattr(record, "begin", None)
            end = getattr(record, "end", None)
            payload["begin_local"] = (
                datetime.fromtimestamp(begin).astimezone().isoformat() if begin else None
            )
            payload["end_local"] = (
                datetime.fromtimestamp(end).astimezone().isoformat() if end else None
            )
            payload["area_m2"] = getattr(record, "square_meter_area", None)
            for attr in ("start_type", "clean_type", "finish_reason"):
                value = getattr(record, attr, None)
                payload[f"{attr}_name"] = getattr(value, "name", None)
            records.append(payload)
        except Exception as exc:
            records.append({"record_id": record_id, "error_loading_record": str(exc)})

    clean_area = getattr(summary, "clean_area", None)
    clean_time = getattr(summary, "clean_time", None)
    return {
        "clean_summary": {
            "clean_time_seconds": clean_time,
            "clean_area_m2": round(clean_area / 1_000_000, 1)
            if isinstance(clean_area, int | float)
            else None,
            "clean_count": getattr(summary, "clean_count", None),
            "dust_collection_count": getattr(summary, "dust_collection_count", None),
            "record_ids": list(summary.records or []),
        },
        "records": records,
    }


# --------------------------------------------------------------------------- #
# Action tools (return immediately; poll get_status for progress)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def start_clean() -> str:
    """Start a full clean of the entire current map."""
    await _send(RoborockCommand.APP_START)
    return "Full clean started. Poll get_status() for progress."


@mcp.tool()
async def clean_rooms(segments: list[int], repeat: int = 1) -> str:
    """Clean specific rooms.

    Args:
        segments: segment IDs from list_rooms(), e.g. [16, 19].
        repeat:   number of passes per room (1 or 2).
    """
    if not segments:
        return "No segments given. Call list_rooms() to get segment IDs first."
    await _send(RoborockCommand.APP_SEGMENT_CLEAN, [{"segments": segments, "repeat": repeat}])
    return f"Cleaning started for segments {segments} (x{repeat}). Poll get_status()."


@mcp.tool()
async def pause() -> str:
    """Pause the current job."""
    await _send(RoborockCommand.APP_PAUSE)
    return "Paused."


@mcp.tool()
async def stop() -> str:
    """Stop the current job (does not return to the dock)."""
    await _send(RoborockCommand.APP_STOP)
    return "Stopped."


@mcp.tool()
async def return_to_dock() -> str:
    """Send the vacuum back to its dock to charge."""
    await _send(RoborockCommand.APP_CHARGE)
    return "Returning to dock."


@mcp.tool()
async def locate() -> str:
    """Make the vacuum play a locate sound so you can find it."""
    await _send(RoborockCommand.FIND_ME)
    return "Locating — the robot should beep."


@mcp.tool()
async def set_fan_power(level: int | str) -> str:
    """Set suction power. Accepts either a numeric code or a level name.

    Call get_status() first — its fan_speed_mapping field lists the exact
    {code: name} pairs this model supports (e.g. {101: "quiet", 102: "balanced"}).
    """
    device = await _get_device()
    mapping: dict[int, str] = {}
    try:
        mapping = device.v1_properties.status.fan_speed_mapping
    except Exception:
        pass

    if isinstance(level, str) and not level.isdigit():
        by_name = {name.lower(): code for code, name in mapping.items()}
        code = by_name.get(level.lower())
        if code is None:
            return f"Unknown level {level!r}. This model supports: {mapping or 'unknown — pass a numeric code'}."
    else:
        code = int(level)
        if mapping and code not in mapping:
            return f"Code {code} is not in this model's table {mapping}. Not sent."

    await _send(RoborockCommand.SET_CUSTOM_MODE, [code])
    return f"Fan power set to {code} ({mapping.get(code, 'unverified')})."


# --------------------------------------------------------------------------- #
# Experimental: manual driving. Model-dependent — may be a no-op on some units.
# --------------------------------------------------------------------------- #
_rc_seq = 0


@mcp.tool()
async def drive(velocity: float = 0.2, omega: float = 0.0, duration_ms: int = 1000) -> str:
    """EXPERIMENTAL manual remote-control drive.

    Args:
        velocity:    forward speed in m/s (negative reverses; keep within ~-0.3..0.3).
        omega:       turn rate in rad/s (positive turns one way, negative the other).
        duration_ms: how long to move, in milliseconds.

    Sends an rc_start / rc_move / rc_end sequence. Support varies by model; if
    nothing moves, your unit likely doesn't expose remote control over this API.
    """
    global _rc_seq
    device = await _get_device()
    await device.v1_properties.command.send(RoborockCommand.APP_RC_START)
    _rc_seq += 1
    await device.v1_properties.command.send(
        RoborockCommand.APP_RC_MOVE,
        [{"omega": omega, "velocity": velocity, "seqnum": _rc_seq, "duration": duration_ms}],
    )
    await asyncio.sleep(duration_ms / 1000)
    await device.v1_properties.command.send(RoborockCommand.APP_RC_END)
    return f"Drove velocity={velocity} omega={omega} for {duration_ms}ms."


if __name__ == "__main__":
    mcp.run(transport="stdio")
