#!/usr/bin/env python3
"""
Hermes Mascot Bridge Server
Monitors Hermes Telegram Gateway activity and streams real-time status/events via HTTP.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Set, Dict, Any, Optional

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("hermes-mascot-bridge")

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/root/.hermes"))
GATEWAY_LOG = HERMES_HOME / "logs" / "gateway.log"
AGENT_LOG = HERMES_HOME / "logs" / "agent.log"
ACTIVE_SESSIONS = HERMES_HOME / "runtime" / "active_sessions.json"
STATE_DB = HERMES_HOME / "state.db"

# Strict Regex patterns
RE_INBOUND = re.compile(
    r"inbound message:\s+platform=telegram\s+user=(?P<user>.*?)\s+chat=(?P<chat>\d+)\s+msg='(?P<msg>.*?)'",
    re.DOTALL
)
RE_RESPONSE_READY = re.compile(
    r"response ready:\s+platform=telegram\s+chat=(?P<chat>\d+)\s+time=(?P<time>[\d\.]+)s"
)
RE_SENDING = re.compile(
    r"\[Telegram\] Sending response \((?P<len>\d+) chars\) to (?P<chat>\d+)"
)
RE_API_CALL = re.compile(
    r"agent\.conversation_loop:\s+API call #(?P<num>\d+):\s+model=(?P<model>\S+)"
)
RE_TOOL_COMPLETED = re.compile(
    r"agent\.tool_executor:\s+tool\s+(?P<tool>\w+)\s+completed\s+\((?P<dur>[\d\.]+)s"
)
RE_TOOL_EXECUTING = re.compile(
    r"executing tool:\s+(?P<tool>\w+)"
)

class MascotState:
    def __init__(self):
        self.status: str = "idle"  # idle, thinking, working, done
        self.prompt: str = ""
        self.user: str = ""
        self.current_tool: Optional[str] = None
        self.tool_duration: Optional[float] = None
        self.api_call_count: int = 0
        self.model: str = ""
        self.turn_start_ts: float = 0.0
        self.last_update_ts: float = time.time()
        self.last_response_time: Optional[float] = None
        self.session_id: str = ""
        self.active: bool = False
        self._done_timer_task: Optional[asyncio.Task] = None
        self.subscribers: Set[asyncio.Queue] = set()

    def to_dict(self) -> Dict[str, Any]:
        elapsed = 0.0
        if self.active and self.turn_start_ts > 0:
            elapsed = round(time.time() - self.turn_start_ts, 1)
        elif self.last_response_time is not None and self.status == "done":
            elapsed = round(self.last_response_time, 1)

        return {
            "status": self.status,
            "active": self.active,
            "prompt": self.prompt,
            "user": self.user,
            "current_tool": self.current_tool,
            "tool_duration": self.tool_duration,
            "api_call_count": self.api_call_count,
            "model": self.model,
            "elapsed_seconds": elapsed,
            "last_response_time": self.last_response_time,
            "session_id": self.session_id,
            "last_update": self.last_update_ts,
        }

    async def broadcast(self):
        self.last_update_ts = time.time()
        data = self.to_dict()
        dead = set()
        for q in self.subscribers:
            try:
                q.put_nowait(data)
            except Exception:
                dead.add(q)
        for q in dead:
            self.subscribers.discard(q)

    def trigger_done(self, duration: Optional[float] = None):
        self.status = "done"
        self.active = False
        self.current_tool = None
        if duration is not None:
            self.last_response_time = duration
        if self._done_timer_task and not self._done_timer_task.done():
            self._done_timer_task.cancel()

        async def _transition_to_idle():
            # Keep done state visible for 5s, then transition to idle
            await asyncio.sleep(5.0)
            if self.status == "done" and not self.active:
                self.status = "idle"
                self.prompt = ""
                self.current_tool = None
                self.api_call_count = 0
                await self.broadcast()

        self._done_timer_task = asyncio.create_task(_transition_to_idle())

state = MascotState()

async def read_db_last_prompt():
    """Fetch exact latest prompt and session info from state.db"""
    try:
        if not STATE_DB.exists():
            return
        conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, title, last_activity_description FROM sessions WHERE source='telegram' ORDER BY last_activity_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row:
            sess_id, title, activity = row
            state.session_id = sess_id
            cursor.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1",
                (sess_id,)
            )
            msg_row = cursor.fetchone()
            if msg_row and msg_row[0]:
                content = msg_row[0]
                # Strip system image tags if any
                if "[The user sent an image" in content:
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1:
                        content = parts[1]
                state.prompt = content.strip()
        conn.close()
    except Exception as e:
        logger.debug("state.db read error: %s", e)

async def tail_log(filepath: Path, on_line):
    """Tail a file from EOF onwards (NO replay of old lines)."""
    while not filepath.exists():
        await asyncio.sleep(1.0)

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            logger.info("Tailing %s from end (pos: %d)", filepath.name, f.tell())

            while True:
                line = f.readline()
                if line:
                    await on_line(line.strip())
                else:
                    await asyncio.sleep(0.1)
                    try:
                        cur_size = os.path.getsize(filepath)
                        if cur_size < f.tell():
                            logger.info("Log %s rotated/truncated, rewinding", filepath.name)
                            f.seek(0)
                    except OSError:
                        pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Error tailing %s: %s", filepath, e)

async def on_gateway_line(line: str):
    m_in = RE_INBOUND.search(line)
    if m_in:
        user = m_in.group("user")
        raw_msg = m_in.group("msg").strip()
        state.user = user
        state.status = "thinking"
        state.active = True
        state.turn_start_ts = time.time()
        state.api_call_count = 0
        state.current_tool = None
        state.prompt = raw_msg
        logger.info("New TG task from %s: %s", user, state.prompt[:60])
        await state.broadcast()
        return

    m_out = RE_RESPONSE_READY.search(line)
    if m_out:
        dur = float(m_out.group("time"))
        logger.info("TG response ready in %.1fs", dur)
        state.trigger_done(dur)
        await state.broadcast()
        return

    if "[Telegram] Sending response" in line:
        if state.active:
            state.trigger_done()
            await state.broadcast()
        return

async def on_agent_line(line: str):
    m_api = RE_API_CALL.search(line)
    if m_api:
        state.api_call_count = int(m_api.group("num"))
        state.model = m_api.group("model")
        state.active = True
        if state.status not in ("working", "thinking"):
            state.status = "thinking"
        await state.broadcast()
        return

    m_exec = RE_TOOL_EXECUTING.search(line)
    if m_exec:
        tool = m_exec.group("tool")
        state.current_tool = tool
        state.status = "working"
        state.active = True
        await state.broadcast()
        return

    m_tool = RE_TOOL_COMPLETED.search(line)
    if m_tool:
        tool = m_tool.group("tool")
        dur = float(m_tool.group("dur"))
        state.current_tool = tool
        state.tool_duration = dur
        state.status = "working"
        state.active = True
        await state.broadcast()
        return

async def watch_active_sessions():
    """Poll active_sessions.json to reconcile turn state without killing prompts prematurely."""
    while True:
        try:
            if ACTIVE_SESSIONS.exists():
                with open(ACTIVE_SESSIONS, "r", encoding="utf-8") as f:
                    data = json.load(f)
                entries = data.get("entries", [])
                tg_active = any(
                    e.get("metadata", {}).get("platform") == "telegram"
                    for e in entries
                )
                if not tg_active and state.active:
                    logger.info("Session became idle in active_sessions.json")
                    state.trigger_done()
                    await state.broadcast()
        except Exception as e:
            logger.debug("watch_active_sessions error: %s", e)
        await asyncio.sleep(2.0)

async def heartbeat_loop():
    while True:
        await asyncio.sleep(1.0)
        if state.active:
            await state.broadcast()

# Web handlers
async def handle_status(request: web.Request) -> web.Response:
    return web.json_response(state.to_dict(), headers={
        "Access-Control-Allow-Origin": "*"
    })

async def handle_events(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        }
    )
    await resp.prepare(request)

    queue = asyncio.Queue(maxsize=50)
    state.subscribers.add(queue)

    init_data = f"data: {json.dumps(state.to_dict(), ensure_ascii=False)}\n\n"
    await resp.write(init_data.encode("utf-8"))
    await resp.drain()

    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=15.0)
                msg = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                await resp.write(msg.encode("utf-8"))
                await resp.drain()
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
                await resp.drain()
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        state.subscribers.discard(queue)

    return resp

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "subscribers": len(state.subscribers)})

def init_app():
    app = web.Application()
    app.router.add_get("/status", handle_status)
    app.router.add_get("/events", handle_events)
    app.router.add_get("/health", handle_health)
    return app

async def start_background_tasks(app):
    await read_db_last_prompt()
    app["gw_task"] = asyncio.create_task(tail_log(GATEWAY_LOG, on_gateway_line))
    app["agent_task"] = asyncio.create_task(tail_log(AGENT_LOG, on_agent_line))
    app["session_task"] = asyncio.create_task(watch_active_sessions())
    app["heartbeat_task"] = asyncio.create_task(heartbeat_loop())

async def cleanup_background_tasks(app):
    for key in ("gw_task", "agent_task", "session_task", "heartbeat_task"):
        task = app.get(key)
        if task:
            task.cancel()

if __name__ == "__main__":
    app = init_app()
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    port = int(os.environ.get("MASCOT_PORT", "8455"))
    host = os.environ.get("MASCOT_HOST", "0.0.0.0")
    logger.info("Starting Hermes Mascot Bridge on %s:%d", host, port)
    web.run_app(app, host=host, port=port, print=None)
