#!/usr/bin/env python3
"""
Hermes Mascot Bridge Server
Accurate stage tracking, queue support, and strict session-liveness synchronization.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Set, Dict, Any, Optional, List

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
RE_API_CALL = re.compile(
    r"agent\.conversation_loop:\s+API call #(?P<num>\d+):\s+model=(?P<model>\S+)"
)
RE_TOOL_COMPLETED = re.compile(
    r"agent\.tool_executor:\s+tool\s+(?P<tool>\w+)\s+completed\s+\((?P<dur>[\d\.]+)s"
)
RE_TOOL_EXECUTING = re.compile(
    r"executing tool:\s+(?P<tool>\w+)"
)

TOOL_STAGE_MAP = {
    # Terminal
    "terminal": ("terminal", "⚡ Терминал"),
    "laptop_terminal": ("terminal", "💻 Терминал"),
    "laptop_agent": ("terminal", "💻 Агент"),
    # Search
    "search_files": ("search", "🔍 Поиск"),
    "web_search": ("search", "🌐 Веб-поиск"),
    "web_extract": ("search", "📄 Веб"),
    "browser_exec": ("search", "🌐 Браузер"),
    # Files / Code
    "read_file": ("files", "📖 Чтение"),
    "write_file": ("files", "✍️ Запись"),
    "patch": ("files", "🛠️ Правка"),
    # AI / Vision
    "vision_analyze": ("thinking", "👁️ Вижн"),
}

class MascotState:
    def __init__(self):
        self.status: str = "idle"  # idle, thinking, working, done
        self.current_stage: str = "idle"  # idle, thinking, search, terminal, files
        self.stage_label: str = ""
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
        self.queue: List[str] = []
        self._done_timer_task: Optional[asyncio.Task] = None
        self.subscribers: Set[asyncio.Queue] = set()

    def to_dict(self) -> Dict[str, Any]:
        elapsed = 0.0
        if self.active and self.turn_start_ts > 0:
            elapsed = round(time.time() - self.turn_start_ts, 1)
        elif self.last_response_time is not None:
            elapsed = round(self.last_response_time, 1)

        return {
            "status": self.status,
            "active": self.active,
            "current_stage": self.current_stage,
            "stage_label": self.stage_label,
            "prompt": self.prompt,
            "user": self.user,
            "current_tool": self.current_tool,
            "tool_duration": self.tool_duration,
            "api_call_count": self.api_call_count,
            "model": self.model,
            "elapsed_seconds": elapsed,
            "last_response_time": self.last_response_time,
            "session_id": self.session_id,
            "queued_count": len(self.queue),
            "queue": self.queue[:3],
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
        self.current_stage = "done"
        self.stage_label = "Выполнено"
        self.active = False
        self.current_tool = None
        self.queue.clear()
        if duration is not None:
            self.last_response_time = duration
        if self._done_timer_task and not self._done_timer_task.done():
            self._done_timer_task.cancel()

        async def _transition_to_idle():
            await asyncio.sleep(4.0)
            if self.status == "done" and not self.active:
                self.status = "idle"
                self.current_stage = "idle"
                self.stage_label = ""
                self.prompt = ""
                self.current_tool = None
                self.api_call_count = 0
                await self.broadcast()

        self._done_timer_task = asyncio.create_task(_transition_to_idle())

state = MascotState()

async def tail_log(filepath: Path, on_line):
    """Tail file from EOF."""
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

        # If currently active, this is a QUEUED follow-up!
        if state.active and state.prompt:
            if raw_msg not in state.queue and raw_msg != state.prompt:
                state.queue.append(raw_msg)
                logger.info("Queued task added: %s (queue len: %d)", raw_msg[:50], len(state.queue))
        else:
            state.prompt = raw_msg
            state.status = "thinking"
            state.current_stage = "thinking"
            state.stage_label = "Обдумывание задачи"
            state.active = True
            state.turn_start_ts = time.time()
            state.api_call_count = 0
            state.current_tool = None
            logger.info("New TG task from %s: %s", user, state.prompt[:60])

        await state.broadcast()
        return

    # Queued follow-up transition
    if "Queued follow-up for session" in line:
        logger.info("Queued follow-up active in gateway")
        if state.queue:
            state.prompt = state.queue.pop(0)
        state.active = True
        state.status = "working"
        state.current_stage = "thinking"
        state.stage_label = "Следующий запрос из очереди"
        await state.broadcast()
        return

    # Response ready ONLY updates duration; does NOT stop session if still active!
    m_out = RE_RESPONSE_READY.search(line)
    if m_out:
        dur = float(m_out.group("time"))
        state.last_response_time = dur
        logger.info("Interim turn response ready in %.1fs", dur)
        return

async def on_agent_line(line: str):
    m_api = RE_API_CALL.search(line)
    if m_api:
        state.api_call_count = int(m_api.group("num"))
        state.model = m_api.group("model")
        state.active = True
        state.status = "thinking"
        state.current_stage = "thinking"
        state.stage_label = f"Обдумываю ответ (API #{state.api_call_count})"
        await state.broadcast()
        return

    m_exec = RE_TOOL_EXECUTING.search(line)
    if m_exec:
        tool = m_exec.group("tool")
        state.current_tool = tool
        stage, label = TOOL_STAGE_MAP.get(tool, ("working", f"Инструмент: {tool}"))
        state.current_stage = stage
        state.stage_label = label
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
        stage, label = TOOL_STAGE_MAP.get(tool, ("working", f"Инструмент: {tool}"))
        state.current_stage = stage
        state.stage_label = f"{label} ({dur:.1f}s)"
        state.status = "working"
        state.active = True
        await state.broadcast()
        return

def get_latest_user_prompt() -> str:
    """Fetch the latest active user prompt from state.db as ground-truth fallback."""
    try:
        if not STATE_DB.exists():
            return ""
        con = sqlite3.connect(str(STATE_DB))
        cur = con.cursor()
        cur.execute("SELECT content FROM messages WHERE role='user' ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        con.close()
        if row and row[0]:
            # Clean prompt
            text = row[0]
            # Strip image markers or system headers
            lines = [l for l in text.splitlines() if not l.startswith("[The user sent an image") and not l.startswith("[If you need a closer look")]
            return "\n".join(lines).strip()
    except Exception as e:
        logger.debug("get_latest_user_prompt error: %s", e)
    return ""

async def watch_active_sessions():
    """Ground truth for session liveness."""
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
                    logger.info("Session lease released in active_sessions.json -> Done!")
                    state.trigger_done()
                    await state.broadcast()
                elif tg_active:
                    state.active = True
                    if not state.prompt:
                        p = get_latest_user_prompt()
                        if p:
                            state.prompt = p
                            state.user = "черепахабро⁹²"
                    if state.status == "idle":
                        state.status = "thinking"
                        state.current_stage = "thinking"
                        state.stage_label = "Выполнение задачи"
                        state.turn_start_ts = time.time()
                    await state.broadcast()
        except Exception as e:
            logger.debug("watch_active_sessions error: %s", e)
        await asyncio.sleep(0.5)

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

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "active": state.active, "subscribers": len(state.subscribers)})

def init_app():
    app = web.Application()
    app.router.add_get("/status", handle_status)
    app.router.add_get("/health", handle_health)
    return app

async def start_background_tasks(app):
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
