#!/usr/bin/env python3
"""
openbve-to-blender.py - Convert openBVE / BVE Trainsim objects (.b3d / .csv) to GLB for Blender.

Handles: CreateMeshBuilder, Vertex/AddVertex, Face/Face2/AddFace/AddFace2, Cube,
Cylinder, Translate/Scale/Rotate/Shear/Mirror (+ the *All variants), Color/SetColor,
EmissiveColor, textures (Load/LoadTexture), transparent colour
(Transparent/SetDecalTransparentColor) and texture coordinates
(Coordinates/SetTextureCoordinates).

Also assembles .animated files (3567.animated etc.): every [Object]/[Include] is placed at its
Position (first state, animation functions ignored), producing one GLB per car.
Text DirectX .x objects (Mesh/Material/TextureFilename/Frame) are read too.
Binary and MSZIP-compressed .x are read too. Not supported: animation functions.

Roblox: importing the GLB still has transparency problems (see README). Cut-out pixels are filled
with the surrounding colour (never the BVE key colour) and triangles that are 100% cut out are
removed, but partly transparent parts may still look wrong in Studio.

Usage:
  Double-click            -> pick one or more .b3d/.csv files (cancel to pick a folder)
  python openbve-to-blender.py file.b3d [more files or folders ...]
  Options: -o OUTDIR   --combine (one GLB for everything)   --flip (reverse face winding)
           --smooth-angle DEG (default 45; 0 = flat shading)

Textures (PNG/JPG/BMP/...) are looked up next to each object, then in parent folders,
and embedded in the GLB. If the exact file is missing, the same name with another image
extension is used (e.g. roof.bmp -> roof.png / roof.jpg). Pillow is installed automatically
if needed.

Coordinates: BVE is left-handed (+Z forward); the GLB is right-handed with Z mirrored, so in
Blender the train points along +Y with +X to its right. Face winding is converted so that
normals point outwards (use --flip only if a particular file is authored backwards).
"""
import argparse
import copy
import io
import json
import math
import re
import struct
import subprocess
import sys
import traceback
from pathlib import Path

OBJ_EXTS = {".b3d", ".csv", ".x"}
IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tga", ".tif", ".tiff", ".webp")
UNSUPPORTED_EXTS = set()
ANIM_EXTS = {".animated"}
ASSEMBLED_DIR = "assembled_models"
LOOSE_DIR = "loose_parts"
FORCE_DOUBLE_SIDED = False
KEY_TOLERANCE = None          # None = automatic (see transparent_tolerance)
CUT_CLEAR_FACES = True        # drop triangles whose texture area is 100% cut out (windows etc.)
CUT_PART = None               # None | "opaque" | "cutout" (used by --split-cutouts)
SPLIT_CUTOUTS = False
SMOOTH_ANGLE = 45.0           # degrees; faces meeting at a gentler angle share smooth normals (0 = flat)
_ALPHA_CACHE = {}             # (path, transp, additive) -> alpha channel (PIL 'L' image)


# --------------------------------------------------------------- Pillow
_PIL = None


def get_pil():
    global _PIL
    if _PIL is None:
        try:
            from PIL import Image
            _PIL = Image
        except ImportError:
            print("Installing Pillow (needed for BMP/transparent textures)...")
            for extra in ([], ["--user"], ["--break-system-packages"]):
                try:
                    r = subprocess.run([sys.executable, "-m", "pip", "install", "pillow"] + extra,
                                       capture_output=True, text=True)
                except OSError:
                    break
                if r.returncode == 0:
                    break
            try:
                import importlib
                importlib.invalidate_caches()
                from PIL import Image
                _PIL = Image
            except ImportError:
                print("warning: Pillow could not be installed; only PNG/JPG textures will work.")
                _PIL = False
    return _PIL or None


# --------------------------------------------------------------- parsing
class Builder:
    def __init__(self):
        self.verts = []
        self.faces = []          # (indices, double_sided)
        self.uv = {}
        self.color = (255, 255, 255, 255)
        self.emissive = (0, 0, 0)
        self.tex = None          # daytime texture file name
        self.transp = None
        self.additive = False


CMD_RE = re.compile(r'^\s*(\[[^\]]*\]|[A-Za-z0-9_]+)(?:[\s,]+(.*))?$')
SEC_RE = re.compile(r'^\[\s*([^\]]*?)\s*\]$')


