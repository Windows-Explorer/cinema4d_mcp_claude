import contextlib
import io
import json
import math
import os
import queue
import socket
import struct
import tempfile
import threading
import time
import traceback

import c4d

PLUGIN_ID = 1000008
HOST = "127.0.0.1"
PORT = int(os.environ.get("C4D_MCP_PORT", "55104"))
MAX_FRAME = 64 * 1024 * 1024

COMMANDS = {}
jobs = queue.Queue()
state = {"busy": False}
symbols = {}

OBJECT_ALIASES = {
    "cube": "Ocube", "sphere": "Osphere", "cylinder": "Ocylinder", "cone": "Ocone", "torus": "Otorus",
    "plane": "Oplane", "disc": "Odisc", "tube": "Otube", "capsule": "Ocapsule", "pyramid": "Opyramid",
    "platonic": "Oplatonic", "floor": "Ofloor", "sky": "Osky", "null": "Onull", "camera": "Ocamera",
    "light": "Olight", "circle": "Osplinecircle", "rectangle": "Osplinerectangle", "star": "Osplinestar",
    "polygon": "Opolygon", "extrude": "Oextrude", "lathe": "Olathe", "sweep": "Osweep", "loft": "Oloft",
    "boole": "Oboole", "symmetry": "Osymmetry", "array": "Oarray", "subdivision_surface": "Osds",
    "instance": "Oinstance", "connect": "Oconnector", "cloner": "Omgcloner", "bend": "Obend",
    "twist": "Otwist", "bulge": "Obulge", "shear": "Oshear", "taper": "Otaper",
}
TAG_ALIASES = {"texture": "Ttexture", "phong": "Tphong", "protection": "Tprotection", "look_at": "Ttargetexpression"}
PROJECTIONS = {
    "uvw": c4d.TEXTURETAG_PROJECTION_UVW, "spherical": c4d.TEXTURETAG_PROJECTION_SPHERICAL,
    "cylindrical": c4d.TEXTURETAG_PROJECTION_CYLINDRICAL, "flat": c4d.TEXTURETAG_PROJECTION_FLAT,
    "cubic": c4d.TEXTURETAG_PROJECTION_CUBIC, "frontal": c4d.TEXTURETAG_PROJECTION_FRONTAL,
}
ENGINES = {
    "standard": c4d.RDATA_RENDERENGINE_STANDARD, "physical": c4d.RDATA_RENDERENGINE_PHYSICAL,
    "viewport": c4d.RDATA_RENDERENGINE_PREVIEWHARDWARE, "redshift": c4d.VPrsrenderer,
}
EXPORTERS = {
    ".c4d": c4d.FORMAT_C4DEXPORT, ".fbx": c4d.FORMAT_FBX_EXPORT, ".obj": c4d.FORMAT_OBJ2EXPORT,
    ".abc": c4d.FORMAT_ABCEXPORT, ".gltf": c4d.FORMAT_GLTFEXPORT, ".glb": c4d.FORMAT_GLTFEXPORT,
    ".stl": c4d.FORMAT_STL_EXPORT, ".dae": c4d.FORMAT_DAE15EXPORT, ".usd": c4d.FORMAT_USDEXPORT,
    ".3ds": c4d.FORMAT_3DSEXPORT, ".dxf": c4d.FORMAT_DXFEXPORT, ".wrl": c4d.FORMAT_VRML2EXPORT,
}
INTERPOLATIONS = {
    "spline": c4d.CINTERPOLATION_SPLINE, "linear": c4d.CINTERPOLATION_LINEAR, "step": c4d.CINTERPOLATION_STEP,
}
SHORTHAND = {
    "position": "ID_BASEOBJECT_REL_POSITION", "rotation": "ID_BASEOBJECT_REL_ROTATION",
    "scale": "ID_BASEOBJECT_REL_SCALE",
}
AXES = {"x": 0, "y": 1, "z": 2}
COMPONENT_IDS = (c4d.VECTOR_X, c4d.VECTOR_Y, c4d.VECTOR_Z)
KINDS = {
    c4d.DTYPE_LONG: "int", c4d.DTYPE_REAL: "float", c4d.DTYPE_BOOL: "bool", c4d.DTYPE_VECTOR: "vector",
    c4d.DTYPE_COLOR: "color", c4d.DTYPE_STRING: "string", c4d.DTYPE_FILENAME: "filename",
    c4d.DTYPE_BASELISTLINK: "link", c4d.DTYPE_TIME: "time",
}
MODES = {0: "on", 1: "off", 2: "default"}


