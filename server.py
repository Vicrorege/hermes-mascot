#!/usr/bin/env python3
"""
Hermes Mascot Bridge Server
Accurate stage tracking, rock-solid queue support, and real-time state.db inspection.
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
RE_TEXT_BATCH = re.compile(
    r"\[Telegram\]\s+Flushing\s+(?:text|photo)\s+batch\s+agent:main:telegram:dm:\d+\s+\((?P<cnt>\d+)\s+(?P<unit>chars|image)"
)

def clean_prompt_text(text: str) -> str:
    if not text:
        return ""
    lines = []
    for l in text.splitlines():
        l_strip = l.strip()
        if (
            l_strip.startswith("[The user sent an image") or
            l_strip.startswith("[If you need a closer look") or
            l_strip.startswith("[IMAGE:")
        ):
            continue
        lines.append(l)
    res = "\n".join(lines).strip()
    return res if res else "Запрос с изображением"


BRIDGE_CONFIG_FILE = Path("/root/projects/hermes-mascot-bridge/bridge_config.json")

def load_bridge_config() -> dict:
    if BRIDGE_CONFIG_FILE.exists():
        try:
            with open(BRIDGE_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"auto_launch": True, "laptop_host": "timant32@192.168.0.132:2222"}

def save_bridge_config(cfg: dict):
    try:
        with open(BRIDGE_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

bridge_cfg = load_bridge_config()

class MascotState:
    def __init__(self):
        self.status: str = "idle"  # idle, thinking, working, done
        self.current_stage: str = "idle"  # idle, thinking, search, terminal, files
        self.stage_label: str = ""
        self.tool_action: str = ""
        self.prompt: str = ""
        self.user: str = "черепахабро⁹²"
        self.current_tool: Optional[str] = None
        self.tool_duration: Optional[float] = None
        self.api_call_count: int = 0
        self.model: str = ""
        self.turn_start_ts: float = 0.0
        self.last_update_ts: float = time.time()
        self.last_response_time: Optional[float] = None
        self.session_id: str = ""
        self.active: bool = False
        self.queued_count: int = 0
        self.last_user_msg_id: int = 0
        self.auto_launch: bool = bridge_cfg.get("auto_launch", True)
        self.saw_active_lease: bool = False
        self.unseen_lease_count: int = 0
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
            "tool_action": self.tool_action,
            "prompt": self.prompt,
            "user": self.user,
            "current_tool": self.current_tool,
            "tool_duration": self.tool_duration,
            "api_call_count": self.api_call_count,
            "model": self.model,
            "elapsed_seconds": elapsed,
            "last_response_time": self.last_response_time,
            "session_id": self.session_id,
            "queued_count": self.queued_count,
            "auto_launch": self.auto_launch,
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
        self.tool_action = ""
        self.queued_count = 0
        if duration is not None:
            self.last_response_time = duration
        if self._done_timer_task and not self._done_timer_task.done():
            self._done_timer_task.cancel()

        async def _transition_to_idle():
            await asyncio.sleep(4.5)
            if self.status == "done" and not self.active:
                self.status = "idle"
                self.current_stage = "idle"
                self.stage_label = ""
                self.tool_action = ""
                self.prompt = ""
                self.current_tool = None
                self.api_call_count = 0
                await self.broadcast()

        self._done_timer_task = asyncio.create_task(_transition_to_idle())

state = MascotState()

async def ensure_laptop_mascot_running():
    """If auto_launch is enabled, trigger startup on laptop if not running."""
    if not state.auto_launch:
        return
    try:
        host_parts = bridge_cfg.get("laptop_host", "timant32@192.168.0.132:2222").split(":")
        user_host = host_parts[0]
        port = host_parts[1] if len(host_parts) > 1 else "22"
        cmd = "/home/timant32/.local/bin/hermes-mascot start"
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-p", port, "-o", "ConnectTimeout=2", "-o", "BatchMode=yes",
            user_host, cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.5)
        out_msg = stdout.decode().strip()
        logger.info("ensure_laptop_mascot_running result: %s", out_msg)
    except Exception as e:
        logger.error("ensure_laptop_mascot_running error: %s", e)

def format_tool_action(name: str, args: dict) -> tuple[str, str, str]:
    """Format tool into (stage, badge_label, specific_action_detail)."""
    if name in ("terminal", "laptop_terminal"):
        cmd = args.get("command", "").strip()
        cmd_first = cmd.splitlines()[0] if cmd else ""
        if len(cmd_first) > 65:
            cmd_first = cmd_first[:62] + "..."
        icon = "💻 " if name == "laptop_terminal" else "⚡ "
        return "terminal", f"{icon}{name}", cmd_first

    elif name in ("read_file", "write_file", "patch"):
        path = args.get("path", "")
        short_path = path
        for prefix in ("/root/projects/", "/root/.hermes/", "/root/", "/home/timant32/"):
            if short_path.startswith(prefix):
                short_path = short_path[len(prefix):]
                break
        if len(short_path) > 50:
            short_path = "..." + short_path[-47:]
        icons = {"read_file": "📖 ", "write_file": "✍️ ", "patch": "🛠️ "}
        return "files", f"{icons.get(name, '📄 ')}{name}", short_path

    elif name == "search_files":
        pat = args.get("pattern", "")
        return "search", "🔍 search_files", f"'{pat}'"

    elif name == "web_search":
        q = args.get("query", "")
        return "search", "🌐 web_search", f"'{q}'"

    elif name == "web_extract":
        urls = args.get("urls", [])
        u = urls[0] if urls else ""
        return "search", "📄 web_extract", u[:45]

    elif name == "vision_analyze":
        return "thinking", "👁️ vision_analyze", "Анализ изображения"

    elif name in ("skill_view", "skill_manage"):
        op_name = args.get("name") or (args.get("operations", [{}])[0].get("name") if args.get("operations") else "")
        return "files", f"🧩 {name}", str(op_name)

    return "working", f"⚙️ {name}", str(args)[:45]

def get_active_tool_and_args() -> Optional[tuple[str, str, str]]:
    """Inspect state.db for currently uncompleted tool call."""
    try:
        if not STATE_DB.exists():
            return None
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute('''
            SELECT m.id, m.tool_calls 
            FROM messages m
            WHERE m.tool_calls IS NOT NULL AND m.role = 'assistant'
            ORDER BY m.id DESC LIMIT 1
        ''')
        row = cur.fetchone()
        if not row:
            con.close()
            return None
        ast_id, tool_calls_json = row
        calls = json.loads(tool_calls_json)

        cur.execute('''
            SELECT tool_call_id FROM messages 
            WHERE role = 'tool' AND id > ?
        ''', (ast_id,))
        completed_ids = set(r[0] for r in cur.fetchall())
        con.close()

        for c in calls:
            cid = c.get("id")
            if cid not in completed_ids:
                fn = c.get("function", {})
                name = fn.get("name")
                args_raw = fn.get("arguments", "{}")
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                return format_tool_action(name, args)
        return None
    except Exception as e:
        logger.debug("get_active_tool_and_args error: %s", e)
        return None

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
    # Detect incoming message while busy -> increment queue count
    m_batch = RE_TEXT_BATCH.search(line)
    if m_batch:
        if state.active:
            state.queued_count += 1
            logger.info("Queued follow-up message received (queued_count: %d)", state.queued_count)
            await state.broadcast()
        return

    m_in = RE_INBOUND.search(line)
    if m_in:
        user = m_in.group("user")
        raw_msg = m_in.group("msg").strip()
        state.user = user

        if not state.active:
            state.prompt = clean_prompt_text(raw_msg)
            state.status = "working"
            state.current_stage = "thinking"
            state.stage_label = "Обдумывание задачи"
            state.active = True
            state.turn_start_ts = time.time()
            state.api_call_count = 0
            state.tool_action = ""
            state.current_tool = None
            state.saw_active_lease = False
            state.unseen_lease_count = 0
            state.queued_count = 0
            logger.info("New task from %s: %s", user, state.prompt[:60])
            asyncio.create_task(ensure_laptop_mascot_running())
            await state.broadcast()
        else:
            asyncio.create_task(ensure_laptop_mascot_running())
        return

    if "Queued follow-up for session" in line:
        logger.info("Queued follow-up processing started in gateway!")
        state.active = True
        state.status = "working"
        state.current_stage = "thinking"
        state.stage_label = "Следующий запрос из очереди"
        state.turn_start_ts = time.time()
        state.tool_action = ""
        state.api_call_count = 0
        asyncio.create_task(ensure_laptop_mascot_running())
        await state.broadcast()
        return

    m_out = RE_RESPONSE_READY.search(line)
    if m_out:
        dur = float(m_out.group("time"))
        state.last_response_time = dur
        logger.info("Turn response ready in %.1fs", dur)
        return

async def on_agent_line(line: str):
    m_api = RE_API_CALL.search(line)
    if m_api:
        state.api_call_count = int(m_api.group("num"))
        state.model = m_api.group("model")
        state.active = True
        if state.status != "working":
            state.status = "working"
        state.current_stage = "thinking"
        state.stage_label = f"🧠 API #{state.api_call_count}"
        await state.broadcast()
        return

    m_exec = RE_TOOL_EXECUTING.search(line)
    if m_exec:
        tool = m_exec.group("tool")
        state.current_tool = tool
        state.active = True
        state.status = "working"
        await state.broadcast()
        return

    m_tool = RE_TOOL_COMPLETED.search(line)
    if m_tool:
        tool = m_tool.group("tool")
        dur = float(m_tool.group("dur"))
        state.current_tool = tool
        state.tool_duration = dur
        state.active = True
        state.status = "working"
        await state.broadcast()
        return

async def poll_active_tool_from_db():
    """Poll state.db for currently executing tool, live arguments, and queue turn transitions."""
    while True:
        try:
            if STATE_DB.exists():
                # 1. Check for prompt changes (including queued follow-ups)
                con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
                cur = con.cursor()
                cur.execute("SELECT id, content FROM messages WHERE role='user' ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
                con.close()

                if row:
                    msg_id, raw_content = row
                    cleaned = clean_prompt_text(raw_content)

                    if state.last_user_msg_id == 0:
                        state.last_user_msg_id = msg_id
                        if not state.prompt:
                            state.prompt = cleaned
                    elif msg_id > state.last_user_msg_id:
                        # NEW TURN BEGUN (e.g. from queue!)
                        logger.info("New turn detected from DB (id=%d): %s", msg_id, cleaned[:50])
                        state.last_user_msg_id = msg_id
                        state.prompt = cleaned
                        state.active = True
                        state.status = "working"
                        state.turn_start_ts = time.time()
                        state.api_call_count = 0
                        state.tool_action = ""
                        state.saw_active_lease = False
                        state.unseen_lease_count = 0
                        state.current_stage = "thinking"
                        state.stage_label = "Выполняю запрос"
                        if state.queued_count > 0:
                            state.queued_count -= 1
                        asyncio.create_task(ensure_laptop_mascot_running())
                        await state.broadcast()

                # 2. Check for active tool calls if active
                if state.active:
                    tool_info = get_active_tool_and_args()
                    if tool_info:
                        stage, badge, action_text = tool_info
                        changed = False
                        if state.current_stage != stage:
                            state.current_stage = stage
                            changed = True
                        if state.stage_label != badge:
                            state.stage_label = badge
                            changed = True
                        if state.tool_action != action_text:
                            state.tool_action = action_text
                            changed = True
                        if state.status != "working":
                            state.status = "working"
                            changed = True
                        if changed:
                            await state.broadcast()
                    else:
                        if state.tool_action and state.tool_action != "Обдумывание...":
                            state.current_stage = "thinking"
                            state.stage_label = f"🧠 API #{state.api_call_count}" if state.api_call_count else "🧠 Думаю..."
                            state.tool_action = "Обдумывание ответа..."
                            await state.broadcast()
        except Exception as e:
            logger.debug("poll_active_tool_from_db error: %s", e)
        await asyncio.sleep(0.25)

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

                now = time.time()
                turn_age = now - state.turn_start_ts if state.turn_start_ts > 0 else 0.0

                if tg_active:
                    state.saw_active_lease = True
                    state.unseen_lease_count = 0
                    if not state.active:
                        state.active = True
                        state.status = "working"
                        state.current_stage = "thinking"
                        state.stage_label = "Выполнение задачи"
                        state.turn_start_ts = now
                        await state.broadcast()

                elif state.active:
                    # Lease not in active_sessions.json
                    if state.saw_active_lease and turn_age > 2.0:
                        state.unseen_lease_count += 1
                        # Require 3 consecutive checks (~1.5s) with NO active lease AND no active tool in DB
                        if state.unseen_lease_count >= 3:
                            active_tool = get_active_tool_and_args()
                            if not active_tool:
                                logger.info(
                                    "Session lease confirmed released (unseen=%d, age=%.1fs) -> All tasks finished!",
                                    state.unseen_lease_count, turn_age
                                )
                                state.trigger_done()
                                await state.broadcast()
                    elif not state.saw_active_lease and turn_age > 20.0:
                        # Orphan task fallback
                        logger.info("Task timed out without registered lease -> Done")
                        state.trigger_done()
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

async def handle_toggle_autolaunch(request: web.Request) -> web.Response:
    val = request.query.get("enabled")
    if val is not None:
        state.auto_launch = val.lower() in ("true", "1", "yes")
    else:
        state.auto_launch = not state.auto_launch
    bridge_cfg["auto_launch"] = state.auto_launch
    save_bridge_config(bridge_cfg)
    logger.info("Auto-launch setting changed to: %s", state.auto_launch)
    await state.broadcast()
    return web.json_response({"auto_launch": state.auto_launch}, headers={
        "Access-Control-Allow-Origin": "*"
    })

def init_app():
    app = web.Application()
    app.router.add_get("/status", handle_status)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/toggle_autolaunch", handle_toggle_autolaunch)
    return app

async def start_background_tasks(app):
    app["gw_task"] = asyncio.create_task(tail_log(GATEWAY_LOG, on_gateway_line))
    app["agent_task"] = asyncio.create_task(tail_log(AGENT_LOG, on_agent_line))
    app["session_task"] = asyncio.create_task(watch_active_sessions())
    app["tool_db_task"] = asyncio.create_task(poll_active_tool_from_db())
    app["heartbeat_task"] = asyncio.create_task(heartbeat_loop())

async def cleanup_background_tasks(app):
    for key in ("gw_task", "agent_task", "session_task", "tool_db_task", "heartbeat_task"):
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
