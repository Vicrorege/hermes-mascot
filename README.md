# Hermes Mascot 🪽

Desktop companion and animated status mascot for **Hermes Agent** Telegram Gateway on Linux (Wayland / X11).

Floats in the screen corner across all virtual desktops, responds to Telegram agent activity in real-time, reacts to mouse cursor movement, and hops playfully when poked or when tasks complete.

## Features

- **Real-Time Gateway Sync**: Live integration with Hermes Agent Telegram Gateway (`thinking`, `working`, `done`, `idle`).
- **Smooth Animation States**:
  - Grounded idle breathing cycle.
  - Interactive hop physics on mouse click, hover poke, and task completion.
  - Head and eye direction tracking your mouse cursor.
  - Active review / working animation cycles during tool execution.
  - Star particle burst celebration when your task finishes.
- **Side Speech Bubble**:
  - Expands horizontally to the left, allowing the mascot to sit flush against the right edge of your screen.
  - Displays the active prompt, executing tool, elapsed time, and API turn count.
  - Collapse / expand chevron toggle (`▶` / `◀`).
  - Automatically hides completely during idle to stay unobtrusive.
- **Desktop Integration**:
  - Always-on-top window (`WindowStaysOnTopHint`).
  - Sticky across all GNOME / Wayland virtual workspaces (`_NET_WM_DESKTOP`).
  - Movable by dragging anywhere with the mouse.
  - Right-click context menu: scale (60% to 100%), skins (`Codex Blue`, `Emerald`), sound alerts toggle.

## Architecture

1. **Server Bridge (`server.py`)**:
   Runs on the server alongside Hermes Agent Gateway, streaming real-time status and turn events over HTTP.
2. **Desktop Client (`mascot.py`)**:
   Lightweight PyQt6 application running on the client desktop, loading multi-frame spritesheets.

## Installation & Usage

```bash
# Launch mascot
hermes-mascot

# Control
hermes-mascot stop
hermes-mascot restart
```

## License

[MIT](LICENSE) © 2026 Vicrorege