def command(name, raw=False):
    def register(fn):
        COMMANDS[name] = (fn, raw)
        return fn
    return register


def doc():
    return c4d.documents.GetActiveDocument()


def to_json(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, c4d.Vector):
        return [to_json(v.x), to_json(v.y), to_json(v.z)]
    if isinstance(v, c4d.BaseTime):
        return v.Get()
    if isinstance(v, c4d.BaseList2D):
        return v.GetName()
    if isinstance(v, (list, tuple)):
        return [to_json(x) for x in v]
    if isinstance(v, dict):
        return {str(k): to_json(x) for k, x in v.items()}
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


def vec3(v):
    if isinstance(v, (int, float)):
        return c4d.Vector(v, v, v)
    x, y, z = v
    return c4d.Vector(float(x), float(y), float(z))


def parse_vector(v):
    if isinstance(v, str):
        h = v.lstrip("#")
        if len(h) != 6:
            raise ValueError(f"bad color {v}")
        return c4d.Vector(*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)))
    return vec3(v)


def rad3(v):
    x, y, z = v
    return c4d.Vector(math.radians(x), math.radians(y), math.radians(z))


def deg3(v):
    return [math.degrees(v.x), math.degrees(v.y), math.degrees(v.z)]


def scale_angle(v, f):
    if isinstance(v, c4d.Vector):
        return c4d.Vector(f(v.x), f(v.y), f(v.z))
    return f(v)


def symbol_value(name):
    v = getattr(c4d, name, None)
    if not isinstance(v, int):
        raise ValueError(f"unknown symbol {name}")
    return v


def resolve_type(value, prefix, aliases):
    if isinstance(value, int):
        return value
    s = str(value)
    if s.isdigit():
        return int(s)
    s = aliases.get(s.lower().replace(" ", "_"), s)
    for candidate in (s, prefix + s, prefix + s.lower()):
        v = getattr(c4d, candidate, None)
        if isinstance(v, int):
            return v
    raise ValueError(f"unknown type {value}")


def type_symbol(item):
    if isinstance(item, c4d.BaseObject):
        prefix = "O"
    elif isinstance(item, c4d.BaseTag):
        prefix = "T"
    else:
        prefix = "M"
    table = symbols.get(prefix)
    if table is None:
        table = symbols[prefix] = {}
        for n in dir(c4d):
            if len(n) > 1 and n[0] == prefix and n[1].islower():
                v = getattr(c4d, n)
                if isinstance(v, int):
                    table.setdefault(v, n)
    return table.get(item.GetType(), item.GetType())


def siblings(first):
    out = []
    while first:
        out.append(first)
        first = first.GetNext()
    return out


def child_list(parent):
    return siblings(parent.GetDown() if parent else doc().GetFirstObject())


def walk(first):
    while first:
        yield first
        yield from walk(first.GetDown())
        first = first.GetNext()


def materials():
    return siblings(doc().GetFirstMaterial())


def pick(items, seg, ref):
    exact = [o for o in items if o.GetName() == seg]
    if exact:
        return exact[0]
    name, sep, idx = seg.rpartition("#")
    if sep and idx.isdigit():
        named = [o for o in items if o.GetName() == name]
        if 1 <= int(idx) <= len(named):
            return named[int(idx) - 1]
    raise ValueError(f"object not found: {ref}")


def find_object(ref):
    segs = ref.strip("/").split("/")
    if ref.startswith("/"):
        node = pick(child_list(None), segs[0], ref)
    else:
        node = pick(list(walk(doc().GetFirstObject())), segs[0], ref)
    for seg in segs[1:]:
        node = pick(child_list(node), seg, ref)
    return node


def find_material(name):
    for m in materials():
        if m.GetName() == name:
            return m
    raise ValueError(f"material not found: {name}")


def find_tag(op, spec):
    tags = op.GetTags()
    if spec.isdigit():
        if int(spec) < len(tags):
            return tags[int(spec)]
    else:
        for t in tags:
            if spec in (t.GetName(), type_symbol(t)):
                return t
    raise ValueError(f"tag not found: {spec}")