_NUM_PREFIX = re.compile(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")


def lenient_float(text):
    """Like openBVE (VB6 Val): malformed numbers such as `1.85.4` or `0.5m` use their leading
    valid part (1.85 / 0.5). Returns None if there is none."""
    try:
        return float(text)
    except (TypeError, ValueError):
        m = _NUM_PREFIX.match(text or "")
        return float(m.group(1)) if m else None


def num(args, i, d=0.0):
    try:
        v = lenient_float(args[i])
    except IndexError:
        return d
    return d if v is None else v


def parse_rgb(args, n=3, default=255):
    """Parse a BVE colour: `r, g, b[, a]` decimal OR `rrggbb` / `rrggbbaa` hex."""
    if len(args) == 1 and args[0]:
        s = args[0].strip().lstrip("#")
        if len(s) in (6, 8):
            try:
                v = int(s, 16)
                if len(s) == 6:
                    return tuple([(v >> 16) & 255, (v >> 8) & 255, v & 255, 255][:n])
                return tuple([(v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255][:n])
            except ValueError:
                pass
    return tuple(int(max(0, min(255, num(args, i, default)))) for i in range(n))


def read_text(path):
    data = Path(path).read_bytes()
    # UTF-16/32 files (Notepad "Unicode") would otherwise decode as NUL-riddled garbage
    if data[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        return data.decode("utf-32", errors="replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:200]:                       # BOM-less UTF-16
        enc = "utf-16-le" if data[1:2] == b"\x00" else "utf-16-be"
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1252", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def rotate_fn(ax, ay, az, deg):
    length = math.sqrt(ax * ax + ay * ay + az * az)
    if length == 0:
        # openBVE: a zero direction vector means "rotate about the X axis". Real trains rely on it,
        # e.g. `Rotate 0,0,0,90` to turn a Y-axis Cylinder into a headlight pointing forward.
        ax, ay, az, length = 1.0, 0.0, 0.0, 1.0
    kx, ky, kz = ax / length, ay / length, az / length
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))

    def fn(v):
        x, y, z = v
        dot = kx * x + ky * y + kz * z
        cx, cy, cz = ky * z - kz * y, kz * x - kx * z, kx * y - ky * x
        return (x * c + cx * s + kx * dot * (1 - c),
                y * c + cy * s + ky * dot * (1 - c),
                z * c + cz * s + kz * dot * (1 - c))
    return fn


def shear_fn(dx, dy, dz, sx, sy, sz, r):
    """openBVE Shear: every vertex moves by  s * r * dot(d, v)."""
    def unit(x, y, z):
        t = math.sqrt(x * x + y * y + z * z)
        return (x / t, y / t, z / t) if t else (x, y, z)
    dx, dy, dz = unit(dx, dy, dz)          # openBVE normalises both vectors first
    sx, sy, sz = unit(sx, sy, sz)

    def fn(v):
        n = r * (dx * v[0] + dy * v[1] + dz * v[2])
        return (v[0] + sx * n, v[1] + sy * n, v[2] + sz * n)
    return fn


def make_cube(b, hx, hy, hz):
    base = len(b.verts)
    b.verts += [(hx, hy, -hz), (hx, -hy, -hz), (-hx, -hy, -hz), (-hx, hy, -hz),
                (hx, hy, hz), (hx, -hy, hz), (-hx, -hy, hz), (-hx, hy, hz)]
    for f in [(0, 1, 2, 3), (0, 4, 5, 1), (0, 3, 7, 4), (6, 5, 4, 7), (6, 7, 3, 2), (6, 2, 1, 5)]:
        b.faces.append(([base + i for i in f], True))


def make_cylinder(b, n, up, lo, h):
    if n < 2:
        return
    base = len(b.verts)
    for i in range(n):
        dx, dz = math.cos(2 * math.pi * i / n), math.sin(2 * math.pi * i / n)
        b.verts.append((dx * up, h / 2, dz * up))     # 2i   = top ring
        b.verts.append((dx * lo, -h / 2, dz * lo))    # 2i+1 = bottom ring
    for i in range(n):
        j = (i + 1) % n
        # out-facing winding: top_i -> top_j -> bottom_j -> bottom_i
        b.faces.append(([base + 2 * i, base + 2 * j, base + 2 * j + 1, base + 2 * i + 1], True))
    if up > 0:
        b.faces.append(([base + 2 * i for i in reversed(range(n))], True))
    if lo > 0:
        b.faces.append(([base + 2 * i + 1 for i in range(n)], True))


def parse_bve(text, warn):
    builders = []
    cur = None
    unknown = set()

    def apply(targets, fn):
        for t in targets:
            t.verts = [fn(v) for v in t.verts]

    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        m = CMD_RE.match(line)
        if not m:
            continue
        cmd = m.group(1).lower()
        raw_args = m.group(2) or ""
        args = [a.strip() for a in raw_args.split(",")]

        # section markers (handle `[ MeshBuilder ]` with spaces too)
        if cmd.startswith("["):
            sm = SEC_RE.match(cmd)
            if sm and sm.group(1).strip() == "meshbuilder":
                cur = Builder()
                builders.append(cur)
            continue
        if cmd == "createmeshbuilder":
            cur = Builder()
            builders.append(cur)
            continue
        if cur is None:
            cur = Builder()
            builders.append(cur)

        if cmd in ("vertex", "addvertex"):
            cur.verts.append((num(args, 0), num(args, 1), num(args, 2)))
        elif cmd in ("face", "addface", "face2", "addface2"):
            idx = []
            for a in args:
                v = lenient_float(a)
                if v is not None:
                    idx.append(int(v))
            cur.faces.append((idx, cmd.endswith("2")))
        elif cmd == "cube":
            # openBVE: Cube HalfWidth, HalfHeight, HalfDepth (the args ARE half-extents)
            x = num(args, 0, 1.0)
            y = num(args, 1, x)
            z = num(args, 2, x)
            make_cube(cur, x, y, z)
        elif cmd == "cylinder":
            make_cylinder(cur, int(num(args, 0, 8)),
                          num(args, 1, 1.0), num(args, 2, 1.0), num(args, 3, 1.0))
        elif cmd in ("translate", "translateall"):
            dx, dy, dz = num(args, 0), num(args, 1), num(args, 2)
            apply(builders if cmd.endswith("all") else [cur],
                  lambda v: (v[0] + dx, v[1] + dy, v[2] + dz))
        elif cmd in ("scale", "scaleall"):
            # openBVE: a zero scale factor is an error and is replaced by 1
            sx, sy, sz = (num(args, 0, 1.0) or 1.0, num(args, 1, 1.0) or 1.0, num(args, 2, 1.0) or 1.0)
            targets = builders if cmd.endswith("all") else [cur]
            apply(targets, lambda v: (v[0] * sx, v[1] * sy, v[2] * sz))
            if ((sx < 0) + (sy < 0) + (sz < 0)) % 2 == 1:
                for t in targets:
                    t.faces = [(list(reversed(i)), d) for i, d in t.faces]
        elif cmd in ("rotate", "rotateall"):
            fn = rotate_fn(num(args, 0), num(args, 1), num(args, 2), num(args, 3))
            if fn:
                apply(builders if cmd.endswith("all") else [cur], fn)
        elif cmd in ("mirror", "mirrorall"):
            fx, fy, fz = (num(args, 0) != 0), (num(args, 1) != 0), (num(args, 2) != 0)
            targets = builders if cmd.endswith("all") else [cur]
            apply(targets, lambda v: (-v[0] if fx else v[0],
                                      -v[1] if fy else v[1],
                                      -v[2] if fz else v[2]))
            if (fx + fy + fz) % 2 == 1:
                for t in targets:
                    t.faces = [(list(reversed(i)), d) for i, d in t.faces]
        elif cmd in ("color", "setcolor", "colorall", "setcolorall"):
            col = parse_rgb(args, n=4, default=255)
            for t in (builders if cmd.endswith("all") else [cur]):
                t.color = col
        elif cmd in ("emissivecolor", "setemissivecolor", "emissivecolorall", "setemissivecolorall"):
            col = parse_rgb(args, n=3, default=0)
            for t in (builders if cmd.endswith("all") else [cur]):
                t.emissive = col
        elif cmd in ("load", "loadtexture"):
            # `LoadTexture day, night` -> keep the daytime texture (fall back to night)
            names = [p.strip().strip('"') for p in raw_args.split(",") if p.strip().strip('"')]
            if names:
                cur.tex = names[0]
        elif cmd in ("transparent", "setdecaltransparentcolor"):
            cur.transp = parse_rgb(args, n=3, default=0)
        elif cmd == "coordinates":
            # Standard BVE: `Coordinates u, v` applies to the last vertex added.
            # Some non-standard files use an indexed form: `Coordinates idx, u, v`.
            if len(args) >= 3 and args[0].lstrip("+-").isdigit():
                cur.uv[int(args[0])] = (num(args, 1), num(args, 2))
            elif len(args) >= 2 and cur.verts:
                cur.uv[len(cur.verts) - 1] = (num(args, 0), num(args, 1))
        elif cmd == "settexturecoordinates":
            if len(args) >= 3 and args[0].lstrip("+-").isdigit():
                cur.uv[int(args[0])] = (num(args, 1), num(args, 2))
        elif cmd in ("shear", "shearall"):
            apply(builders if cmd.endswith("all") else [cur],
                  shear_fn(num(args, 0), num(args, 1), num(args, 2),
                           num(args, 3), num(args, 4), num(args, 5), num(args, 6)))
        elif cmd in ("blendmode", "setblendmode"):
            cur.additive = bool(args and args[0].lower() == "additive")
        elif cmd in ("generatenormals", "settext", "settextcolor", "settextbackgroundcolor",
                     "settextcolorall", "setdecaltransparentcolorall", "settextbackgroundcolorall"):
            pass
        else:
            unknown.add(m.group(1))
    if unknown:
        warn("ignored commands: " + ", ".join(sorted(unknown)))
    return builders


# --------------------------------------------------------------- DirectX .x (text format)
X_TOKEN_RE = re.compile(r'"[^"]*"|[{}]|[^\s{}";,]+')


def _x_tokens(text):
    text = re.sub(r"//[^\n]*|#[^\n]*", "", text)
    return X_TOKEN_RE.findall(text)


def _x_parse_block(toks, i):
    """Parse after the opening brace. Returns (items, next_index); items hold numbers/strings,
    ('block', type, name, items) children and ('ref', name) references."""
    items = []
    while i < len(toks):
        t = toks[i]
        if t == "}":
            return items, i + 1
        if t == "{":                                     # reference: { name }
            j = i + 1
            ref = []
            while j < len(toks) and toks[j] != "}":
                ref.append(toks[j])
                j += 1
            items.append(("ref", ref[0] if ref else ""))
            i = j + 1
            continue
        if t.startswith('"'):
            items.append(t.strip('"'))
            i += 1
            continue
        # type [name] {   -> child block (identifiers followed by "{" within two tokens)
        if i + 1 < len(toks) and toks[i + 1] == "{" and re.match(r"[A-Za-z_]", t):
            typ, name, i = t, "", i + 2
        elif (i + 2 < len(toks) and toks[i + 2] == "{" and toks[i + 1] not in ("{", "}")
              and re.match(r"[A-Za-z_]", t)):
            typ, name, i = t, toks[i + 1], i + 3
        else:
            try:
                items.append(float(t))
            except ValueError:
                items.append(t)
            i += 1
            continue
        sub, i = _x_parse_block(toks, i)
        items.append(("block", typ, name, sub))
    return items, i


def _x_nums(items):
    return [x for x in items if isinstance(x, float)]


def _x_material(items):
    n = _x_nums(items)
    color = tuple(int(max(0, min(255, round(c * 255)))) for c in (n[0:4] if len(n) >= 4 else (1, 1, 1, 1)))
    tex = None
    for it in items:
        if isinstance(it, tuple) and it[0] == "block" and it[1].lower() == "texturefilename":
            names = [x for x in it[3] if isinstance(x, str)]
            if names:
                tex = names[0]
    return color, tex


def _x_mszip(data):
    """Decompress the MSZIP payload of 'bzip'/'tzip' .x files (chunks of <=32 KB, each chunk
    uses the previous chunk as its dictionary)."""
    import zlib
    pos, out, prev = 20, bytearray(), b""
    while pos + 4 <= len(data):
        _, dsz = struct.unpack("<HH", data[pos:pos + 4])
        pos += 4
        chunk = data[pos:pos + dsz]
        pos += dsz
        if chunk[:2] != b"CK":
            raise ValueError("bad MSZIP chunk")
        d = zlib.decompressobj(-15, zdict=prev) if prev else zlib.decompressobj(-15)
        prev = d.decompress(chunk[2:])
        out += prev
    return bytes(out)


def _x_binary_tokens(data, floatsize):
    """Binary .x payload (after the 16-byte header) -> the same token list the text reader makes."""
    toks, pos, n = [], 0, len(data)
    ff, fs = ("<d", 8) if floatsize == 64 else ("<f", 4)
    depth_skip = 0                                    # brace depth while inside a `template { ... }`
    in_tmpl = False                                   # saw TEMPLATE, waiting for its `{`
    while pos + 2 <= n:
        t = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if t in (1, 2):                               # NAME / STRING
            ln = struct.unpack_from("<I", data, pos)[0]
            s = data[pos + 4:pos + 4 + ln].decode("latin-1")
            pos += 4 + ln
            if t == 2:
                pos += 2                              # terminator token (, or ;)
            if not depth_skip and not in_tmpl:
                toks.append('"%s"' % s if t == 2 else s)
        elif t == 3:
            pos += 4
            if not depth_skip:
                toks.append(str(struct.unpack_from("<I", data, pos - 4)[0]))
        elif t == 5:
            pos += 16
        elif t == 6:
            cnt = struct.unpack_from("<I", data, pos)[0]
            vals = struct.unpack_from("<%dI" % cnt, data, pos + 4)
            pos += 4 + 4 * cnt
            if not depth_skip:
                toks.extend(str(v) for v in vals)
        elif t == 7:
            cnt = struct.unpack_from("<I", data, pos)[0]
            vals = struct.unpack_from("<%s" % (ff[1] * cnt), data, pos + 4)
            pos += 4 + fs * cnt
            if not depth_skip:
                toks.extend(repr(v) for v in vals)
        elif t == 10:
            if in_tmpl:
                in_tmpl, depth_skip = False, 1
            elif depth_skip:
                depth_skip += 1
            else:
                toks.append("{")
        elif t == 11:
            if depth_skip:
                depth_skip -= 1
            else:
                toks.append("}")
        elif t == 31:                                 # template definition: skip it entirely
            in_tmpl = True
    return toks



def parse_x(text, warn, toks=None):
    """Text DirectX .x (what openBVE loads): Mesh, MeshMaterialList, Material/TextureFilename,
    MeshTextureCoords and Frame/FrameTransformMatrix. Also accepts a pre-built token list (binary .x)."""
    if toks is None:
        head = text[:16]
        if not head.startswith("xof"):
            warn("not a DirectX .x file")
            return []
        if head[8:11] != "txt":
            warn("compressed text .x files are not supported (re-save as text .x)")
            return []
        toks = _x_tokens(text[16:])
    i, root = 0, []
    while i < len(toks):
        t = toks[i]
        if i + 1 < len(toks) and toks[i + 1] == "{" and re.match(r"[A-Za-z_]", t):
            typ, name, i = t, "", i + 2
        elif i + 2 < len(toks) and toks[i + 2] == "{" and re.match(r"[A-Za-z_]", t):
            typ, name, i = t, toks[i + 1], i + 3
        else:
            i += 1
            continue
        sub, i = _x_parse_block(toks, i)
        root.append(("block", typ, name, sub))

    materials = {}                                       # named top-level/nested materials
    builders = []

    def collect_mats(items):
        for it in items:
            if isinstance(it, tuple) and it[0] == "block":
                if it[1].lower() == "material" and it[2]:
                    materials[it[2]] = _x_material(it[3])
                collect_mats(it[3])
    collect_mats(root)

    ident = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)

    def mul(a, b):                                       # row-vector convention: v * A * B
        return tuple(sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4)) for r in range(4) for c in range(4))

    def mesh(items, mat):
        nums = []
        for it in items:
            if isinstance(it, float):
                nums.append(it)
            else:
                break
        pos = 0
        nv = int(nums[pos]); pos += 1
        verts = []
        for _ in range(nv):
            x, y, z = nums[pos:pos + 3]
            pos += 3
            verts.append((x * mat[0] + y * mat[4] + z * mat[8] + mat[12],
                          x * mat[1] + y * mat[5] + z * mat[9] + mat[13],
                          x * mat[2] + y * mat[6] + z * mat[10] + mat[14]))
        nf = int(nums[pos]); pos += 1
        faces = []
        for _ in range(nf):
            k = int(nums[pos]); pos += 1
            faces.append([int(v) for v in nums[pos:pos + k]])
            pos += k
        uvs, face_mat, mlist = {}, [0] * nf, [(( 255, 255, 255, 255), None)]
        for it in items:
            if not (isinstance(it, tuple) and it[0] == "block"):
                continue
            kind = it[1].lower()
            sub = it[3]
            if kind == "meshtexturecoords":
                n = _x_nums(sub)
                cnt = int(n[0])
                for vi in range(cnt):
                    uvs[vi] = (n[1 + 2 * vi], n[2 + 2 * vi])
            elif kind == "meshmateriallist":
                n = _x_nums(sub)
                nm, nidx = int(n[0]), int(n[1])
                idx = [int(v) for v in n[2:2 + nidx]]
                if idx:
                    face_mat = [idx[f % len(idx)] if nidx != 1 else idx[0] for f in range(nf)]
                mlist = []
                for c in sub:
                    if isinstance(c, tuple) and c[0] == "block" and c[1].lower() == "material":
                        mlist.append(_x_material(c[3]))
                    elif isinstance(c, tuple) and c[0] == "ref":
                        mlist.append(materials.get(c[1], ((255, 255, 255, 255), None)))
                if not mlist:
                    mlist = [((255, 255, 255, 255), None)]
        for mi, (color, tex) in enumerate(mlist):
            fl = [f for f, m in zip(faces, face_mat) if m == mi]
            if not fl:
                continue
            b = Builder()
            b.verts = list(verts)
            b.uv = dict(uvs)
            b.faces = [(f, False) for f in fl]
            b.color = color
            b.tex = tex
            builders.append(b)

    def walk(items, mat):
        for it in items:
            if not (isinstance(it, tuple) and it[0] == "block"):
                continue
            kind = it[1].lower()
            if kind == "frame":
                m = mat
                for c in it[3]:
                    if isinstance(c, tuple) and c[0] == "block" and c[1].lower() == "frametransformmatrix":
                        n = _x_nums(c[3])
                        if len(n) >= 16:
                            m = mul(tuple(n[:16]), mat)
                walk(it[3], m)
            elif kind == "mesh":
                try:
                    mesh(it[3], mat)
                except (IndexError, ValueError) as e:
                    warn("could not read a Mesh in .x file (%s)" % e)
                    continue
                walk(it[3], mat)

    walk(root, ident)
    return builders


