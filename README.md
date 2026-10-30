# Cinema 4D MCP

An [MCP](https://modelcontextprotocol.io) server that lets Claude drive Cinema 4D: build and edit scenes, materials and animation, render images, and run Python inside the open document.

Built and tested against Cinema 4D 2023 (Python 3.10). No third-party packages are needed.

## How it works

```
Claude  <--stdio-->  server.py  <--TCP 127.0.0.1:55104-->  c4d_mcp.pyp (inside Cinema 4D)
```

- `c4d_mcp.pyp` is a Cinema 4D plugin. It listens on `127.0.0.1:55104` and runs every command on the main thread, so the scene is never touched from a worker thread. Each command is one undo step.
- `server.py` is the MCP server (stdio, standard library only). It exposes 25 tools and forwards them to the plugin.

The plugin only listens on localhost.

## Requirements

- Cinema 4D 2023 (other versions with a Python 3 API may work but are untested)
- Python 3 on `PATH` for `server.py` (this is separate from the Python bundled with Cinema 4D)
- An MCP client, for example Claude Code or Claude Desktop

## Install

### 1. Install the plugin into Cinema 4D

Copy `c4d_mcp.pyp` into a `plugins` folder that Cinema 4D scans, inside its own subfolder.

User preferences folder (recommended). Open it from Cinema 4D with Edit > Preferences > Open Preferences Folder, or find it here:

```
%APPDATA%\Maxon\Maxon Cinema 4D 2023_<hash>\plugins\c4d_mcp\c4d_mcp.pyp
```

Or the installation folder:

```
C:\Program Files\Maxon Cinema 4D 2023\plugins\c4d_mcp\c4d_mcp.pyp
```

Restart Cinema 4D completely. Open the console (Extensions > Console, or Shift+F10). You should see:

```
[c4d_mcp] listening on 127.0.0.1:55104
```

### 2. Register the server with your MCP client

Use the absolute path to `server.py` on your machine.

**Claude Code**

```bash
claude mcp add --scope user cinema4d -- python /path/to/cinema4d_mcp_claude/server.py
```

Check it with `claude mcp get cinema4d`. To remove it: `claude mcp remove cinema4d -s user`.

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "cinema4d": {
      "command": "python",
      "args": ["C:\\path\\to\\cinema4d_mcp_claude\\server.py"]
    }
  }
}
```

Restart the client after changing its config. MCP servers are loaded when a session starts, so an already open session will not see the new server.

### 3. Try it

With Cinema 4D running and the plugin loaded, start a new session and ask:

> Create a 100 cm cube in the scene and render it.

If the Cinema 4D window is closed, tool calls return "Cannot reach the Cinema 4D bridge". The status "Connected" in the client only means `server.py` started; the tools work while Cinema 4D is open with the plugin loaded.

## Tools

| Group | Tools |
| --- | --- |
| Scene | `scene_info`, `new_scene`, `open_scene`, `save_scene`, `set_timeline`, `undo` |
| Objects | `list_objects`, `get_object`, `create_object`, `create_mesh`, `set_transform`, `duplicate_object`, `reparent_object`, `delete_objects` |
| Parameters | `get_parameters`, `set_parameters`, `search_symbols` |
| Materials and tags | `list_materials`, `create_material`, `assign_material`, `add_tag` |
| Animation | `set_keyframes`, `clear_animation` |
| Rendering | `render_image` (returns the image to Claude) |
| Scripting | `execute_python` (runs Python in the open document with `c4d` and `doc` available) |

Conventions:

- An object is referenced by name or path, for example `Cube` or `/Group/Cube`. Duplicate names are written `Cube#2`.
- Rotations are HPB in degrees.
- Colors are `#rrggbb` or `[r, g, b]` in the range 0..1.
- Parameter targets can also be `material:Name`, `render`, `document` or `ObjectRef@Tag`.
- `save_scene` picks the format from the extension: `.c4d`, `.fbx`, `.obj`, `.glb`, `.stl`.

`execute_python` runs arbitrary code with full access to Cinema 4D and the file system. Only connect a client you trust.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `C4D_MCP_PORT` | `55104` | TCP port of the bridge. It must be the same for Cinema 4D and for `server.py`. |

If the port is taken (for example by a second Cinema 4D instance), the plugin cannot start its server. Close the other instance or change the port in both places.

## Updating

The plugin is loaded from the copy in the `plugins` folder, not from this repository. After editing `c4d_mcp.pyp`, copy it over the installed one and restart Cinema 4D. Changes to `server.py` apply to new sessions.

## Troubleshooting

- No `[c4d_mcp] listening` line in the console: check that the file is in `plugins\c4d_mcp\` (a subfolder, not the `plugins` root) and that the console shows no Python error from the plugin.
- Tools are missing in the client: start a new session; check `claude mcp get cinema4d`.
- "Cannot reach the Cinema 4D bridge": Cinema 4D is closed, the plugin did not load, or the ports differ.
- Commands hang until you click in the Cinema 4D window: the plugin wakes the main thread with an event and a 30 ms timer; if you see this, please report it with your Cinema 4D build number.

## Status

Verified headless with `c4dpy` (all command handlers and an end-to-end MCP session). Not yet verified: behavior in the live GUI event loop, the Redshift renderer, and the glTF/FBX importer dialogs.