def resolve(target):
    if target == "document":
        return doc()
    if target == "render":
        return doc().GetActiveRenderData()
    if target.startswith("material:"):
        return find_material(target[9:])
    ref, sep, tag = target.partition("@")
    op = find_object(ref)
    return find_tag(op, tag) if sep else op


def path_of(op):
    parts = []
    while op:
        same = [s for s in child_list(op.GetUp()) if s.GetName() == op.GetName()]
        name = op.GetName()
        if len(same) > 1:
            name += "#" + str(same.index(op) + 1)
        parts.append(name)
        op = op.GetUp()
    return "/" + "/".join(reversed(parts))


def describe(op):
    return {"path": path_of(op), "name": op.GetName(), "type": type_symbol(op)}


def unique(base, names):
    if base not in names:
        return base
    i = 1
    while f"{base}.{i}" in names:
        i += 1
    return f"{base}.{i}"


def object_names():
    return {o.GetName() for o in walk(doc().GetFirstObject())}


def insert(op, parent):
    items = child_list(parent)
    doc().InsertObject(op, parent, items[-1] if items else None)


def place(op, a):
    glob = a.get("space", "local") == "global"
    if a.get("position") is not None:
        (op.SetAbsPos if glob else op.SetRelPos)(vec3(a["position"]))
    if a.get("rotation") is not None:
        (op.SetAbsRot if glob else op.SetRelRot)(rad3(a["rotation"]))
    if a.get("scale") is not None:
        (op.SetAbsScale if glob else op.SetRelScale)(vec3(a["scale"]))
    if a.get("look_at") is not None:
        op.SetAbsRot(c4d.utils.VectorToHPB(vec3(a["look_at"]) - op.GetAbsPos()))


def transform_info(op):
    return {
        "position": to_json(op.GetRelPos()), "rotation": deg3(op.GetRelRot()), "scale": to_json(op.GetRelScale()),
        "global_position": to_json(op.GetAbsPos()), "global_rotation": deg3(op.GetAbsRot()),
    }


def param_table(target):
    table = {}
    for bc, did, gid in target.GetDescription(c4d.DESCFLAGS_DESC_0):
        ident = bc[c4d.DESC_IDENT]
        if ident and ident not in table:
            table[ident] = (bc, did)
    return table


def lookup(table, key):
    name, _, comp = key.partition(".")
    name = SHORTHAND.get(name.lower(), name)
    if name not in table:
        raise ValueError(f"unknown parameter {name}")
    bc, did = table[name]
    axis = None
    if comp:
        axis = AXES.get(comp.lower())
        if axis is None:
            raise ValueError(f"unknown component {comp}")
    return did, bc[c4d.DESC_UNIT] == c4d.DESC_UNIT_DEGREE, did[0].dtype, axis


def track_id(did, axis):
    levels = [c4d.DescLevel(did[0].id, did[0].dtype, 0)]
    if axis is not None:
        levels.append(c4d.DescLevel(COMPONENT_IDS[axis], c4d.DTYPE_REAL, 0))
    return c4d.DescID(*levels)


def coerce(value, current, dtype):
    if dtype == c4d.DTYPE_BASELISTLINK:
        return resolve(value) if value else None
    if isinstance(current, c4d.Vector):
        return parse_vector(value)
    if isinstance(current, c4d.BaseTime):
        return c4d.BaseTime(float(value))
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int):
        return symbol_value(value) if isinstance(value, str) else int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, str):
        return str(value)
    return value


def apply_params(target, params, undo=True):
    if undo and not isinstance(target, c4d.documents.BaseDocument):
        doc().AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, target)
    table = param_table(target)
    done, errors = [], {}
    for key, value in params.items():
        try:
            did, deg, dtype, axis = lookup(table, key)
            current = target[did]
            if axis is None:
                v = coerce(value, current, dtype)
                target[did] = scale_angle(v, math.radians) if deg else v
            else:
                current[axis] = math.radians(float(value)) if deg else float(value)
                target[did] = current
            done.append(key)
        except Exception as e:
            errors[key] = str(e)
    return {"set": done, "errors": errors}


def timeline(d):
    fps = d.GetFps()
    return {
        "fps": fps, "frame": d.GetTime().GetFrame(fps),
        "start": d.GetMinTime().GetFrame(fps), "end": d.GetMaxTime().GetFrame(fps),
    }


