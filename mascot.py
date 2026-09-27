#!/usr/bin/env python3
"""
Hermes Mascot - Official Companion for Hermes Agent (TG Gateway).
Authentic multi-stage animations from official Codex spritesheet:
- Search stage (Row 1: Walk/Search)
- Terminal stage (Row 7: Fast Work/Run)
- Files stage (Row 8: Review/Inspect)
- Thinking stage (Row 6: Wait/Ponder)
- Idle stage (Row 0: Grounded Breathing)
- Jump/Hop on poke or idle (Row 4: Jump)
- Done (Row 3: Waving Celebration)
Smooth cursor tracking with zero eye jitter.
"""

import sys
import os
import math
import time
import json
import random
import ctypes
import urllib.request
import subprocess
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QPoint, QRect, QRectF, QThread, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QColor, QBrush, QPen, QLinearGradient,
    QFont, QPainterPath, QImage, QRegion
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QMenu
)

CONFIG_DIR = Path.home() / ".config" / "hermes-mascot"
CONFIG_FILE = CONFIG_DIR / "config.json"
ASSETS_DIR = Path.home() / ".local" / "share" / "hermes-mascot"

DEFAULT_CONFIG = {
    "server_url": "http://192.168.0.127:8455",
    "skin": "hermes",  # "hermes" (Codex blue) or "turtlebro" (Emerald)
    "sound_enabled": False,
    "scale": 0.70,
    "pos_x": None,
    "pos_y": None,
    "auto_hide_idle": False,
    "auto_launch_on_query": True,
}

# Spritesheet dimensions
FRAME_W = 192
FRAME_H = 208

# Animation row mapping
ROW_IDLE = 0        # 6 frames
ROW_SEARCH = 1      # 8 frames (Walk/Search)
ROW_WAVE = 3        # 4 frames (Greeting/Celebration)
ROW_JUMP = 4        # 5 frames (Jump/Hop)
ROW_WAITING = 6     # 6 frames (Ponder/Thinking)
ROW_WORKING = 7     # 6 frames (Fast Run/Terminal)
ROW_REVIEW = 8      # 6 frames (Review/Files)

def make_pen(color, width=1.0, cap=None):
    p = QPen(color, float(width))
    if cap is not None:
        p.setCapStyle(cap)
    return p

def make_window_sticky(win_id: int):
    """Make the window appear on all virtual desktops/workspaces and stay on top."""
    try:
        x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XChangeProperty.restype = ctypes.c_int
        x11.XChangeProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_int
        ]
        x11.XSendEvent.restype = ctypes.c_int
        x11.XSendEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
            ctypes.c_long, ctypes.c_void_p
        ]
        x11.XFlush.restype = ctypes.c_int
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.restype = ctypes.c_int
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]

        display = x11.XOpenDisplay(None)
        if not display:
            return

        # 1. Set _NET_WM_DESKTOP to 0xFFFFFFFF (all workspaces)
        net_wm_desktop = x11.XInternAtom(display, b"_NET_WM_DESKTOP", 0)
        cardinal = x11.XInternAtom(display, b"CARDINAL", 0)
        all_desktops = ctypes.c_ulong(0xFFFFFFFF)
        x11.XChangeProperty(
            display,
            ctypes.c_ulong(win_id),
            net_wm_desktop,
            cardinal,
            32,
            0,
            ctypes.byref(all_desktops),
            1
        )

        # 2. Set _NET_WM_STATE to include STICKY and ABOVE
        root = x11.XDefaultRootWindow(display)
        net_wm_state = x11.XInternAtom(display, b"_NET_WM_STATE", 0)
        net_wm_state_sticky = x11.XInternAtom(display, b"_NET_WM_STATE_STICKY", 0)
        net_wm_state_above = x11.XInternAtom(display, b"_NET_WM_STATE_ABOVE", 0)

        class XClientMessageEvent(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_int),
                ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong),
                ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int),
                ("data", ctypes.c_long * 5)
            ]

        class XEvent(ctypes.Union):
            _fields_ = [
                ("type", ctypes.c_int),
                ("xclient", XClientMessageEvent),
                ("pad", ctypes.c_long * 24)
            ]

        for atom in (net_wm_state_sticky, net_wm_state_above):
            ev = XEvent()
            ev.type = 33
            ev.xclient.type = 33
            ev.xclient.serial = 0
            ev.xclient.send_event = 1
            ev.xclient.display = display
            ev.xclient.window = win_id
            ev.xclient.message_type = net_wm_state
            ev.xclient.format = 32
            ev.xclient.data[0] = 1
            ev.xclient.data[1] = atom
            ev.xclient.data[2] = 0
            ev.xclient.data[3] = 1
            ev.xclient.data[4] = 0

            mask = (1 << 20) | (1 << 19)
            x11.XSendEvent(display, root, 0, ctypes.c_long(mask), ctypes.byref(ev))

        x11.XFlush(display)
        x11.XCloseDisplay(display)
    except Exception:
        pass

