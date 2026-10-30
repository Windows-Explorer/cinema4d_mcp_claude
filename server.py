import base64
import json
import os
import socket
import struct
import sys

HOST = os.environ.get("C4D_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("C4D_MCP_PORT", "55104"))
MAX_TEXT = 40000
MAX_IMAGE = 4 * 1024 * 1024

INSTRUCTIONS = (
    "Controls a running Cinema 4D 2023 through the c4d_mcp bridge plugin. "
    "Object refs are a name ('Cube') or a hierarchy path ('/Group/Cube'); duplicate siblings are 'Cube#2'. "
    "Other targets: 'material:Name', 'render', 'document', 'ObjectRef@TagIndexOrName'. "
    "Units are scene units (cm by default), rotations are HPB in degrees, colors are '#rrggbb' or [r,g,b] in 0..1. "
    "Every edit is one undo step (see the undo tool). "
    "Parameter ids are C4D symbols such as PRIM_CUBE_LEN: list them with get_parameters, find any constant with search_symbols. "
    "Use execute_python for anything the other tools do not cover."
)


def p(base, description):
    return {**base, "description": description}


STR = {"type": "string"}
NUM = {"type": "number"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}
VEC3 = {"type": "array", "items": NUM, "minItems": 3, "maxItems": 3}
STRS = {"type": "array", "items": STR}

OBJ = p(STR, "Object ref: name or hierarchy path like '/Group/Cube' (duplicate siblings: 'Cube#2')")
TARGET = p(STR, "Object ref, 'material:Name', 'render', 'document' or 'ObjectRef@TagIndexOrName'")
POSITION = p(VEC3, "Position [x,y,z]")
ROTATION = p(VEC3, "Rotation [H,P,B] in degrees")
SCALE = p(VEC3, "Scale [x,y,z]")
SPACE = p({"type": "string", "enum": ["local", "global"]}, "Coordinate space for position/rotation/scale, default local")
LOOK_AT = p(VEC3, "Global point the object's +Z axis should face (cameras, lights)")
PARENT = p(STR, "Parent object ref, omit for the scene root")
PARAMS = p(
    {"type": "object", "additionalProperties": True},
    "Parameter id -> value. Ids come from get_parameters, a component is 'ID.x' (e.g. 'PRIM_CUBE_LEN.x', 'rotation.y'). "
    "Vectors [x,y,z], colors '#rrggbb' or [r,g,b], angles in degrees, enums as numbers or symbol names, links as an object ref or 'material:Name'",
)

TOOLS = [
    {
        "name": "scene_info", "read_only": True,
        "description": "Version, document path, fps, frame range, current frame, render size and renderer, object and material counts, selected objects.",
        "props": {}, "required": [],
    },
    {
        "name": "list_objects", "read_only": True,
        "description": "Flat listing of the object hierarchy with ready-to-use paths, C4D type symbols and depth.",
        "props": {
            "root": p(STR, "List only below this object"),
            "depth": p(INT, "Maximum depth, unlimited if omitted"),
            "limit": p(INT, "Maximum entries, default 300"),
        },
        "required": [],
    },
    {
        "name": "get_object", "read_only": True,
        "description": "Details of one object: type, local and global transform (degrees), bounding box size, children, tags, visibility, animation flag, point and polygon counts.",
        "props": {"target": OBJ}, "required": ["target"],
    },
    {
        "name": "get_parameters", "read_only": True,
        "description": "List the parameters (id, name, type, current value) of an object, tag, material, the render settings or the document. Use filter to search by id or name.",
        "props": {
            "target": TARGET,
            "filter": p(STR, "Case-insensitive substring of the id or the name"),
            "limit": p(INT, "Maximum entries, default 100"),
        },
        "required": ["target"],
    },
    {
        "name": "set_parameters",
        "description": "Set parameters on an object, tag, material, the render settings or the document. Returns which keys were set and which failed with a reason.",
        "props": {"target": TARGET, "params": PARAMS}, "required": ["target", "params"],
    },
    {
        "name": "create_object",
        "description": (
            "Create an object. type is a friendly name (cube, sphere, cylinder, cone, torus, plane, disc, tube, capsule, pyramid, platonic, "
            "floor, sky, null, camera, light, circle, rectangle, star, extrude, lathe, sweep, loft, boole, symmetry, array, subdivision_surface, "
            "instance, connect, cloner, bend, twist, bulge, shear, taper), a C4D symbol (Ocube) or a numeric id. Names are made unique (Cube, Cube.1)."
        ),
        "props": {
            "type": p(STR, "Object type"), "name": p(STR, "Object name"), "parent": PARENT,
            "position": POSITION, "rotation": ROTATION, "scale": p({"anyOf": [VEC3, NUM]}, "Scale [x,y,z] or a uniform number"),
            "space": SPACE, "look_at": LOOK_AT, "params": PARAMS,
        },
        "required": ["type"],
    },
    {
        "name": "create_mesh",
        "description": "Create an editable polygon object from raw data. Polygons are lists of 3 or 4 vertex indices, uvs has one list of 3 or 4 [u,v] pairs per polygon. Flat shaded unless smooth is true.",
        "props": {
            "name": p(STR, "Object name"),
            "vertices": p({"type": "array", "items": VEC3}, "Vertex positions"),
            "polygons": p({"type": "array", "items": {"type": "array", "items": INT}}, "Vertex index lists"),
            "uvs": p({"type": "array", "items": {"type": "array", "items": {"type": "array", "items": NUM}}}, "Per-polygon UVs"),
            "smooth": p(BOOL, "Add a Phong tag"),
            "parent": PARENT, "position": POSITION, "rotation": ROTATION, "scale": SCALE, "space": SPACE,
        },
        "required": ["vertices", "polygons"],
    },
    {
        "name": "set_transform",
        "description": "Move, rotate or scale an object. Omitted fields stay unchanged. look_at is applied last.",
        "props": {"target": OBJ, "position": POSITION, "rotation": ROTATION, "scale": p({"anyOf": [VEC3, NUM]}, "Scale [x,y,z] or a uniform number"), "space": SPACE, "look_at": LOOK_AT},
        "required": ["target"],
    },
    {
        "name": "delete_objects",
        "description": "Delete objects together with their children.",
        "props": {"targets": p({"type": "array", "items": STR}, "Object refs")}, "required": ["targets"],
    },
    {
        "name": "duplicate_object",
        "description": "Clone an object with children and tags next to the original. With offset every clone is shifted from the previous one.",
        "props": {
            "target": OBJ, "count": p(INT, "Number of clones, default 1"),
            "offset": p(VEC3, "Local position step between clones"), "name": p(STR, "Base name for the clones"),
        },
        "required": ["target"],
    },
    {
        "name": "reparent_object",
        "description": "Move an object under another object, or to the scene root when parent is omitted.",
        "props": {"target": OBJ, "parent": PARENT, "keep_transform": p(BOOL, "Keep the global transform, default true")},
        "required": ["target"],
    },
    {
        "name": "add_tag",
        "description": "Add a tag to an object. type is a friendly name (texture, phong, protection, look_at), a C4D symbol (Tphong) or an id. Returns the tag target ('/Cube@0') usable with get_parameters and set_parameters.",
        "props": {"target": OBJ, "type": p(STR, "Tag type"), "name": p(STR, "Tag name"), "params": PARAMS},
        "required": ["target", "type"],
    },
    {
        "name": "list_materials", "read_only": True,
        "description": "Materials of the document with their type and color.",
        "props": {}, "required": [],
    },
    {
        "name": "create_material",
        "description": "Create a standard material. color applies to the Color channel, texture is an image file path loaded through a Bitmap shader, luminance is a glow color, transparency is 0..1. params accepts any other material parameter.",
        "props": {
            "name": p(STR, "Material name"), "color": p({"anyOf": [STR, VEC3]}, "'#rrggbb' or [r,g,b] in 0..1"),
            "texture": p(STR, "Absolute path of an image"), "luminance": p({"anyOf": [STR, VEC3]}, "Glow color"),
            "transparency": p(NUM, "Transparency amount 0..1"), "params": PARAMS,
        },
        "required": [],
    },
    {
        "name": "assign_material",
        "description": "Assign a material to objects through a Texture tag, reusing an existing texture tag when there is one.",
        "props": {
            "targets": p({"type": "array", "items": STR}, "Object refs"), "material": p(STR, "Material name"),
            "projection": p({"type": "string", "enum": ["uvw", "spherical", "cylindrical", "flat", "cubic", "frontal"]}, "Projection, the tag default if omitted"),
        },
        "required": ["targets", "material"],
    },
    {
        "name": "set_keyframes",
        "description": (
            "Add or replace keyframes on a parameter. param is 'position', 'rotation' (degrees), 'scale', any parameter id from get_parameters, "
            "or a component such as 'position.x'. For a vector parameter without component every key value is [x,y,z]. Frames use the document fps."
        ),
        "props": {
            "target": TARGET, "param": p(STR, "Parameter id or shorthand"),
            "keys": p({"type": "array", "items": {"type": "object", "properties": {"frame": NUM, "value": {}}, "required": ["frame", "value"]}}, "Keyframes {frame, value}"),
            "interpolation": p({"type": "string", "enum": ["spline", "linear", "step"]}, "Default spline"),
        },
        "required": ["target", "param", "keys"],
    },
    {
        "name": "clear_animation",
        "description": "Remove the animation tracks of an object, or only those of one parameter.",
        "props": {"target": TARGET, "param": p(STR, "Parameter id or shorthand")}, "required": ["target"],
    },
    {
        "name": "set_timeline",
        "description": "Set fps, the start and end frame and the current frame of the document.",
        "props": {"fps": INT, "start": NUM, "end": NUM, "frame": NUM}, "required": [],
    },
    {
        "name": "render_image", "timeout": 600,
        "description": (
            "Render the current document to an image and return it. engine: viewport (fast, default), standard, physical, redshift or current "
            "(whatever the render settings use). The scene time is restored afterwards. Blocks the Cinema 4D UI while rendering."
        ),
        "props": {
            "width": p(INT, "Default 960"), "height": p(INT, "Default 540"), "frame": NUM,
            "engine": {"type": "string", "enum": ["viewport", "standard", "physical", "redshift", "current"]},
            "path": p(STR, "Output file (.png or .jpg); a temporary file is used and deleted when omitted"),
            "embed": p(BOOL, "Return the image to the model, default true"),
        },
        "required": [],
    },
    {
        "name": "new_scene",
        "description": "Create an empty document and make it the active one.",
        "props": {}, "required": [],
    },
    {
        "name": "open_scene", "timeout": 300,
        "description": "Open a scene file in a new document, or with merge import it into the current document (c4d, fbx, obj, glTF, abc and the other formats Cinema 4D reads).",
        "props": {"path": p(STR, "Absolute path"), "merge": p(BOOL, "Merge into the current document")}, "required": ["path"],
    },
    {
        "name": "save_scene", "timeout": 300,
        "description": "Save or export the document. The format comes from the extension: .c4d, .fbx, .obj, .abc, .gltf, .glb, .stl, .dae, .usd, .3ds, .dxf, .wrl. Exporters use their last used settings. Without path the current file is overwritten.",
        "props": {"path": p(STR, "Absolute path")}, "required": [],
    },
    {
        "name": "undo",
        "description": "Undo (or redo) the last edits made in Cinema 4D.",
        "props": {"steps": p(INT, "Default 1"), "redo": p(BOOL, "Redo instead of undo")}, "required": [],
    },
    {
        "name": "execute_python", "timeout": 600,
        "description": "Run Python inside Cinema 4D on the main thread with c4d, doc (active document) and op (active object) in scope, like the Script Manager. Printed output is returned, assign result to return a value. Not wrapped in undo.",
        "props": {"code": p(STR, "Python source")}, "required": ["code"],
    },
    {
        "name": "search_symbols", "read_only": True,
        "description": "Find c4d module integer constants by case-insensitive substring (object types Ocube, parameter ids PRIM_CUBE_LEN, enums). Returns [name, value] pairs, prefix matches first.",
        "props": {"query": STR, "limit": p(INT, "Default 60")}, "required": ["query"],
    },
]

BY_NAME = {t["name"]: t for t in TOOLS}
TOOL_LIST = [
    {
        "name": t["name"],
        "description": t["description"],
        "inputSchema": {"type": "object", "properties": t["props"], "required": t["required"]},
        "annotations": {"readOnlyHint": t.get("read_only", False)},
    }
    for t in TOOLS
]


def recv_exact(sock, size):
    chunks = []
    while size:
        chunk = sock.recv(min(size, 1 << 20))
        if not chunk:
            raise ConnectionError("Cinema 4D closed the connection")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def call_bridge(cmd, args, timeout):
    payload = json.dumps({"cmd": cmd, "args": args, "timeout": timeout}).encode("utf-8")
    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        sock.settimeout(timeout + 15)
        sock.sendall(struct.pack(">I", len(payload)) + payload)
        (size,) = struct.unpack(">I", recv_exact(sock, 4))
        return json.loads(recv_exact(sock, size))


def text_result(text, error=False):
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + f"\n... truncated, {len(text) - MAX_TEXT} more characters"
    return {"content": [{"type": "text", "text": text}], "isError": error}


def call_tool(params):
    name = params.get("name")
    args = params.get("arguments") or {}
    tool = BY_NAME.get(name)
    if tool is None:
        return text_result(f"unknown tool {name}", True)
    timeout = tool.get("timeout", 60)
    try:
        response = call_bridge(name, args, timeout)
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        return text_result(
            f"Cannot reach the Cinema 4D bridge on {HOST}:{PORT} ({e}). "
            "Start Cinema 4D 2023 and check that the c4d_mcp plugin is loaded (Console shows '[c4d_mcp] listening').",
            True,
        )
    if not response.get("ok"):
        message = response.get("error", "unknown error")
        trace = response.get("trace", "").strip()
        if trace:
            message += "\n" + (trace if name == "execute_python" else trace.splitlines()[-1])
        return text_result(message, True)
    result = response["result"]
    if name == "execute_python":
        text = result.get("output", "")
        if result.get("result") is not None:
            text += ("\n" if text else "") + "result: " + json.dumps(result["result"], ensure_ascii=False)
        return text_result(text or "(no output)")
    content = []
    if name == "render_image":
        path = result["path"]
        summary = {k: v for k, v in result.items() if k != "temporary"}
        if args.get("embed", True) and os.path.getsize(path) <= MAX_IMAGE:
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            mime = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
            content.append({"type": "image", "data": data, "mimeType": mime})
            if result.get("temporary"):
                os.remove(path)
                summary.pop("path")
        result = summary
    content.insert(0, {"type": "text", "text": json.dumps(result, ensure_ascii=False, separators=(",", ":"))[:MAX_TEXT]})
    return {"content": content, "isError": False}


def handle(message):
    method = message.get("method")
    if method is None or "id" not in message:
        return None
    params = message.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "cinema4d", "version": "1.0.0"},
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOL_LIST}
    elif method == "tools/call":
        result = call_tool(params)
    else:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except Exception as e:
            print(f"c4d_mcp server error: {e}", file=sys.stderr)
            continue
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