def listing(first, prefix, depth, max_depth, out, limit):
    items = siblings(first)
    counts = {}
    for o in items:
        counts[o.GetName()] = counts.get(o.GetName(), 0) + 1
    seen = {}
    for o in items:
        name = o.GetName()
        seen[name] = seen.get(name, 0) + 1
        path = prefix + "/" + (f"{name}#{seen[name]}" if counts[name] > 1 else name)
        if len(out) >= limit:
            return
        out.append({"path": path, "type": type_symbol(o), "depth": depth})
        if max_depth is None or depth < max_depth:
            listing(o.GetDown(), path, depth + 1, max_depth, out, limit)


def add_key(op, did, t, value, interpolation):
    track = op.FindCTrack(did)
    if track is None:
        track = c4d.CTrack(op, did)
        op.InsertTrackSorted(track)
    curve = track.GetCurve()
    found = curve.FindKey(t)
    key = found["key"] if found else curve.AddKey(t)["key"]
    key.SetValue(curve, value)
    key.SetInterpolation(curve, interpolation)


@command("scene_info", raw=True)
def scene_info(a):
    d = doc()
    rd = d.GetActiveRenderData()
    engine = rd[c4d.RDATA_RENDERENGINE]
    info = {
        "version": c4d.GetC4DVersion(),
        "document": os.path.join(d.GetDocumentPath(), d.GetDocumentName()),
        "render_size": [rd[c4d.RDATA_XRES], rd[c4d.RDATA_YRES]],
        "renderer": next((k for k, v in ENGINES.items() if v == engine), engine),
        "objects": sum(1 for _ in walk(d.GetFirstObject())),
        "materials": len(materials()),
        "selected": [path_of(o) for o in d.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_0)],
    }
    info.update(timeline(d))
    return info


@command("list_objects", raw=True)
def list_objects(a):
    out = []
    limit = int(a.get("limit", 300))
    max_depth = a.get("depth")
    if a.get("root"):
        node = find_object(a["root"])
        listing(node.GetDown(), path_of(node), 1, max_depth, out, limit)
    else:
        listing(doc().GetFirstObject(), "", 0, max_depth, out, limit)
    return {"objects": out, "truncated": len(out) >= limit}


@command("get_object", raw=True)
def get_object(a):
    d = doc()
    op = find_object(a["target"])
    d.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_0)
    rad, center = op.GetRad(), op.GetMp()
    info = describe(op)
    info.update(transform_info(op))
    info.update({
        "type_name": op.GetTypeName(),
        "bbox_size": [rad.x * 2, rad.y * 2, rad.z * 2],
        "bbox_center": to_json(center),
        "children": [o.GetName() for o in child_list(op)],
        "tags": [{"name": t.GetName(), "type": type_symbol(t)} for t in op.GetTags()],
        "editor_visibility": MODES.get(op[c4d.ID_BASEOBJECT_VISIBILITY_EDITOR]),
        "render_visibility": MODES.get(op[c4d.ID_BASEOBJECT_VISIBILITY_RENDER]),
        "animated": len(op.GetCTracks()) > 0,
    })
    if isinstance(op, c4d.PointObject):
        info["points"] = op.GetPointCount()
    if isinstance(op, c4d.PolygonObject):
        info["polygons"] = op.GetPolygonCount()
    cache = op.GetCache()
    if isinstance(cache, c4d.PolygonObject):
        info["cache_points"] = cache.GetPointCount()
        info["cache_polygons"] = cache.GetPolygonCount()
    return info


@command("get_parameters", raw=True)
def get_parameters(a):
    target = resolve(a["target"])
    needle = a.get("filter", "").lower()
    limit = int(a.get("limit", 100))
    out = []
    for bc, did, gid in target.GetDescription(c4d.DESCFLAGS_DESC_0):
        ident = bc[c4d.DESC_IDENT]
        kind = KINDS.get(did[0].dtype) if did.GetDepth() else None
        name = bc[c4d.DESC_NAME] or ""
        if not ident or kind is None or (needle and needle not in ident.lower() and needle not in name.lower()):
            continue
        try:
            value = target[did]
        except Exception:
            continue
        deg = bc[c4d.DESC_UNIT] == c4d.DESC_UNIT_DEGREE
        entry = {"id": ident, "name": name, "type": kind}
        if deg:
            value = scale_angle(value, math.degrees)
            entry["unit"] = "deg"
        entry["value"] = to_json(value)
        out.append(entry)
        if len(out) >= limit:
            break
    return out