def load_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                res = DEFAULT_CONFIG.copy()
                res.update(cfg)
                return res
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


class EventWorker(QThread):
    status_updated = pyqtSignal(dict)
    connection_changed = pyqtSignal(bool)

    def __init__(self, server_url: str):
        super().__init__()
        self.server_url = server_url.rstrip("/")
        self.running = True

    def run(self):
        while self.running:
            sleep_time = 0.5
            try:
                status_url = f"{self.server_url}/status"
                req = urllib.request.Request(status_url, headers={"User-Agent": "HermesMascot/1.0"})
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    raw_data = resp.read()
                    data = json.loads(raw_data.decode("utf-8"))
                    self.connection_changed.emit(True)
                    self.status_updated.emit(data)
                    is_active = data.get("active", False) or data.get("status") in ("thinking", "working")
                    sleep_time = 0.4 if is_active else 0.8
            except Exception:
                self.connection_changed.emit(False)
                sleep_time = 1.5

            slices = int(sleep_time / 0.1)
            for _ in range(slices):
                if not self.running:
                    break
                time.sleep(0.1)

    def stop(self):
        self.running = False


class StarParticle:
    def __init__(self, x, y, vx, vy, color, life=1.0):
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.color = color
        self.life = life
        self.max_life = life
        self.size = 5.0