def load_builders(path, warn):
    """Parse an object file by extension (.b3d/.csv -> BVE script, .x -> DirectX text)."""
    if Path(path).suffix.lower() == ".x":
        data = Path(path).read_bytes()
        if data[:4] == b"xof " and data[8:12] in (b"bin ", b"bzip", b"tzip"):
            try:
                fmt, fsz = data[8:12], (64 if data[12:16] == b"0064" else 32)
                if fmt == b"bin ":
                    return parse_x(None, warn, _x_binary_tokens(data[16:], fsz))
                raw = _x_mszip(data)
                if fmt == b"tzip":
                    return parse_x(None, warn, _x_tokens(raw.decode("latin-1")))
                return parse_x(None, warn, _x_binary_tokens(raw, fsz))
            except Exception as e:
                warn("could not read binary/compressed .x (%s)" % e)
                return []
        return parse_x(read_text(path), warn)
    return parse_bve(read_text(path), warn)


# --------------------------------------------------------------- textures
_TEX_INDEX = {}


def _index(root):
    """Cached index of everything under root (bounded): returns (names, stems).
    names: lowercase filename -> path;  stems: lowercase stem -> [paths of image files]."""
    try:
        root = Path(root).resolve()
    except (OSError, ValueError):
        return {}, {}
    if root in _TEX_INDEX:
        return _TEX_INDEX[root]
    names, stems = {}, {}
    result = (names, stems)
    _TEX_INDEX[root] = result
    # Never walk an entire filesystem if someone points us at / or C:\
    if not root.is_dir() or len(root.parts) <= 1:
        return result
    try:
        max_depth, count = 8, 0
        for p in root.rglob("*"):
            try:
                if not p.is_file():
                    continue
                if len(p.relative_to(root).parts) > max_depth:
                    continue
            except (ValueError, OSError):
                continue
            names.setdefault(p.name.lower(), p)
            if p.suffix.lower() in IMG_EXTS:
                stems.setdefault(p.stem.lower(), []).append(p)
            count += 1
            if count > 100000:
                break
    except (PermissionError, OSError):
        pass
    return result