@command("set_parameters")
def set_parameters(a):
    return apply_params(resolve(a["target"]), a["params"])


@command("create_object")
def create_object(a):
    op = c4d.BaseObject(resolve_type(a["type"], "O", OBJECT_ALIASES))
    if op is None:
        raise ValueError("cannot create object")
    op.SetName(unique(a.get("name") or op.GetName(), object_names()))
    result = apply_params(op, a["params"], False) if a.get("params") else None
    insert(op, find_object(a["parent"]) if a.get("parent") else None)
    place(op, a)
    doc().AddUndo(c4d.UNDOTYPE_NEW, op)
    info = describe(op)
    if result and result["errors"]:
        info["errors"] = result["errors"]
    return info


@command("create_mesh")
def create_mesh(a):
    points = [vec3(p) for p in a["vertices"]]
    polygons = a["polygons"]
    if len(points) < 3 or not polygons:
        raise ValueError("need at least 3 vertices and 1 polygon")
    op = c4d.PolygonObject(len(points), len(polygons))
    op.SetAllPoints(points)
    for i, p in enumerate(polygons):
        idx = [int(k) for k in p]
        if len(idx) not in (3, 4) or any(not 0 <= k < len(points) for k in idx):
            raise ValueError(f"bad polygon {i}: {p}")
        if len(idx) == 3:
            idx.append(idx[2])
        op.SetPolygon(i, c4d.CPolygon(*idx))
    if a.get("uvs"):
        if len(a["uvs"]) != len(polygons):
            raise ValueError("uvs must have one entry per polygon")
        tag = op.MakeVariableTag(c4d.Tuvw, len(polygons))
        for i, uv in enumerate(a["uvs"]):
            q = [c4d.Vector(float(u), float(v), 0) for u, v in uv]
            if len(q) not in (3, 4):
                raise ValueError(f"bad uv entry {i}")
            while len(q) < 4:
                q.append(q[-1])
            tag.SetSlow(i, q[0], q[1], q[2], q[3])
    if a.get("smooth"):
        op.MakeTag(c4d.Tphong)
    op.SetName(unique(a.get("name") or "Mesh", object_names()))
    op.Message(c4d.MSG_UPDATE)
    insert(op, find_object(a["parent"]) if a.get("parent") else None)
    place(op, a)
    doc().AddUndo(c4d.UNDOTYPE_NEW, op)
    return describe(op)


@command("set_transform")
def set_transform(a):
    op = find_object(a["target"])
    doc().AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, op)
    place(op, a)
    info = describe(op)
    info.update(transform_info(op))
    return info


@command("delete_objects")
def delete_objects(a):
    ops = [find_object(t) for t in a["targets"]]
    names = [path_of(o) for o in ops]
    for op in ops:
        doc().AddUndo(c4d.UNDOTYPE_DELETE, op)
        op.Remove()
    return {"deleted": names}


@command("duplicate_object")
def duplicate_object(a):
    src = find_object(a["target"])
    offset = vec3(a["offset"]) if a.get("offset") is not None else None
    names = object_names()
    prev = src
    out = []
    for _ in range(int(a.get("count", 1))):
        clone = src.GetClone(c4d.COPYFLAGS_0)
        clone.SetName(unique(a.get("name") or src.GetName(), names))
        names.add(clone.GetName())
        doc().InsertObject(clone, src.GetUp(), prev)
        if offset is not None:
            clone.SetRelPos(prev.GetRelPos() + offset)
        doc().AddUndo(c4d.UNDOTYPE_NEW, clone)
        out.append(describe(clone))
        prev = clone
    return out


@command("reparent_object")
def reparent_object(a):
    op = find_object(a["target"])
    parent = find_object(a["parent"]) if a.get("parent") else None
    p = parent
    while p:
        if p == op:
            raise ValueError("cannot parent an object under itself")
        p = p.GetUp()
    mg = op.GetMg()
    doc().AddUndo(c4d.UNDOTYPE_HIERARCHY_PSR, op)
    op.Remove()
    insert(op, parent)
    if a.get("keep_transform", True):
        op.SetMg(mg)
    return describe(op)