class MascotWindow(QWidget):
    def __init__(self, config):
        super().__init__()
        self.cfg = config
        self.server_url = self.cfg.get("server_url", "http://192.168.0.127:8455")
        self.skin = self.cfg.get("skin", "hermes")
        self.sound_enabled = self.cfg.get("sound_enabled", False)
        self.scale = float(self.cfg.get("scale", 0.70))
        self.auto_hide_idle = self.cfg.get("auto_hide_idle", False)
        self.auto_launch_on_query = self.cfg.get("auto_launch_on_query", True)

        # Horizontal side-bubble layout:
        # [ Speech Bubble (x=10..246) ] <--- Tail --- [ Mascot (cx=310, cy=60) ]
        self.BASE_W = 380
        self.BASE_H = 120
        self.w_width = int(self.BASE_W * self.scale)
        self.w_height = int(self.BASE_H * self.scale)
        self.setFixedSize(self.w_width, self.w_height)

        # Window flags
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)

        # Load Full Official Spritesheets
        self.sheet_turtle = QImage(str(ASSETS_DIR / "turtle_spritesheet.webp"))
        self.sheet_codex = QImage(str(ASSETS_DIR / "codex_spritesheet.webp"))

        # Animation states
        self.anim_t = 0.0
        self.particles = []
        self.connected = False

        # Hop and cursor reactivity
        self.jump_frame = -1
        self.jump_timer = 0.0
        self.next_idle_jump = time.time() + random.uniform(6.0, 12.0)
        
        # Smooth cursor tracking (damping)
        self.target_look_x = 0.0
        self.target_look_y = 0.0
        self.look_offset_x = 0.0
        self.look_offset_y = 0.0

        # Activity State
        self.state_data = {
            "status": "idle",
            "active": False,
            "current_stage": "idle",
            "stage_label": "",
            "tool_action": "",
            "prompt": "",
            "user": "",
            "current_tool": None,
            "api_call_count": 0,
            "model": "",
            "elapsed_seconds": 0.0,
            "last_response_time": None,
            "queued_count": 0,
        }
        self.last_status = "idle"

        # Bubble visibility & side layout
        self.bubble_alpha = 0.0
        self.bubble_target_alpha = 0.0
        self.bubble_force_show = False
        self.bubble_collapsed = False
        self.hovered = False

        # Button rects in base coordinates
        self.collapse_btn_rect = QRectF(228, 16, 16, 16)
        self.expand_btn_rect = QRectF(252, 52, 16, 18)

        # Mouse dragging strictly on mascot body
        self.dragging_mascot = False
        self.drag_has_moved = False
        self.drag_start_global = QPoint()
        self.drag_window_start = QPoint()

        # Audio cooldown
        self.last_sound_time = 0.0

        # Animation timer (30 FPS = 33ms)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_animation)
        self.timer.start(33)

        # Worker thread
        self.worker = EventWorker(self.server_url)
        self.worker.status_updated.connect(self.on_status_update)
        self.worker.connection_changed.connect(self.on_connection_change)
        self.worker.start()

        # Positioning & Input Mask
        self.init_position()
        self.update_mask()

    def update_mask(self):
        """Update the OS window input mask so clicks outside visible elements pass through."""
        s = self.scale
        cx = 310.0
        cy = 60.0

        # Mascot circular/rounded body region
        mascot_rect = QRect(
            int((cx - 36) * s),
            int((cy - 40) * s),
            int(74 * s),
            int(82 * s)
        )
        region = QRegion(mascot_rect, QRegion.RegionType.Ellipse)

        # If speech bubble is visible (alpha > 0.05), include the speech bubble + tail in mask
        if self.bubble_alpha > 0.05:
            bubble_rect = QRect(
                int(10 * s),
                int(12 * s),
                int(248 * s),
                int(96 * s)
            )
            region = region.united(QRegion(bubble_rect))
        elif self.bubble_collapsed and (self.state_data.get("active") or self.hovered):
            # Include the small expand chevron button
            btn_rect = QRect(
                int(248 * s),
                int(48 * s),
                int(24 * s),
                int(26 * s)
            )
            region = region.united(QRegion(btn_rect))

        self.setMask(region)

    def init_position(self):
        screen = QApplication.primaryScreen()
        if not screen:
            self.move(1600, 900)
            return

        geom = screen.availableGeometry()
        saved_x = self.cfg.get("pos_x")
        saved_y = self.cfg.get("pos_y")

        if saved_x is not None and saved_x > 50 and saved_y is not None and saved_y > 50:
            x = max(geom.x(), min(saved_x, geom.x() + geom.width() - self.w_width))
            y = max(geom.y(), min(saved_y, geom.y() + geom.height() - self.w_height))
            self.move(int(x), int(y))
        else:
            x = geom.x() + geom.width() - self.w_width - 15
            y = geom.y() + geom.height() - self.w_height - 35
            self.move(int(x), int(y))

        self.raise_()
        QTimer.singleShot(200, lambda: make_window_sticky(int(self.winId())))
        QTimer.singleShot(1000, lambda: make_window_sticky(int(self.winId())))

    def on_connection_change(self, is_conn):
        self.connected = is_conn

    def on_status_update(self, data):
        prev_status = self.state_data.get("status", "idle")
        prev_active = self.state_data.get("active", False)
        prev_prompt = self.state_data.get("prompt", "").strip()
        self.state_data = data
        new_status = data.get("status", "idle")
        new_active = data.get("active", False)
        new_prompt = data.get("prompt", "").strip()

        # Uncollapse & unhide on incoming task
        if new_status in ("thinking", "working"):
            if self.auto_hide_idle and not self.isVisible():
                self.show()
                self.raise_()
                self.update_mask()
                QTimer.singleShot(200, lambda: make_window_sticky(int(self.winId())))
            if prev_status in ("idle", "done"):
                self.bubble_collapsed = False
                self.bubble_force_show = False
        elif new_status == "idle" and self.auto_hide_idle and self.bubble_alpha < 0.05:
            if self.isVisible():
                self.hide()

        # Hop on queued task transition
        if new_active and prev_active and prev_prompt and new_prompt != prev_prompt:
            self.start_jump()
            self.bubble_collapsed = False

        # Only celebrate when whole task finishes
        if new_status == "done" and (prev_status in ("thinking", "working") or prev_active):
            self.start_jump()
            self.spawn_stars()
            if self.sound_enabled:
                self.play_sound("complete")

        self.last_status = new_status

    def start_jump(self):
        self.jump_frame = 0
        self.jump_timer = time.time()

    def spawn_stars(self):
        cx = 310
        cy = 60
        for _ in range(14):
            ang = (time.time() * 10 + _ * 0.45) % (2 * math.pi)
            speed = 2.0 + (_ % 3) * 1.5
            vx = math.cos(ang) * speed
            vy = math.sin(ang) * speed - 1.0
            p = StarParticle(cx, cy, vx, vy, QColor(251, 191, 36), life=1.1)
            self.particles.append(p)

    def play_sound(self, kind="complete"):
        if not self.sound_enabled:
            return
        now = time.time()
        if now - self.last_sound_time < 20.0:
            return
        self.last_sound_time = now
        try:
            if kind == "complete":
                if os.path.exists("/usr/share/sounds/freedesktop/stereo/complete.oga"):
                    subprocess.Popen(["canberra-gtk-play", "-i", "complete"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.Popen(["canberra-gtk-play", "-i", "bell"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def update_animation(self):
        self.anim_t += 0.05
        now = time.time()

        # Random cute idle hops
        if self.jump_frame < 0 and now > self.next_idle_jump:
            self.start_jump()
            self.next_idle_jump = now + random.uniform(8.0, 16.0)

        # Advance jump frame (5 frames)
        if self.jump_frame >= 0:
            frame_elapsed = int((now - self.jump_timer) / 0.08)
            if frame_elapsed < 5:
                self.jump_frame = frame_elapsed
            else:
                self.jump_frame = -1

        # Smooth cursor look tracking (smooth lerp, NO jitter)
        self.look_offset_x += (self.target_look_x - self.look_offset_x) * 0.15
        self.look_offset_y += (self.target_look_y - self.look_offset_y) * 0.15

        # Particle physics
        alive_particles = []
        for p in self.particles:
            p.x += p.vx
            p.y += p.vy
            p.vy += 0.1
            p.life -= 0.03
            if p.life > 0:
                alive_particles.append(p)
        self.particles = alive_particles

        # STRICT BUBBLE VISIBILITY
        status = self.state_data.get("status", "idle")
        active = self.state_data.get("active", False)
        prompt = self.state_data.get("prompt", "").strip()

        if self.bubble_collapsed:
            self.bubble_target_alpha = 1.0 if self.bubble_force_show else 0.0
        elif self.bubble_force_show:
            self.bubble_target_alpha = 1.0
        elif active and status in ("thinking", "working") and prompt:
            self.bubble_target_alpha = 1.0
        else:
            self.bubble_target_alpha = 0.0

        old_alpha = self.bubble_alpha
        diff = self.bubble_target_alpha - self.bubble_alpha
        self.bubble_alpha += diff * 0.2

        # Update input mask when bubble appears or disappears
        if (old_alpha < 0.05 <= self.bubble_alpha) or (self.bubble_alpha < 0.05 <= old_alpha):
            self.update_mask()
            if self.auto_hide_idle and status == "idle" and self.bubble_alpha < 0.05:
                if self.isVisible():
                    self.hide()

        self.update()

    def mousePressEvent(self, event):
        pos_base = event.position() / self.scale
        cx = 310.0
        cy = 60.0
        dist_to_mascot = math.hypot(pos_base.x() - cx, pos_base.y() - cy)

        if event.button() == Qt.MouseButton.LeftButton:
            # 1. Click on collapse chevron (▶)
            if self.bubble_alpha > 0.3 and self.collapse_btn_rect.contains(pos_base):
                self.bubble_collapsed = True
                self.bubble_force_show = False
                self.update_mask()
                self.update()
                event.accept()
                return

            # 2. Click on expand chevron (◀)
            if self.bubble_alpha < 0.3 and self.expand_btn_rect.contains(pos_base):
                self.bubble_collapsed = False
                self.bubble_force_show = True
                self.update_mask()
                self.update()
                event.accept()
                return

            # 3. Hitbox for dragging / clicking is STRICTLY on the mascot body (radius 38)
            if dist_to_mascot <= 38.0:
                self.dragging_mascot = True
                self.drag_has_moved = False
                self.drag_start_global = event.globalPosition().toPoint()
                self.drag_window_start = self.frameGeometry().topLeft()
                event.accept()
                return

            # Click elsewhere inside visible bubble -> accept without dragging
            event.accept()

        elif event.button() == Qt.MouseButton.RightButton:
            self.show_context_menu(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event):
        pos_base = event.position() / self.scale
        cx = 310.0
        cy = 60.0
        dx = (pos_base.x() - cx) / 16.0
        dy = (pos_base.y() - cy) / 16.0
        self.target_look_x = max(-3.0, min(3.0, dx))
        self.target_look_y = max(-2.0, min(2.0, dy))

        if self.dragging_mascot and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self.drag_start_global
            if delta.manhattanLength() > 3:
                self.drag_has_moved = True
                self.move(self.drag_window_start + delta)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.dragging_mascot:
                self.dragging_mascot = False
                if self.drag_has_moved:
                    self.cfg["pos_x"] = self.x()
                    self.cfg["pos_y"] = self.y()
                    save_config(self.cfg)
                else:
                    # Pure click on mascot without drag -> Poke hop & toggle bubble!
                    self.start_jump()
                    self.bubble_collapsed = not self.bubble_collapsed
                    self.bubble_force_show = not self.bubble_collapsed
                    self.update_mask()
                    self.update()
                event.accept()

    def enterEvent(self, event):
        self.hovered = True
        if self.jump_frame < 0:
            self.start_jump()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hovered = False
        self.target_look_x = 0.0
        self.target_look_y = 0.0
        super().leaveEvent(event)

    def show_context_menu(self, global_pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #0f172a;
                color: #f1f5f9;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 6px;
                font-family: sans-serif;
                font-size: 13px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #0284c7;
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background-color: #1e293b;
                margin: 4px 10px;
            }
        """)

        skin_menu = menu.addMenu("🎭 Выбрать маскота")
        act_hermes = skin_menu.addAction("🪽 Гермес (Codex Blue)")
        act_hermes.setCheckable(True)
        act_hermes.setChecked(self.skin == "hermes")
        act_hermes.triggered.connect(lambda: self.set_skin("hermes"))

        act_turtle = skin_menu.addAction("🐢 Черепахабро (Emerald)")
        act_turtle.setCheckable(True)
        act_turtle.setChecked(self.skin == "turtlebro")
        act_turtle.triggered.connect(lambda: self.set_skin("turtlebro"))

        scale_menu = menu.addMenu("📏 Размер")
        for label, val in [("Мини (60%)", 0.60), ("Компактный (70%)", 0.70), ("Средний (85%)", 0.85), ("Обычный (100%)", 1.0)]:
            act_s = scale_menu.addAction(label)
            act_s.setCheckable(True)
            act_s.setChecked(abs(self.scale - val) < 0.05)
            act_s.triggered.connect(lambda chk, v=val: self.set_scale(v))

        menu.addSeparator()

        auto_menu = menu.addMenu("⚡ Автоматизация")
        act_autolaunch = auto_menu.addAction("🚀 Запускать маскота, если закрыт")
        act_autolaunch.setCheckable(True)
        act_autolaunch.setChecked(self.state_data.get("auto_launch", self.auto_launch_on_query))
        act_autolaunch.triggered.connect(self.toggle_server_autolaunch)

        act_autohide = auto_menu.addAction("🙈 Прятать в покое (показывать по запросу)")
        act_autohide.setCheckable(True)
        act_autohide.setChecked(self.auto_hide_idle)
        act_autohide.triggered.connect(self.toggle_auto_hide)

        menu.addSeparator()

        act_sound = menu.addAction("🔊 Звуковые сигналы")
        act_sound.setCheckable(True)
        act_sound.setChecked(self.sound_enabled)
        act_sound.triggered.connect(self.toggle_sound)

        menu.addSeparator()

        act_reset = menu.addAction("📌 Сбросить позицию")
        act_reset.triggered.connect(self.reset_position)

        act_reconn = menu.addAction("🔄 Переподключиться к серверу")
        act_reconn.triggered.connect(self.reconnect)

        menu.addSeparator()

        act_quit = menu.addAction("❌ Закрыть")
        act_quit.triggered.connect(QApplication.instance().quit)

        menu.exec(global_pos)

    def toggle_server_autolaunch(self):
        curr = self.state_data.get("auto_launch", self.auto_launch_on_query)
        new_val = not curr
        self.auto_launch_on_query = new_val
        self.cfg["auto_launch_on_query"] = new_val
        save_config(self.cfg)
        try:
            url = f"{self.server_url}/toggle_autolaunch?enabled={'true' if new_val else 'false'}"
            req = urllib.request.Request(url, headers={"User-Agent": "HermesMascot/1.0"})
            urllib.request.urlopen(req, timeout=1.5)
        except Exception:
            pass

    def toggle_auto_hide(self):
        self.auto_hide_idle = not self.auto_hide_idle
        self.cfg["auto_hide_idle"] = self.auto_hide_idle
        save_config(self.cfg)
        if self.auto_hide_idle and self.state_data.get("status", "idle") == "idle":
            self.hide()
        else:
            self.show()
            self.raise_()
            self.update_mask()
            QTimer.singleShot(200, lambda: make_window_sticky(int(self.winId())))

    def set_skin(self, skin_name):
        self.skin = skin_name
        self.cfg["skin"] = skin_name
        save_config(self.cfg)
        self.update()

    def set_scale(self, s):
        self.scale = s
        self.cfg["scale"] = s
        self.w_width = int(self.BASE_W * self.scale)
        self.w_height = int(self.BASE_H * self.scale)
        self.setFixedSize(self.w_width, self.w_height)
        save_config(self.cfg)
        self.reset_position()
        self.update_mask()
        self.update()

    def toggle_sound(self):
        self.sound_enabled = not self.sound_enabled
        self.cfg["sound_enabled"] = self.sound_enabled
        save_config(self.cfg)

    def reset_position(self):
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            x = geom.x() + geom.width() - self.w_width - 15
            y = geom.y() + geom.height() - self.w_height - 35
            self.move(int(x), int(y))
            self.cfg["pos_x"] = int(x)
            self.cfg["pos_y"] = int(y)
            save_config(self.cfg)

    def reconnect(self):
        if self.worker:
            self.worker.stop()
            self.worker.wait()
        self.worker = EventWorker(self.server_url)
        self.worker.status_updated.connect(self.on_status_update)
        self.worker.connection_changed.connect(self.on_connection_change)
        self.worker.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.scale(self.scale, self.scale)

        # Draw Side Speech Bubble if alpha > 0
        if self.bubble_alpha > 0.01:
            self.draw_speech_bubble(painter)
        elif self.bubble_collapsed and (self.state_data.get("active") or self.hovered):
            self.draw_expand_button(painter)

        # Draw Official Animated Mascot
        self.draw_mascot(painter)
        self.draw_particles(painter)

    def draw_expand_button(self, painter: QPainter):
        btn = self.expand_btn_rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(56, 189, 248, 120), 1))
        painter.setBrush(QColor(15, 23, 42, 220))
        painter.drawRoundedRect(btn, 4, 4)

        # Chevron ◀
        chev = QPainterPath()
        chev.moveTo(btn.x() + 11, btn.y() + 5)
        chev.lineTo(btn.x() + 5, btn.y() + 9)
        chev.lineTo(btn.x() + 11, btn.y() + 13)
        painter.strokePath(chev, make_pen(QColor(56, 189, 248), 1.6, Qt.PenCapStyle.RoundCap))
        painter.restore()

    def draw_speech_bubble(self, painter: QPainter):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setOpacity(min(1.0, max(0.0, self.bubble_alpha)))

        bubble_x = 10
        bubble_y = 12
        bubble_w = 236
        bubble_h = 96

        rect = QRectF(bubble_x, bubble_y, bubble_w, bubble_h)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)

        # Side Tail pointing rightwards directly to mascot (cx = 310, cy = 60)
        tail = QPainterPath()
        tail.moveTo(bubble_x + bubble_w, 52)
        tail.lineTo(bubble_x + bubble_w + 14, 60)
        tail.lineTo(bubble_x + bubble_w, 68)
        tail.closeSubpath()
        path.addPath(tail)

        grad = QLinearGradient(0, bubble_y, 0, bubble_y + bubble_h)
        grad.setColorAt(0.0, QColor(15, 23, 42, 248))
        grad.setColorAt(1.0, QColor(11, 15, 25, 252))
        painter.setBrush(QBrush(grad))

        status = self.state_data.get("status", "idle")
        if status in ("thinking", "working"):
            pen_color = QColor(14, 165, 233, 200)
        elif status == "done":
            pen_color = QColor(34, 197, 94, 200)
        else:
            pen_color = QColor(51, 65, 85, 180)

        painter.setPen(QPen(pen_color, 1.5))
        painter.drawPath(path)

        # 1. Header: Title + Status Pill + Collapse Button (Zero overlap layout)
        font_head = QFont("sans-serif", 9, QFont.Weight.Bold)
        painter.setFont(font_head)
        painter.setPen(QColor(241, 245, 249))
        title_text = "Hermes"
        title_w = painter.fontMetrics().horizontalAdvance(title_text)
        painter.drawText(int(bubble_x + 10), int(bubble_y + 20), title_text)

        # Collapse chevron button (▶)
        btn_rect = self.collapse_btn_rect
        painter.setPen(QPen(QColor(56, 189, 248, 80), 1))
        painter.setBrush(QColor(30, 41, 59, 160))
        painter.drawRoundedRect(btn_rect, 4, 4)

        chev = QPainterPath()
        chev.moveTo(btn_rect.x() + 5, btn_rect.y() + 5)
        chev.lineTo(btn_rect.x() + 11, btn_rect.y() + 8)
        chev.lineTo(btn_rect.x() + 5, btn_rect.y() + 11)
        painter.strokePath(chev, make_pen(QColor(148, 163, 184), 1.5, Qt.PenCapStyle.RoundCap))

        # Status badge / stage label
        stage_label = self.state_data.get("stage_label")
        if not self.connected:
            badge_text = "⚠ Офлайн"
            badge_fg = QColor(239, 68, 68)
            badge_bg = QColor(239, 68, 68, 35)
        elif stage_label:
            badge_text = stage_label
            badge_fg = QColor(56, 189, 248) if status == "thinking" else QColor(251, 191, 36)
            badge_bg = QColor(14, 165, 233, 35) if status == "thinking" else QColor(245, 158, 11, 35)
        elif status == "done":
            dur = self.state_data.get("last_response_time")
            dur_str = f" ({dur:.1f}s)" if dur else ""
            badge_text = f"✨ Готово{dur_str}"
            badge_fg = QColor(74, 222, 128)
            badge_bg = QColor(34, 197, 94, 35)
        else:
            badge_text = "● В сети"
            badge_fg = QColor(148, 163, 184)
            badge_bg = QColor(51, 65, 85, 40)

        font_badge = QFont("sans-serif", 8, QFont.Weight.DemiBold)
        painter.setFont(font_badge)
        metrics_b = painter.fontMetrics()

        # Available space between title and button with safe margins
        title_end_x = bubble_x + 10 + title_w
        max_badge_w = (btn_rect.x() - 10) - (title_end_x + 14)
        if max_badge_w > 30:
            badge_text = metrics_b.elidedText(badge_text, Qt.TextElideMode.ElideRight, int(max_badge_w - 12))
            tw = metrics_b.horizontalAdvance(badge_text)
            pill_w = tw + 10
            pill_h = 16
            pill_x = int(btn_rect.x() - 8 - pill_w)
            pill_y = int(bubble_y + 8)

            # Draw subtle pill background
            painter.setPen(QPen(QColor(badge_fg.red(), badge_fg.green(), badge_fg.blue(), 70), 1))
            painter.setBrush(badge_bg)
            painter.drawRoundedRect(QRectF(pill_x, pill_y, pill_w, pill_h), 4.0, 4.0)

            # Draw badge text inside pill
            painter.setPen(badge_fg)
            painter.drawText(pill_x + 5, int(bubble_y + 20), badge_text)

        # 2. User Prompt (compact line)
        prompt = self.state_data.get("prompt", "").strip()
        if prompt:
            font_prompt = QFont("sans-serif", 8)
            painter.setFont(font_prompt)
            painter.setPen(QColor(148, 163, 184))
            metrics_p = painter.fontMetrics()
            max_line_w = bubble_w - 20
            prompt_single = metrics_p.elidedText(prompt, Qt.TextElideMode.ElideRight, int(max_line_w))
            painter.drawText(int(bubble_x + 10), int(bubble_y + 38), prompt_single)

        # 3. Active Tool Command / File / Action Box (Telegram style)
        tool_action = self.state_data.get("tool_action", "").strip()
        if status in ("thinking", "working") and not (status == "done"):
            box_x = int(bubble_x + 8)
            box_y = int(bubble_y + 46)
            box_w = int(bubble_w - 16)
            box_h = 20

            # Dark code container
            painter.setPen(QPen(QColor(56, 189, 248, 40), 1))
            painter.setBrush(QColor(2, 6, 23, 200))
            painter.drawRoundedRect(QRectF(box_x, box_y, box_w, box_h), 4.0, 4.0)

            font_code = QFont("monospace", 7, QFont.Weight.Medium)
            font_code.setStyleHint(QFont.StyleHint.Monospace)
            painter.setFont(font_code)
            metrics_c = painter.fontMetrics()

            display_text = tool_action if tool_action else "Обдумывание ответа..."
            display_text = metrics_c.elidedText(display_text, Qt.TextElideMode.ElideRight, int(box_w - 12))

            color_code = QColor(56, 189, 248) if stage in ("terminal", "files") else QColor(251, 191, 36)
            painter.setPen(color_code)
            painter.drawText(box_x + 6, box_y + 14, display_text)

        elif status == "done":
            font_done = QFont("sans-serif", 8, QFont.Weight.Medium)
            painter.setFont(font_done)
            painter.setPen(QColor(74, 222, 128))
            painter.drawText(int(bubble_x + 10), int(bubble_y + 58), "✓ Ответ отправлен в Telegram")

        # 4. Details Row (Timing, API calls, Queue)
        api_cnt = self.state_data.get("api_call_count", 0)
        elapsed = self.state_data.get("elapsed_seconds", 0.0)
        user_name = self.state_data.get("user") or "черепахабро⁹²"
        queued_count = self.state_data.get("queued_count", 0)

        detail_text = ""
        if status in ("thinking", "working"):
            detail_text = f"👤 {user_name} • ⏱ {elapsed:.0f}s • API #{api_cnt}"
            if queued_count > 0:
                detail_text += f" • 📥 В очереди: {queued_count}"
        elif status == "done":
            dur = self.state_data.get("last_response_time")
            dur_s = f"{dur:.1f}s" if dur else f"{elapsed:.0f}s"
            detail_text = f"👤 {user_name} • Завершено за {dur_s}"

        if detail_text:
            font_det = QFont("sans-serif", 7)
            painter.setFont(font_det)
            painter.setPen(QColor(100, 116, 139))
            painter.drawText(int(bubble_x + 10), int(bubble_y + 80), detail_text)

        # 4. Animated progress bar
        if status in ("thinking", "working"):
            bar_y = bubble_y + bubble_h - 7
            bar_w = bubble_w - 20
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(30, 41, 59))
            painter.drawRoundedRect(int(bubble_x + 10), int(bar_y), int(bar_w), 3, 1, 1)

            shimmer_pos = (math.sin(self.anim_t * 3) + 1.0) * 0.5 * (bar_w - 50)
            shimmer_grad = QLinearGradient(bubble_x + 10 + shimmer_pos, 0,
                                           bubble_x + 10 + shimmer_pos + 50, 0)
            shimmer_grad.setColorAt(0.0, QColor(14, 165, 233, 0))
            shimmer_grad.setColorAt(0.5, QColor(56, 189, 248, 255))
            shimmer_grad.setColorAt(1.0, QColor(14, 165, 233, 0))
            painter.setBrush(QBrush(shimmer_grad))
            painter.drawRoundedRect(int(bubble_x + 10 + shimmer_pos), int(bar_y), 50, 3, 1, 1)

        painter.restore()

    def draw_mascot(self, painter: QPainter):
        """Draw authentic animated spritesheet mascot with stage-specific motions."""
        painter.save()
        sheet = self.sheet_turtle if self.skin == "turtlebro" else self.sheet_codex
        if sheet.isNull():
            painter.restore()
            return

        cx = 310 + self.look_offset_x
        cy_base = 60 + self.look_offset_y

        stage = self.state_data.get("current_stage", "idle")
        status = self.state_data.get("status", "idle")

        # Pick authentic animation row based on STAGE:
        if self.jump_frame >= 0:
            # Jumping / hopping
            row = ROW_JUMP
            col = min(4, self.jump_frame)
        elif status == "done":
            # Waving celebration!
            row = ROW_WAVE
            col = int(self.anim_t * 6) % 4
        elif stage == "terminal":
            # Fast energetic running/typing
            row = ROW_WORKING
            col = int(self.anim_t * 8) % 6
        elif stage == "search":
            # Walking and looking around
            row = ROW_SEARCH
            col = int(self.anim_t * 6) % 8
        elif stage == "files":
            # Reviewing / code inspection with hand on chin
            row = ROW_REVIEW
            col = int(self.anim_t * 4) % 6
        elif stage == "thinking":
            # Thinking / pondering
            row = ROW_WAITING
            col = int(self.anim_t * 4) % 6
        else:
            # Idle calm grounded breathing (Row 0)
            row = ROW_IDLE
            col = int(self.anim_t * 3.5) % 6

        src_rect = QRectF(float(col * FRAME_W), float(row * FRAME_H), float(FRAME_W), float(FRAME_H))

        target_w = 68.0
        target_h = 74.0
        target_rect = QRectF(cx - target_w / 2, cy_base - target_h / 2, target_w, target_h)

        # Draw the clean spritesheet frame (clean natural eyes, ZERO glitchy manual overlay!)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(target_rect, sheet, src_rect)

        painter.restore()

    def draw_particles(self, painter: QPainter):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for p in self.particles:
            alpha = int(255 * (p.life / p.max_life))
            color = QColor(p.color.red(), p.color.green(), p.color.blue(), alpha)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)

            s = int(p.size * (p.life / p.max_life))
            if s > 0:
                painter.drawEllipse(int(p.x), int(p.y), s, s)
        painter.restore()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("HermesMascot")

    config = load_config()
    mascot = MascotWindow(config)
    mascot.show()

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