def _pick_alt(paths, want_ext):
    """Prefer lossless formats; PNG first, since BMPs are usually re-saved as PNG."""
    order = {e: i for i, e in enumerate((".png", ".bmp", ".jpg", ".jpeg", ".tga", ".gif", ".tif", ".tiff", ".webp"))}
    return sorted(paths, key=lambda p: (len(p.parts), order.get(p.suffix.lower(), 99)))[0]


def find_texture(name, base_dir):
    """Exact name first (next to the object, then parents, then a case-insensitive search),
    then the same name with any other image extension (roof.bmp -> roof.png/jpg)."""
    rel = Path(name.replace("\\", "/").strip().strip('"'))
    base_dir = Path(base_dir)
    dirs = [base_dir, base_dir.parent, base_dir.parent.parent]
    for d in dirs:
        cand = d / rel
        if cand.is_file():
            return cand
    lname = rel.name.lower()
    stem = Path(lname).stem
    roots = [base_dir, base_dir.parent.parent]
    for r in roots:
        hit = _index(r)[0].get(lname)
        if hit:
            return hit
    # same stem, different image extension
    for d in dirs:
        for ext in IMG_EXTS:
            cand = d / rel.with_suffix(ext)
            if cand.is_file():
                return cand
    for r in roots:
        alts = _index(r)[1].get(stem)
        if alts:
            return _pick_alt(alts, rel.suffix.lower())
    return None


# --------------------------------------------------------------- GLB writer
def srgb_to_lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


_TEX_CACHE = {}


def bleed_edges(rgb, known, iters=4):
    from PIL import ImageChops
    cur, k = rgb.copy(), known.copy()
    offs = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
    for _ in range(iters):
        new, newk = cur.copy(), k.copy()
        for dx, dy in offs:
            sc = ImageChops.offset(cur, dx, dy)
            sk = ImageChops.offset(k, dx, dy)
            new.paste(sc, mask=ImageChops.subtract(sk, newk))
            newk = ImageChops.lighter(newk, sk)
        cur, k = new, newk
    # Large cut-outs (windows, doors) are far from any opaque pixel, so the local bleed above never
    # reaches them and they would keep the key colour (BVE blue/magenta). Programs that ignore the
    # alpha channel (Roblox's importer, some viewers) then show that colour. Fill the rest with the
    # average colour of the opaque pixels instead.
    from PIL import Image, ImageStat
    unknown = k.point(lambda v: 0 if v else 255)
    if unknown.getextrema()[1] > 0:
        try:
            mean = tuple(int(round(x)) for x in ImageStat.Stat(cur, mask=k).mean[:3])
        except Exception:
            mean = (128, 128, 128)
        if k.getextrema()[1] == 0:
            mean = (128, 128, 128)
        cur.paste(Image.new("RGB", cur.size, mean), mask=unknown)
    return cur


def transparent_tolerance(transp, fmt):
    """BVE matches the transparent colour exactly. Lossless sources (BMP/PNG/GIF) therefore
    need no tolerance; JPEG compression smears the key colour, so allow some slack there."""
    if KEY_TOLERANCE is not None:                    # --key-tolerance N overrides everything
        return KEY_TOLERANCE, KEY_TOLERANCE
    if (fmt or "").upper() not in ("JPEG", "MPO"):
        return 0, 0
    # Only saturated keys (red / blue / magenta / green ...) get slack. Dark or grey keys such as
    # black must stay exact, otherwise dark texture detail (undercarriage, trucks) disappears.
    strong = (max(transp) - min(transp)) >= 150
    return (80, 110) if strong else (0, 0)


def process_texture(path, transp, additive, warn):
    key = (str(path), transp, additive)
    if key in _TEX_CACHE:
        return _TEX_CACHE[key]
    result = None
    Image = get_pil()
    try:
        ext = Path(path).suffix.lower()
        if Image:
            from PIL import ImageChops, ImageFilter
            with Image.open(path) as im:
                im.load()
                fmt = im.format or ""
                img = im.copy()
            if (transp is None and not additive and img.mode in ("RGB", "L")
                    and fmt in ("PNG", "JPEG")):
                data, mode = Path(path).read_bytes(), "OPAQUE"
            else:
                img = img.convert("RGBA")
                r_, g_, b_, a_ = img.split()
                if transp is not None:
                    tol, wide_tol = transparent_tolerance(transp, fmt)
                    diffs = [ImageChops.difference(ch, Image.new("L", img.size, t))
                             for ch, t in zip((r_, g_, b_), transp)]
                    far = ImageChops.lighter(ImageChops.lighter(diffs[0], diffs[1]), diffs[2])
                    hit = far.point(lambda v, t=tol: 255 if v <= t else 0)
                    if wide_tol > tol:
                        # JPEG fringe: pixels near the key colour that touch a key pixel
                        wide = far.point(lambda v, t=wide_tol: 255 if v <= t else 0)
                        touching = hit.filter(ImageFilter.MaxFilter(3))
                        hit = ImageChops.lighter(hit, ImageChops.darker(wide, touching))
                    a_ = ImageChops.subtract(a_, hit)
                    known = a_.point(lambda v: 255 if v > 0 else 0)
                    filled = bleed_edges(Image.merge("RGB", (r_, g_, b_)), known)
                    r_, g_, b_ = filled.split()
                    img = Image.merge("RGBA", (r_, g_, b_, a_))
                if additive:
                    # additive blending: brightness acts as opacity (keep cut-out pixels cut out)
                    lum = ImageChops.lighter(ImageChops.lighter(r_, g_), b_)
                    a_ = ImageChops.darker(a_, lum)
                    img.putalpha(a_)
                lo = a_.getextrema()[0]
                if lo == 0 and transp is not None and not additive:
                    _ALPHA_CACHE[key] = a_
                mode = "OPAQUE"
                if lo < 255:
                    mode = "MASK" if (transp is not None and not additive) else "BLEND"
                buf = io.BytesIO()
                img.save(buf, "PNG", compress_level=3)
                data = buf.getvalue()
        elif ext in (".png", ".jpg", ".jpeg"):
            data, mode = Path(path).read_bytes(), "OPAQUE"
        else:
            warn("cannot convert %s without Pillow" % Path(path).name)
            _TEX_CACHE[key] = None
            return None
        mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        result = (data, mime, mode)
    except Exception as e:
        warn("texture %s failed: %s" % (Path(path).name, e))
    _TEX_CACHE[key] = result
    return result