@command("add_tag")
def add_tag(a):
    op = find_object(a["target"])
    tag = op.MakeTag(resolve_type(a["type"], "T", TAG_ALIASES))
    if tag is None:
        raise ValueError("this object cannot take that tag")
    if a.get("name"):
        tag.SetName(a["name"])
    result = apply_params(tag, a["params"], False) if a.get("params") else None
    doc().AddUndo(c4d.UNDOTYPE_NEW, tag)
    info = {"target": f"{path_of(op)}@{op.GetTags().index(tag)}", "name": tag.GetName(), "type": type_symbol(tag)}
    if result and result["errors"]:
        info["errors"] = result["errors"]
    return info


@command("list_materials", raw=True)
def list_materials(a):
    out = []
    for m in materials():
        item = {"name": m.GetName(), "type": type_symbol(m)}
        if m.GetType() == c4d.Mmaterial:
            item["color"] = to_json(m[c4d.MATERIAL_COLOR_COLOR])
        out.append(item)
    return out


@command("create_material")
def create_material(a):
    m = c4d.BaseMaterial(c4d.Mmaterial)
    m.SetName(unique(a.get("name") or "Material", {x.GetName() for x in materials()}))
    if a.get("color") is not None:
        m[c4d.MATERIAL_COLOR_COLOR] = parse_vector(a["color"])
    if a.get("texture"):
        if not os.path.isfile(a["texture"]):
            raise ValueError(f"texture not found: {a['texture']}")
        shader = c4d.BaseShader(c4d.Xbitmap)
        shader[c4d.BITMAPSHADER_FILENAME] = a["texture"]
        m[c4d.MATERIAL_COLOR_SHADER] = shader
        m.InsertShader(shader)
        m[c4d.MATERIAL_USE_COLOR] = True
    if a.get("luminance") is not None:
        m[c4d.MATERIAL_USE_LUMINANCE] = True
        m[c4d.MATERIAL_LUMINANCE_COLOR] = parse_vector(a["luminance"])
    if a.get("transparency") is not None:
        m[c4d.MATERIAL_USE_TRANSPARENCY] = True
        m[c4d.MATERIAL_TRANSPARENCY_BRIGHTNESS] = float(a["transparency"])
    result = apply_params(m, a["params"], False) if a.get("params") else None
    doc().InsertMaterial(m)
    doc().AddUndo(c4d.UNDOTYPE_NEW, m)
    info = {"name": m.GetName()}
    if result and result["errors"]:
        info["errors"] = result["errors"]
    return info


@command("assign_material")
def assign_material(a):
    mat = find_material(a["material"])
    projection = PROJECTIONS[a["projection"]] if a.get("projection") else None
    out = []
    for t in a["targets"]:
        op = find_object(t)
        tag = next((x for x in op.GetTags() if x.GetType() == c4d.Ttexture), None)
        if tag:
            doc().AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, tag)
        else:
            tag = op.MakeTag(c4d.Ttexture)
            doc().AddUndo(c4d.UNDOTYPE_NEW, tag)
        tag[c4d.TEXTURETAG_MATERIAL] = mat
        if projection is not None:
            tag[c4d.TEXTURETAG_PROJECTION] = projection
        out.append(path_of(op))
    return {"assigned": out}


@command("set_keyframes")
def set_keyframes(a):
    target = resolve(a["target"])
    fps = doc().GetFps()
    interpolation = INTERPOLATIONS[a.get("interpolation", "spline")]
    table = param_table(target)
    key = a["param"]
    did, deg, dtype, axis = lookup(table, key)
    axes = [0, 1, 2] if dtype == c4d.DTYPE_VECTOR and axis is None else [axis]
    for k in a["keys"]:
        t = c4d.BaseTime(float(k["frame"]), fps)
        values = k["value"] if len(axes) == 3 else [k["value"]]
        for ax, v in zip(axes, values):
            v = float(v)
            add_key(target, track_id(did, ax), t, math.radians(v) if deg else v, interpolation)
    return {"param": key, "keys": len(a["keys"]), "tracks": len(axes)}


@command("clear_animation")
def clear_animation(a):
    target = resolve(a["target"])
    pid = None
    if a.get("param"):
        pid = lookup(param_table(target), a["param"])[0][0].id
    removed = 0
    for track in list(target.GetCTracks()):
        if pid is None or track.GetDescriptionID()[0].id == pid:
            doc().AddUndo(c4d.UNDOTYPE_DELETE, track)
            track.Remove()
            removed += 1
    return {"removed": removed}


@command("set_timeline")
def set_timeline(a):
    d = doc()
    if a.get("fps"):
        d.SetFps(int(a["fps"]))
    fps = d.GetFps()
    if a.get("start") is not None:
        t = c4d.BaseTime(float(a["start"]), fps)
        d.SetMinTime(t)
        d.SetLoopMinTime(t)
    if a.get("end") is not None:
        t = c4d.BaseTime(float(a["end"]), fps)
        d.SetMaxTime(t)
        d.SetLoopMaxTime(t)
    if a.get("frame") is not None:
        d.SetTime(c4d.BaseTime(float(a["frame"]), fps))
    d.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_0)
    return timeline(d)


@command("render_image", raw=True)
def render_image(a):
    d = doc()
    rd = d.GetActiveRenderData()
    if rd is None:
        raise ValueError("document has no render settings")
    width, height = int(a.get("width", 960)), int(a.get("height", 540))
    engine = a.get("engine", "viewport")
    data = rd.GetData()
    data[c4d.RDATA_XRES] = width
    data[c4d.RDATA_YRES] = height
    if engine != "current":
        if engine not in ENGINES:
            raise ValueError(f"unknown engine {engine}")
        data[c4d.RDATA_RENDERENGINE] = ENGINES[engine]
    temp = not a.get("path")
    path = a.get("path") or os.path.join(tempfile.gettempdir(), "c4d_mcp", f"render_{int(time.time() * 1000)}.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    previous = d.GetTime()
    if a.get("frame") is not None:
        d.SetTime(c4d.BaseTime(float(a["frame"]), d.GetFps()))
    d.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_0)
    bmp = c4d.bitmaps.BaseBitmap()
    if bmp.Init(width, height, 24) != c4d.IMAGERESULT_OK:
        raise ValueError("cannot allocate the bitmap")
    started = time.time()
    try:
        result = c4d.documents.RenderDocument(d, data, bmp, c4d.RENDERFLAGS_EXTERNAL)
    finally:
        d.SetTime(previous)
        d.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_0)
    if result != c4d.RENDERRESULT_OK:
        raise ValueError(f"render failed with code {result} (try engine 'standard')")
    jpeg = os.path.splitext(path)[1].lower() in (".jpg", ".jpeg")
    if bmp.Save(path, c4d.FILTER_JPG if jpeg else c4d.FILTER_PNG) != c4d.IMAGERESULT_OK:
        raise ValueError(f"cannot save {path}")
    return {
        "path": path, "width": width, "height": height, "engine": engine,
        "seconds": round(time.time() - started, 2), "temporary": temp,
    }


@command("new_scene", raw=True)
def new_scene(a):
    d = c4d.documents.BaseDocument()
    if d.GetActiveRenderData() is None:
        rd = c4d.documents.RenderData()
        d.InsertRenderData(rd)
        d.SetActiveRenderData(rd)
    c4d.documents.InsertBaseDocument(d)
    c4d.documents.SetActiveDocument(d)
    return {"fps": d.GetFps()}


@command("open_scene", raw=True)
def open_scene(a):
    path = a["path"]
    if not os.path.isfile(path):
        raise ValueError(f"file not found: {path}")
    flags = c4d.SCENEFILTER_OBJECTS | c4d.SCENEFILTER_MATERIALS
    if a.get("merge"):
        if not c4d.documents.MergeDocument(doc(), path, flags, None):
            raise ValueError(f"cannot merge {path}")
        return {"merged": path}
    d = c4d.documents.LoadDocument(path, flags, None)
    if d is None:
        raise ValueError(f"cannot open {path}")
    c4d.documents.InsertBaseDocument(d)
    c4d.documents.SetActiveDocument(d)
    return {"opened": path}


@command("save_scene", raw=True)
def save_scene(a):
    d = doc()
    path = a.get("path")
    if not path:
        if not d.GetDocumentPath():
            raise ValueError("path is required for an unsaved document")
        path = os.path.join(d.GetDocumentPath(), d.GetDocumentName())
    ext = os.path.splitext(path)[1].lower()
    if ext not in EXPORTERS:
        raise ValueError(f"unsupported extension {ext}, use one of {sorted(EXPORTERS)}")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not c4d.documents.SaveDocument(d, path, c4d.SAVEDOCUMENTFLAGS_DONTADDTORECENTLIST, EXPORTERS[ext]):
        raise ValueError(f"cannot save {path}")
    if ext == ".c4d":
        d.SetDocumentPath(os.path.dirname(path))
        d.SetDocumentName(os.path.basename(path))
    return {"saved": path}