def tri_is_clear(alpha, uvs):
    """True if the whole UV bounding box of a triangle lies on fully cut-out pixels
    (conservative: any opaque pixel inside the box, or a box spanning more than one tile, keeps it)."""
    us = [u for u, _ in uvs]
    vs = [v for _, v in uvs]
    if max(us) - min(us) > 1.0 + 1e-6 or max(vs) - min(vs) > 1.0 + 1e-6:
        return False
    w, h = alpha.size
    u0, u1 = min(us) % 1.0, min(us) % 1.0 + (max(us) - min(us))
    v0, v1 = min(vs) % 1.0, min(vs) % 1.0 + (max(vs) - min(vs))
    if u1 > 1.0 + 1e-6 or v1 > 1.0 + 1e-6:        # box wraps around the texture edge
        return False
    box = (max(0, int(u0 * w) - 1), max(0, int(v0 * h) - 1),
           min(w, int(math.ceil(u1 * w)) + 2), min(h, int(math.ceil(v1 * h)) + 2))
    if box[2] <= box[0] or box[3] <= box[1]:
        return False
    return alpha.crop(box).getextrema()[1] == 0


class Glb:
    def __init__(self):
        self.bin = bytearray()
        self.views, self.accs, self.images, self.textures = [], [], [], []
        self.materials, self.meshes, self.nodes = [], [], []
        self.mat_cache, self.tex_cache = {}, {}

    def _view(self, data, target=None):
        self.bin += b"\0" * ((-len(self.bin)) % 4)
        off = len(self.bin)
        self.bin += data
        v = {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
        if target:
            v["target"] = target
        self.views.append(v)
        return len(self.views) - 1

    def _acc(self, flat, ncomp, typ, with_minmax=False):
        count = len(flat) // ncomp
        packed = struct.pack("<%df" % len(flat), *flat)
        view = self._view(packed, 34962)
        acc = {"bufferView": view, "componentType": 5126, "count": count, "type": typ}
        if with_minmax:
            # min/max must match the float32 values actually stored
            f32 = struct.unpack("<%df" % len(flat), packed)
            cols = [f32[i::ncomp] for i in range(ncomp)]
            acc["min"] = [min(c) for c in cols]
            acc["max"] = [max(c) for c in cols]
        self.accs.append(acc)
        return len(self.accs) - 1

    def _idx_acc(self, idx):
        view = self._view(struct.pack("<%dI" % len(idx), *idx), 34963)
        self.accs.append({"bufferView": view, "componentType": 5125,
                          "count": len(idx), "type": "SCALAR"})
        return len(self.accs) - 1

    def texture(self, path, transp, additive, warn):
        key = (str(path), transp, additive)
        if key in self.tex_cache:
            return self.tex_cache[key]
        proc = process_texture(path, transp, additive, warn)
        result = None
        if proc:
            data, mime, mode = proc
            view = self._view(data)
            self.images.append({"bufferView": view, "mimeType": mime, "name": Path(path).stem})
            self.textures.append({"source": len(self.images) - 1, "sampler": 0})
            result = (len(self.textures) - 1, mode)
        self.tex_cache[key] = result
        return result

    def material(self, color, emissive, tex, double, mode, additive=False):
        key = (color, emissive, tex, double, mode, additive)
        if key in self.mat_cache:
            return self.mat_cache[key]
        pbr = {"baseColorFactor": [srgb_to_lin(color[0]), srgb_to_lin(color[1]),
                                   srgb_to_lin(color[2]), color[3] / 255.0],
               "metallicFactor": 0.0, "roughnessFactor": 0.9}
        if tex is not None:
            pbr["baseColorTexture"] = {"index": tex}
        if tex is not None:
            label = self.images[self.textures[tex]["source"]].get("name", "tex")
        else:
            label = "color_%02x%02x%02x" % tuple(color[:3])
        if tex is not None and mode in ("MASK", "BLEND"):
            label += "_cutout"                      # marks materials with cut-outs
        m = {"name": "%s_%d" % (label, len(self.materials)), "pbrMetallicRoughness": pbr,
             "doubleSided": bool(double) or FORCE_DOUBLE_SIDED}
        if any(emissive):
            m["emissiveFactor"] = [srgb_to_lin(c) for c in emissive]
            if tex is not None:
                m["emissiveTexture"] = {"index": tex}
        if additive and tex is not None:
            pbr["baseColorFactor"] = [0.0, 0.0, 0.0, 1.0]
            m["emissiveFactor"] = [1.0, 1.0, 1.0]
            m["emissiveTexture"] = {"index": tex}
            m["doubleSided"] = True
            mode = "BLEND"
        if mode == "MASK":
            m["alphaMode"], m["alphaCutoff"] = "MASK", 0.5
        elif mode == "BLEND":
            m["alphaMode"] = "BLEND"
        self.materials.append(m)
        self.mat_cache[key] = len(self.materials) - 1
        return self.mat_cache[key]

    def build_mesh(self, name, builders, base_dir, flip, warn):
        groups = {}
        tri_list = []
        bad = 0
        cut = 0
        for b in builders:
            tex = None
            mode = "OPAQUE"
            alpha = None
            if b.tex:
                p = find_texture(b.tex, base_dir)
                if p is None:
                    warn("texture not found: %s" % b.tex)
                else:
                    t = self.texture(p, b.transp, b.additive, warn)
                    if t:
                        tex, mode = t
                        if CUT_CLEAR_FACES and mode == "MASK":
                            alpha = _ALPHA_CACHE.get((str(p), b.transp, b.additive))
            if b.color[3] < 255 and mode == "OPAQUE":
                mode = "BLEND"
            n = len(b.verts)
            for idx, dbl in b.faces:
                good = [i for i in idx if 0 <= i < n]
                if len(good) != len(idx):
                    bad += 1
                if len(good) < 3:
                    continue
                key = self.material(b.color, b.emissive, tex, dbl, mode, b.additive)
                g = groups.setdefault(key, ([], [], [], [], {}))   # pos, nor, uv, indices, dedupe map
                for k in range(1, len(good) - 1):
                    # BVE is left-handed with clockwise front faces. Mirroring Z into glTF's
                    # right-handed space keeps the *picture* identical, so the winding must be
                    # reversed to get glTF's counter-clockwise front faces.
                    tri = [good[0], good[k + 1], good[k]]
                    if flip:
                        tri = [tri[0], tri[2], tri[1]]
                    if alpha is not None and tri_is_clear(alpha, [b.uv.get(i, (0.0, 0.0)) for i in tri]):
                        cut += 1
                        continue
                    pts = [(b.verts[i][0], b.verts[i][1], -b.verts[i][2]) for i in tri]
                    e1 = [pts[1][j] - pts[0][j] for j in range(3)]
                    e2 = [pts[2][j] - pts[0][j] for j in range(3)]
                    nx = e1[1] * e2[2] - e1[2] * e2[1]
                    ny = e1[2] * e2[0] - e1[0] * e2[2]
                    nz = e1[0] * e2[1] - e1[1] * e2[0]
                    ln = math.sqrt(nx * nx + ny * ny + nz * nz)
                    if ln < 1e-12:
                        continue
                    nrm = (nx / ln, ny / ln, nz / ln)
                    tri_list.append((key, pts, [b.uv.get(i, (0.0, 0.0)) for i in tri], nrm, ln))
        # Smooth normals: every corner averages the face normals of all triangles that touch the
        # same position and bend away by less than SMOOTH_ANGLE (area weighted). Corners that end
        # up identical are shared, so the mesh is connected in Blender (Shade Smooth by Angle works).
        def rk(pt):
            return (round(pt[0], 5), round(pt[1], 5), round(pt[2], 5))
        smooth = SMOOTH_ANGLE > 0
        cos_t = math.cos(math.radians(SMOOTH_ANGLE))
        adj = {}
        if smooth:
            for ti, t in enumerate(tri_list):
                for pt in t[1]:
                    adj.setdefault(rk(pt), []).append(ti)
        ncache = {}
        for gkey, pts, uvl, nrm, _w in tri_list:
            pos, nor, uvs, ind, seen = groups[gkey]
            for pt, uv in zip(pts, uvl):
                n = nrm
                if smooth:
                    ck = (rk(pt), nrm)
                    n = ncache.get(ck)
                    if n is None:
                        sx = sy = sz = 0.0
                        for tj in adj[rk(pt)]:
                            n2, w2 = tri_list[tj][3], tri_list[tj][4]
                            if n2[0] * nrm[0] + n2[1] * nrm[1] + n2[2] * nrm[2] >= cos_t:
                                sx += n2[0] * w2
                                sy += n2[1] * w2
                                sz += n2[2] * w2
                        L = math.sqrt(sx * sx + sy * sy + sz * sz)
                        n = (round(sx / L, 5), round(sy / L, 5), round(sz / L, 5)) if L > 1e-12 else nrm
                        ncache[ck] = n
                vk = (pt, n, uv)
                vi = seen.get(vk)
                if vi is None:
                    vi = len(pos) // 3
                    seen[vk] = vi
                    pos.extend(pt)
                    nor.extend(n)
                    uvs.extend(uv)
                ind.append(vi)
        if bad:
            warn("%d face(s) referenced missing vertices" % bad)
        if cut:
            warn("removed %d fully transparent triangle(s) (window/door cut-outs become real holes; "
                 "use --keep-cutouts to keep them)" % cut)
        prims = []
        tris = 0
        for mat, (pos, nor, uv, ind, _) in groups.items():
            if not ind:
                continue
            if CUT_PART:
                is_cut = self.materials[mat].get("alphaMode") in ("MASK", "BLEND")
                if (CUT_PART == "opaque" and is_cut) or (CUT_PART == "cutout" and not is_cut):
                    continue
            tris += len(ind) // 3
            prims.append({"attributes": {"POSITION": self._acc(pos, 3, "VEC3", True),
                                         "NORMAL": self._acc(nor, 3, "VEC3"),
                                         "TEXCOORD_0": self._acc(uv, 2, "VEC2")},
                          "indices": self._idx_acc(ind),
                          "material": mat, "mode": 4})
        if not prims:
            return None, 0
        self.meshes.append({"name": name, "primitives": prims})
        return len(self.meshes) - 1, tris

    def add_object(self, name, builders, base_dir, flip, warn):
        mi, tris = self.build_mesh(name, builders, base_dir, flip, warn)
        if mi is None:
            return 0
        self.nodes.append({"name": name, "mesh": mi})
        return tris

    def merge(self, other):
        """Append another Glb's nodes/meshes/materials/textures into self.

        Nodes from our writer are flat (no `children`), so we don't need to
        fix hierarchy — but the merge must remap every index.
        """
        self.bin += b"\0" * ((-len(self.bin)) % 4)
        bin_start = len(self.bin)
        self.bin += other.bin

        view_off = len(self.views)
        for v in other.views:
            v2 = dict(v)
            v2["byteOffset"] = v2.get("byteOffset", 0) + bin_start
            self.views.append(v2)

        acc_off = len(self.accs)
        for a in other.accs:
            a2 = dict(a)
            if "bufferView" in a2:
                a2["bufferView"] += view_off
            self.accs.append(a2)

        img_off = len(self.images)
        for im in other.images:
            im2 = dict(im)
            if "bufferView" in im2:
                im2["bufferView"] += view_off
            self.images.append(im2)

        tex_off = len(self.textures)
        for t in other.textures:
            t2 = dict(t)
            if "source" in t2:
                t2["source"] += img_off
            self.textures.append(t2)

        mat_off = len(self.materials)
        for m in other.materials:
            m2 = copy.deepcopy(m)
            pbr = m2.get("pbrMetallicRoughness", {})
            if "baseColorTexture" in pbr and "index" in pbr["baseColorTexture"]:
                pbr["baseColorTexture"]["index"] += tex_off
            for key in ("emissiveTexture", "normalTexture", "occlusionTexture"):
                if key in m2 and "index" in m2[key]:
                    m2[key]["index"] += tex_off
            self.materials.append(m2)

        mesh_off = len(self.meshes)
        for me in other.meshes:
            me2 = copy.deepcopy(me)
            for prim in me2.get("primitives", []):
                if "material" in prim:
                    prim["material"] += mat_off
                for attr in ("POSITION", "NORMAL", "TANGENT",
                             "TEXCOORD_0", "TEXCOORD_1", "COLOR_0",
                             "JOINTS_0", "WEIGHTS_0"):
                    if attr in prim.get("attributes", {}):
                        prim["attributes"][attr] += acc_off
                if "indices" in prim:
                    prim["indices"] += acc_off
            self.meshes.append(me2)

        for n in other.nodes:
            n2 = dict(n)
            if "mesh" in n2:
                n2["mesh"] += mesh_off
            self.nodes.append(n2)

    def write(self, path):
        g = {"asset": {"version": "2.0", "generator": "openbve-to-blender"}, "scene": 0,
             "scenes": [{"nodes": list(range(len(self.nodes)))}],
             "nodes": self.nodes, "meshes": self.meshes, "accessors": self.accs,
             "bufferViews": self.views, "buffers": [{"byteLength": len(self.bin)}],
             "materials": self.materials}
        if self.textures:
            g["images"], g["textures"] = self.images, self.textures
            g["samplers"] = [{"magFilter": 9729, "minFilter": 9987,
                              "wrapS": 10497, "wrapT": 10497}]
        js = json.dumps(g, separators=(",", ":")).encode()
        js += b" " * ((-len(js)) % 4)
        binc = bytes(self.bin) + b"\0" * ((-len(self.bin)) % 4)
        total = 12 + 8 + len(js) + 8 + len(binc)
        with open(path, "wb") as f:
            f.write(struct.pack("<III", 0x46546C67, 2, total))
            f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
            f.write(struct.pack("<II", len(binc), 0x004E4942) + binc)


def verify_glb(path):
    try:
        data = Path(path).read_bytes()
        if data[:4] != b"glTF" or struct.unpack("<I", data[8:12])[0] != len(data):
            return False
        jl = struct.unpack("<I", data[12:16])[0]
        json.loads(data[20:20 + jl])
        return True
    except Exception:
        return False


# --------------------------------------------------------------- .animated
def vec3(s):
    parts = [p.strip() for p in s.split(",")]
    return tuple(num(parts, i) for i in range(3))


def parse_animated(text):
    """Sections with both `key = value` pairs and, for [Include], bare file-name lines."""
    sections, cur = [], None
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            cur = {"sec": line.strip("[] ").lower(), "kv": {}, "lines": []}
            sections.append(cur)
        elif cur is not None:
            if "=" in line:
                k, v = line.split("=", 1)
                cur["kv"][k.strip().lower()] = v.strip()
                cur["lines"].append((k.strip().lower(), v.strip()))
            else:
                cur["lines"].append((None, line.strip('"')))     # bare file name
    return sections


def expand_animated(path, offset):
    base = Path(path).parent
    for e in parse_animated(read_text(path)):
        kv = e["kv"]
        sec_pos = vec3(kv["position"]) if "position" in kv else None
        if e["sec"] == "object":
            states = [x.strip().strip('"') for x in kv.get("states", "").split(",") if x.strip()]
            if states:
                pos = sec_pos or (0.0, 0.0, 0.0)
                yield states[0], tuple(offset[i] + pos[i] for i in range(3)), base
        elif e["sec"] == "include":
            # Accepts `File = x`, `File1 = x` + `Position1 = ...`, and bare file-name lines
            # (optionally followed by `Position = ...`, which then belongs to that file).
            entries = []
            default_pos = None                    # unnumbered Position before any file
            for k, v in e["lines"]:
                if k is None:
                    entries.append({"file": v, "n": None, "pos": None})
                    continue
                m = re.match(r"file(\d*)$", k)
                if m and v:
                    entries.append({"file": v.strip('"'), "n": m.group(1), "pos": None})
                    continue
                m = re.match(r"position(\d*)$", k)
                if m:
                    p = vec3(v)
                    if m.group(1):
                        for en in entries:
                            if en["n"] == m.group(1):
                                en["pos"] = p
                    elif entries and entries[-1]["pos"] is None and entries[-1]["n"] in (None, ""):
                        entries[-1]["pos"] = p
                    elif not entries:
                        default_pos = p
            for en in entries:
                p = en["pos"] or default_pos or (0.0, 0.0, 0.0)
                yield en["file"], tuple(offset[i] + p[i] for i in range(3)), base


def resolve_file(rel, base_dir):
    rel = rel.replace("\\", "/").strip().strip('"')
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    cur, ok = base_dir, bool(parts)
    for part in parts:
        if part == "..":
            cur = cur.parent
            continue
        nxt = None
        if cur.is_dir():
            if (cur / part).exists():
                nxt = cur / part
            else:
                for c in cur.iterdir():
                    if c.name.lower() == part.lower():
                        nxt = c
                        break
        if nxt is None:
            ok = False
            break
        cur = nxt
    if ok and cur.is_file():
        return cur
    if not parts:
        return None
    lname = parts[-1].lower()
    for root in (base_dir, base_dir.parent, base_dir.parent.parent):
        hit = _index(root)[0].get(lname)
        if hit:
            return hit
    return None


_PARSE_CACHE = {}
_USED_PARTS = set()      # object files already placed by some .animated file


def build_animated(path, flip, warn):
    g = Glb()
    cache = {}
    missing, unsupported = set(), set()
    total = [0]

    def place(ap, off, depth):
        entries = list(expand_animated(ap, off))
        if not entries:
            head = [l.strip() for l in read_text(ap).splitlines() if l.strip()][:2]
            warn("%s: no [Object]/[Include] entries found (starts with: %s)"
                 % (Path(ap).name, " | ".join(h[:60] for h in head) or "empty file"))
        for rel, pos, base in entries:
            target = resolve_file(rel, base)
            if target is None:
                missing.add(rel)
                continue
            ext = target.suffix.lower()
            if ext == ".animated":
                if depth < 6:
                    place(target, pos, depth + 1)
                continue
            if ext not in OBJ_EXTS:
                unsupported.add(target.name)
                continue
            key = str(target)
            _USED_PARTS.add(str(target.resolve()))
            if key not in cache:
                if key not in _PARSE_CACHE:
                    _PARSE_CACHE[key] = load_builders(target, warn)
                cache[key] = g.build_mesh(target.stem, _PARSE_CACHE[key],
                                          target.parent, flip, warn)
            mi, t = cache[key]
            if mi is None:
                continue
            g.nodes.append({"name": target.stem, "mesh": mi,
                            "translation": [pos[0], pos[1], -pos[2]]})
            total[0] += t

    place(Path(path), (0.0, 0.0, 0.0), 0)
    if missing:
        warn("referenced files not found: " + ", ".join(sorted(missing)[:8]))
    if unsupported:
        warn("unsupported object types skipped: " + ", ".join(sorted(unsupported)[:8]))
    return g, total[0]


# --------------------------------------------------------------- driver
def collect(paths):
    items, skipped = [], []
    for p in paths:
        p = Path(p).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        if not p.exists():
            print("warning: not found: %s" % p)
            continue
        if p.is_dir():
            root, cands = p, sorted(p.rglob("*"))
        else:
            root, cands = None, [p]
        for f in cands:
            if not f.is_file() or f.name.startswith("."):
                continue
            ext = f.suffix.lower()
            if ext in OBJ_EXTS or ext in ANIM_EXTS:
                items.append((f, root))
            elif ext in UNSUPPORTED_EXTS:
                skipped.append(f)
    return items, skipped


def default_outdir(root):
    return root.parent / (root.name + "_glb")


def convert(items, outdir, combine, flip, flat=False):
    made, no_geo, failed = [], [], []

    def out_dir(f, root):
        if flat:
            base = Path(outdir) if outdir else (default_outdir(root) if root else f.parent)
            if f.suffix.lower() in ANIM_EXTS:
                return base / ASSEMBLED_DIR                  # the complete models: start here
            rel = f.parent.relative_to(root) if root else Path()
            return base / LOOSE_DIR / rel                    # individual parts, mirrors the source tree
        if outdir:
            base = Path(outdir)
            return base / f.parent.relative_to(root) if root else base
        if root:
            return default_outdir(root) / f.parent.relative_to(root)
        return f.parent

    # Pre-compute how many files would land in the same (dir, stem) slot.
    stem_count = {}
    for f, root in items:
        k = (str(out_dir(f, root)), f.stem.lower())
        stem_count[k] = stem_count.get(k, 0) + 1

    # Assign a unique output name to each file.
    names, used = {}, set()
    for f, root in items:
        d = str(out_dir(f, root))
        base = f.stem
        if stem_count[(d, f.stem.lower())] > 1:
            try:
                rel = f.relative_to(root).parent.parts if root else ()
            except ValueError:
                rel = ()
            if rel:
                base = " - ".join(list(rel) + [f.stem])          # e.g. "Cars - TruckPivot - Truck"
            else:
                base = "%s_%s" % (f.stem, f.suffix[1:].lower())
        if (d, base.lower()) in used:
            i = 2
            while (d, ("%s_%d" % (base, i)).lower()) in used:
                i += 1
            base = "%s_%d" % (base, i)
        used.add((d, base.lower()))
        names[(f, root)] = base

    def tag(i, t):
        return "[%d]" % i if flat else "[%d/%d]" % (i, t)

    glb = Glb() if combine else None
    total = len(items)
    n = 0
    for f, root in items:
        label = str(f.relative_to(root)) if root else f.name
        if flat and f.suffix.lower() not in ANIM_EXTS and str(f.resolve()) in _USED_PARTS:
            total -= 1
            continue                      # already part of an assembled car
        n += 1
        msgs = []
        warn = msgs.append
        try:
            def build_one(part):
                global CUT_PART
                CUT_PART = part
                try:
                    if f.suffix.lower() in ANIM_EXTS:
                        return build_animated(f, flip, warn)
                    gg = Glb()
                    return gg, gg.add_object(f.stem, load_builders(f, warn), f.parent, flip, warn)
                finally:
                    CUT_PART = None

            g, tris = build_one("opaque" if SPLIT_CUTOUTS else None)
            if combine and glb is not None and tris > 0:
                glb.merge(g)
            if SPLIT_CUTOUTS:
                g2, tris2 = build_one("cutout")
                if tris2 > 0:
                    d2 = out_dir(f, root)
                    d2.mkdir(parents=True, exist_ok=True)
                    out2 = d2 / (names[(f, root)] + "_cutouts.glb")
                    g2.write(out2)
                    if verify_glb(out2):
                        made.append(out2)
                        print("%s %s: %d cut-out triangles -> %s" % (tag(n, total), label, tris2, out2.name))
                    else:
                        failed.append(label + " (cut-out file failed verification)")

            if tris == 0:
                no_geo.append(label)
                print("%s %s: no geometry, skipped" % (tag(n, total), label))
            else:
                print("%s %s: %d triangles" % (tag(n, total), label, tris))
                d = out_dir(f, root)
                d.mkdir(parents=True, exist_ok=True)
                out = d / (names[(f, root)] + ".glb")
                g.write(out)
                if verify_glb(out):
                    made.append(out)
                else:
                    failed.append(label + " (output failed verification)")
        except Exception as e:
            failed.append("%s (%s)" % (label, e))
            print("%s FAILED %s: %s" % (tag(n, total), label, e))
        for m in sorted(set(msgs)):
            print("    warning:", m)

    if combine and glb and glb.nodes:
        f0, root0 = items[0]
        if outdir:
            d = Path(outdir)
        elif root0:
            d = default_outdir(root0)
        else:
            d = f0.parent
        d.mkdir(parents=True, exist_ok=True)
        out = d / ((root0.name if root0 else f0.parent.name) + "_combined.glb")
        glb.write(out)
        if verify_glb(out):
            made.append(out)
        else:
            failed.append("combined file failed verification")
    return made, no_geo, failed


def pick_paths():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(
            title="Select the train folder (everything inside it gets converted)")
        paths = [folder] if folder else []
        if not paths:
            files = filedialog.askopenfilenames(
                title="...or select individual .b3d/.csv/.animated files",
                filetypes=[("BVE objects", "*.b3d *.csv *.animated")])
            paths = list(files)
        root.destroy()
        return paths
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser(
        description="Convert BVE .b3d/.csv objects to GLB. Pass a folder to convert everything inside it.")
    ap.add_argument("paths", nargs="*", help="folders (searched recursively) and/or files")
    ap.add_argument("-o", "--outdir", help="output folder (default: <folder>_glb next to the input folder)")
    ap.add_argument("--combine", action="store_true", help="write one GLB containing everything")
    ap.add_argument("--flip", action="store_true", help="reverse face winding if faces look inside-out")
    ap.add_argument("--parts", action="store_true",
                    help="also write every loose .b3d/.csv part (default: only assembled .animated cars)")
    ap.add_argument("--double-sided", action="store_true",
                    help="make every face visible from both sides (default: like BVE, only "
                         "Face2/AddFace2 are two-sided; one-sided faces are culled from behind)")
    ap.add_argument("--match", metavar="PATTERN",
                    help='only convert .animated files whose name matches, e.g. --match "9525*"')
    ap.add_argument("--no-loose", action="store_true",
                    help="do not export loose parts that no .animated file uses")
    ap.add_argument("--key-tolerance", type=int, default=None, metavar="N",
                    help="how far (0-255) a pixel may differ from the transparent colour and still be "
                         "cut out. Default: automatic (exact for lossless files). Try 0 if dark "
                         "parts vanish, or 60-100 if window cut-outs stay solid.")
    ap.add_argument("--keep-cutouts", action="store_true",
                    help="keep triangles that are 100%% transparent (default: remove them; they are "
                         "invisible anyway, and Roblox shows them as solid because it ignores alpha)")
    ap.add_argument("--split-cutouts", action="store_true",
                    help="write partly transparent parts (grilles, straps, glass) to a separate "
                         "<name>_cutouts.glb, so they can be handled separately (e.g. in Roblox)")
    ap.add_argument("--smooth-angle", type=float, default=45.0, metavar="DEG",
                    help="smooth shading: faces meeting at less than this angle share normals "
                         "(default 45; 0 = flat shading like before)")
    ap.add_argument("--single-sided", action="store_true", help=argparse.SUPPRESS)  # old option, now the default
    a = ap.parse_args()
    global FORCE_DOUBLE_SIDED, KEY_TOLERANCE, CUT_CLEAR_FACES, SPLIT_CUTOUTS, SMOOTH_ANGLE
    SMOOTH_ANGLE = max(0.0, a.smooth_angle)
    SPLIT_CUTOUTS = a.split_cutouts
    KEY_TOLERANCE = a.key_tolerance
    CUT_CLEAR_FACES = not a.keep_cutouts
    FORCE_DOUBLE_SIDED = a.double_sided and not a.single_sided
    paths = a.paths or pick_paths()
    if not paths:
        sys.exit("Nothing selected.")
    items, skipped = collect(paths)
    if not items:
        sys.exit("No .b3d/.csv/.animated files found.")
    combine = a.combine
    has_anim = any(f.suffix.lower() in ANIM_EXTS for f, _ in items)
    flat = has_anim and not a.parts
    if flat:
        anim = [(f, r) for f, r in items if f.suffix.lower() in ANIM_EXTS]
        parts = [(f, r) for f, r in items if f.suffix.lower() not in ANIM_EXTS]
        if a.match:
            import fnmatch
            pat = a.match.lower()
            anim = [(f, r) for f, r in anim
                    if fnmatch.fnmatch(f.name.lower(), pat) or fnmatch.fnmatch(f.stem.lower(), pat)
                    or fnmatch.fnmatch(str(f.relative_to(r) if r else f.name).lower(), pat)]
            if not anim:
                sys.exit("--match %r matched no .animated file." % a.match)
        if a.no_loose or a.match:
            parts = []
        items = anim + parts              # .animated first, so we know which parts they use
        print("Found .animated files: they are exported as complete models; parts they already "
              "contain are skipped%s." % ("" if not parts else ", other parts go to '%s/'" % LOOSE_DIR))
    print("Converting %d object file(s)...\n" % len(items))
    made, no_geo, failed = convert(items, a.outdir, combine, a.flip, flat)
    print("\n=== Done: %d GLB file(s) written ===" % len(made))
    if made:
        p0 = Path(paths[0]).expanduser().resolve()
        outroot = (Path(a.outdir) if a.outdir
                   else (default_outdir(p0) if p0.is_dir() else made[0].parent))
        print("Output folder:", outroot)
        n_asm = sum(1 for m in made if ASSEMBLED_DIR in m.parts)
        n_loose = sum(1 for m in made if LOOSE_DIR in m.parts)
        if n_asm:
            print("\n  START HERE  -> %s%s%s   (%d complete model(s); import these)"
                  % (outroot, "\\" if "\\" in str(outroot) else "/", ASSEMBLED_DIR, n_asm))
        if n_loose:
            print("  Optional    -> %s%s%s   (%d individual part(s), mostly not needed)"
                  % (outroot, "\\" if "\\" in str(outroot) else "/", LOOSE_DIR, n_loose))
    if no_geo:
        print("No geometry in %d file(s)" % len(no_geo))
    if failed:
        print("FAILED %d file(s):" % len(failed))
        for x in failed[:20]:
            print("  ", x)
    if not made:
        sys.exit("No GLB files were created.")


if __name__ == "__main__":
    interactive = len(sys.argv) == 1
    code = 0
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            print(e.code)
            code = 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        code = 1
    except Exception:
        traceback.print_exc()
        code = 1
    if interactive:
        try:
            input("\nPress Enter to close...")
        except (EOFError, OSError):
            pass
    sys.exit(code)