@command("undo", raw=True)
def undo(a):
    d = doc()
    for _ in range(int(a.get("steps", 1))):
        if a.get("redo"):
            d.DoRedo()
        else:
            d.DoUndo()
    return {"ok": True}


@command("execute_python", raw=True)
def execute_python(a):
    d = doc()
    scope = {"__name__": "__main__", "c4d": c4d, "doc": d, "op": d.GetActiveObject(), "result": None}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(a["code"], "<c4d_mcp>", "exec"), scope)
    return {"output": out.getvalue(), "result": scope["result"]}


@command("search_symbols", raw=True)
def search_symbols(a):
    q = a["query"].lower()
    hits = []
    for n in dir(c4d):
        if q in n.lower():
            v = getattr(c4d, n, None)
            if isinstance(v, int) and not isinstance(v, bool):
                hits.append((not n.lower().startswith(q), n, v))
    hits.sort()
    return [[n, v] for _, n, v in hits[:int(a.get("limit", 60))]]


def dispatch(name, args):
    entry = COMMANDS.get(name)
    if entry is None:
        raise ValueError(f"unknown command {name}")
    fn, raw = entry
    d = doc()
    if not raw:
        d.StartUndo()
    try:
        return fn(args)
    finally:
        if not raw:
            d.EndUndo()
        c4d.EventAdd()


class Job:
    def __init__(self, cmd, args):
        self.cmd = cmd
        self.args = args
        self.done = threading.Event()
        self.cancelled = False
        self.response = None


def run_job(job):
    try:
        job.response = {"ok": True, "result": to_json(dispatch(job.cmd, job.args))}
    except KeyError as e:
        job.response = {"ok": False, "error": f"missing or unknown key {e}", "trace": traceback.format_exc()}
    except ValueError as e:
        job.response = {"ok": False, "error": str(e)}
    except Exception as e:
        job.response = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}
    job.done.set()


def drain():
    if state["busy"] or jobs.empty():
        return
    state["busy"] = True
    try:
        while not jobs.empty():
            job = jobs.get_nowait()
            if not job.cancelled:
                run_job(job)
    finally:
        state["busy"] = False


def read_exact(conn, size):
    chunks = []
    while size:
        chunk = conn.recv(min(size, 1 << 20))
        if not chunk:
            raise ConnectionError
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def send_frame(conn, obj):
    body = json.dumps(obj).encode("utf-8")
    conn.sendall(struct.pack(">I", len(body)) + body)


def submit(request):
    job = Job(request.get("cmd"), request.get("args") or {})
    jobs.put(job)
    try:
        c4d.SpecialEventAdd(PLUGIN_ID)
    except Exception:
        pass
    if not job.done.wait(float(request.get("timeout", 60))):
        job.cancelled = True
        return {"ok": False, "error": "timed out waiting for the Cinema 4D main thread, a modal dialog or a long operation may be blocking it"}
    return job.response


def serve(conn):
    with conn:
        try:
            while True:
                (size,) = struct.unpack(">I", read_exact(conn, 4))
                if size > MAX_FRAME:
                    return
                request = json.loads(read_exact(conn, size))
                send_frame(conn, submit(request))
        except (OSError, ValueError):
            return


def accept_loop(sock):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


def start_server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, PORT))
    except OSError as e:
        sock.close()
        print(f"[c4d_mcp] cannot listen on {HOST}:{PORT}: {e}")
        return False
    sock.listen(8)
    state["sock"] = sock
    threading.Thread(target=accept_loop, args=(sock,), daemon=True).start()
    print(f"[c4d_mcp] listening on {HOST}:{PORT}")
    return True


def stop_server():
    sock = state.pop("sock", None)
    if sock:
        sock.close()


class Bridge(c4d.plugins.MessageData):
    def GetTimer(self):
        return 30

    def CoreMessage(self, id, bc):
        if id == PLUGIN_ID or id == c4d.MSG_TIMER:
            drain()
        return True


def PluginMessage(id, data):
    if id == c4d.C4DPL_ENDPROGRAM:
        stop_server()
    return False


if __name__ == "__main__":
    c4d.plugins.RegisterMessagePlugin(PLUGIN_ID, "C4D MCP Bridge", 0, Bridge())
    start_server()
