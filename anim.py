bl_info = {
    "name": "Cars 2 Animation (.oct)",
    "author": "Pentane project",
    "version": (1, 3, 0),
    "blender": (3, 0, 0),
    "location": "File > Import/Export > Cars 2 Animation (.oct); "
                "3D View > Sidebar > Cars 2 Character",
    "description": "Import and export Avalanche .oct animation clips, "
                   "including eyelid/mouth sliders",
    "category": "Import-Export",
}

import base64
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper


# ===========================================================================
# ClipDataBlock codec
# ===========================================================================
#
# Command word 0 is a bitfield:
#   bits  0-2   pre-extrapolation mode
#   bits  3-5   post-extrapolation mode
#   bits  6-9   opcode
#   bits 10-19  channel count
#   bits 20-31  key count
#   word[1] tStart (f32), word[2] tEnd (f32)
#
# Opcode 0 stores 4 f32 per channel (endSlope, endValue, bias, scale),
# u16 normalised key times, then 4 i16 per channel per key. Opcode 4 keeps
# u16 times but uses f32 samples; opcode 6 is fully uncompressed.
#
# The four values per channel per key are successive deltas of cubic Bezier
# control points, which is exactly a Blender F-curve segment. Key k's
# segment runs to key k+1, and the LAST key's segment runs to t_end - it is
# part of the clip, not extrapolation data. In all 692 channels of the 49
# shipped facial/frontend clips the last key sits before t_end, endValue is
# that final segment's P3 exactly, and endSlope is its outgoing slope
# (3 * (P3 - P2) / span) wherever the span is long enough to measure. Those
# two are what post-extrapolation continues from; every shipped command uses
# CLAMP at both ends, so in practice the curve holds endValue after t_end.
#
# The deltas are summed in at least 32 bits: running sums reach +/-49986 in
# shipped data. P0 and every delta must fit an i16; intermediate control
# points need not.

EXTRAPOLATION = {0: "CLAMP", 1: "LINEAR", 2: "LOOP", 3: "LOOP_OFFSET", 4: "PINGPONG"}

# Rotation bindings store angles in RADIANS, so nothing is scaled on the way
# in. An earlier version converted degrees to radians on the strength of an
# end-slope parameter read as -360.0; no parameter in any shipped clip
# is within 0.5 of +/-360, and thirty-five are within 0.01 of +/-2*pi, which
# is what that actually was. Measured against the clips: a 180 trick spans
# exactly pi and a three-turn spinout exactly 6*pi.
VALUE_SCALE = {}

# Rotation order lives in bits 6-8 of an 0x1000 sub-word, as Maya's
# rotateOrder enum: xyz, yzx, zxy, xzy, yxz, zyx. Shipped Cars 2 clips use 2
# (zxy) throughout, but converted clips (e.g. the Cars 3 DTW bank) also use 0
# (xyz) on wheels and body bones, so the order is read per sub-op, kept per
# bone on import and written back on export. EULER_ORDER is only the default
# for bones with no recorded order.
EULER_ORDER = "ZXY"
MAYA_ORDERS = ("XYZ", "YZX", "ZXY", "XZY", "YXZ", "ZYX")
ORDER_CODE = {name: i for i, name in enumerate(MAYA_ORDERS)}


def euler_order_of(sub_word):
    code = (sub_word >> 6) & 7
    return MAYA_ORDERS[code] if code < len(MAYA_ORDERS) else EULER_ORDER


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _i16(b, o):
    return struct.unpack_from("<h", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _f32(b, o):
    return struct.unpack_from("<f", b, o)[0]


def command_size(opcode, n_ch, n_keys):
    if opcode == 0:
        n = 12 + n_ch * 16 + n_keys * 2 + n_keys * n_ch * 8
        return (n + 3) & ~3
    if opcode == 4:
        return (12 + n_ch * 8 + n_keys * 2 + (2 if n_keys & 1 else 0)
                + n_keys * n_ch * 16)
    if opcode == 6:
        n = 12 + n_ch * 8 + n_keys * 4 + n_keys * n_ch * 16
        return (n + 3) & ~3
    raise ValueError("unsupported opcode %d" % opcode)


def deltas_to_bezier(d):
    p0 = d[0]
    p1 = p0 + d[1]
    p2 = p1 + d[2]
    return [p0, p1, p2, p2 + d[3]]


def bezier_to_deltas(p):
    return [p[0], p[1] - p[0], p[2] - p[1], p[3] - p[2]]


def decode_command(b, off):
    w = _u32(b, off)
    n_ch = (w & 0xFFC00) >> 10
    n_keys = w >> 20
    cmd = {
        "pre": w & 7,
        "post": (w >> 3) & 7,
        "opcode": (w & 0x3C0) >> 6,
        "n_channels": n_ch,
        "n_keys": n_keys,
        "t_start": _f32(b, off + 4),
        "t_end": _f32(b, off + 8),
        "pad": b"",
    }
    op = cmd["opcode"]
    cmd["size"] = command_size(op, n_ch, n_keys)

    if op == 0:
        cmd["params"] = [[_f32(b, off + 12 + 16 * c + 4 * j) for j in range(4)]
                         for c in range(n_ch)]
        kt = off + 12 + n_ch * 16
        cmd["times"] = [_u16(b, kt + 2 * k) for k in range(n_keys)]
        sm = kt + n_keys * 2
        cmd["raw"] = [[[_i16(b, sm + (k * n_ch + c) * 8 + 2 * j) for j in range(4)]
                       for c in range(n_ch)] for k in range(n_keys)]
        used = 12 + n_ch * 16 + n_keys * 2 + n_keys * n_ch * 8
        cmd["pad"] = b[off + used:off + cmd["size"]]
    elif op == 4:
        cmd["params"] = [[_f32(b, off + 12 + 8 * c + 4 * j) for j in range(2)]
                         for c in range(n_ch)]
        kt = off + 12 + n_ch * 8
        cmd["times"] = [_u16(b, kt + 2 * k) for k in range(n_keys)]
        cmd["pad"] = b[kt + n_keys * 2:kt + n_keys * 2 + (2 if n_keys & 1 else 0)]
        sm = kt + n_keys * 2 + (2 if n_keys & 1 else 0)
        cmd["raw"] = [[[_f32(b, sm + (k * n_ch + c) * 16 + 4 * j) for j in range(4)]
                       for c in range(n_ch)] for k in range(n_keys)]
    else:
        cmd["params"] = [[_f32(b, off + 12 + 8 * c + 4 * j) for j in range(2)]
                         for c in range(n_ch)]
        kt = off + 12 + n_ch * 8
        cmd["times"] = [_f32(b, kt + 4 * k) for k in range(n_keys)]
        sm = kt + n_keys * 4
        cmd["raw"] = [[[_f32(b, sm + (k * n_ch + c) * 16 + 4 * j) for j in range(4)]
                       for c in range(n_ch)] for k in range(n_keys)]
    return cmd


def encode_command(cmd):
    op, n_ch, n_keys = cmd["opcode"], cmd["n_channels"], cmd["n_keys"]
    w = ((cmd["pre"] & 7) | ((cmd["post"] & 7) << 3) | ((op & 0xF) << 6)
         | ((n_ch & 0x3FF) << 10) | ((n_keys & 0xFFF) << 20))
    out = bytearray(struct.pack("<Iff", w, cmd["t_start"], cmd["t_end"]))

    if op == 0:
        for c in range(n_ch):
            out += struct.pack("<4f", *cmd["params"][c])
        for t in cmd["times"]:
            out += struct.pack("<H", t)
        for k in range(n_keys):
            for c in range(n_ch):
                out += struct.pack("<4h", *cmd["raw"][k][c])
    elif op == 4:
        for c in range(n_ch):
            out += struct.pack("<2f", *cmd["params"][c])
        for t in cmd["times"]:
            out += struct.pack("<H", t)
        if n_keys & 1:
            out += (cmd.get("pad") or b"\xee\xee")[:2].ljust(2, b"\xee")
        for k in range(n_keys):
            for c in range(n_ch):
                out += struct.pack("<4f", *cmd["raw"][k][c])
    else:
        for c in range(n_ch):
            out += struct.pack("<2f", *cmd["params"][c])
        for t in cmd["times"]:
            out += struct.pack("<f", t)
        for k in range(n_keys):
            for c in range(n_ch):
                out += struct.pack("<4f", *cmd["raw"][k][c])

    pad = -len(out) % 4
    if pad:
        out += (cmd.get("pad") or b"\xee" * pad)[:pad].ljust(pad, b"\xee")
    return bytes(out)


def decode_clip(b):
    n_binds = _u16(b, 8)
    n_cmds = _u16(b, 10)
    pool = _u32(b, 12)
    offsets = [_u32(b, 16 + 4 * i) for i in range(n_binds + n_cmds)]
    bounds = sorted(offsets) + [pool]
    clip = {
        "flags": _u32(b, 0),
        "duration": _f32(b, 4),
        "n_binds": n_binds,
        "pool_offset": pool,
        "commands": [decode_command(b, o) for o in offsets[n_binds:]],
        "pool": b[pool:],
        "binds": [b[o:min(x for x in bounds if x > o)] for o in offsets[:n_binds]],
    }
    return clip


def encode_clip(clip):
    n_binds = clip["n_binds"]
    n_cmds = len(clip["commands"])
    cursor = 16 + 4 * (n_binds + n_cmds)

    body = bytearray()
    offsets = []
    for blob in clip["binds"]:
        offsets.append(cursor)
        body += blob
        cursor += len(blob)
    for cmd in clip["commands"]:
        offsets.append(cursor)
        blob = encode_command(cmd)
        body += blob
        cursor += len(blob)

    out = bytearray(struct.pack("<IfHHI", clip["flags"], clip["duration"],
                                n_binds, n_cmds, cursor))
    for o in offsets:
        out += struct.pack("<I", o)
    return bytes(out + body + clip["pool"])


def channel_curve(cmd, channel):
    """[(time_seconds, [P0,P1,P2,P3])] for one channel, in real units.

    A sentinel key at t_end, holding the final segment's P3, closes the list,
    so "each key's segment runs to the next key" is true for every real key
    and anything sampled at or past t_end holds the end value (CLAMP).
    """
    op = cmd["opcode"]
    span = cmd["t_end"] - cmd["t_start"]
    bias, scale = (cmd["params"][channel][2], cmd["params"][channel][3]) \
        if op == 0 else (0.0, 1.0)
    keys = []
    for k in range(cmd["n_keys"]):
        if op == 6:
            t = cmd["times"][k]
        else:
            t = cmd["t_start"] + span * cmd["times"][k] / 65535.0
        pts = deltas_to_bezier(cmd["raw"][k][channel])
        keys.append((t, [bias + scale * p for p in pts]))
    if keys and cmd["t_end"] > keys[-1][0]:
        end = keys[-1][1][3]
        keys.append((cmd["t_end"], [end, end, end, end]))
    return keys


def quantize_segments(segments):
    """(bias, scale) so every P0 and every delta of every segment fits an i16.

    Sizing the scale from the points alone (as an earlier version did) lets
    a delta reach nearly twice the i16 range, and clamping it then silently
    flattens the segment. One count of margin absorbs rounding.
    """
    flat = [v for seg in segments for v in seg]
    bias = (min(flat) + max(flat)) * 0.5
    reach = max(abs(v - bias) for v in flat)
    step = max([abs(b - a) for seg in segments for a, b in zip(seg, seg[1:])]
               or [0.0])
    scale = max(reach, step) / 32766.0
    return bias, (scale if scale > 0.0 else 1.0)


def encode_segment(seg, bias, scale):
    """Round the absolute points, then difference, so decoding sums exactly
    back to the rounded points instead of accumulating rounding error."""
    ints = [int(round((v - bias) / scale)) for v in seg]
    return [max(-32768, min(32767, d)) for d in bezier_to_deltas(ints)]


# ---------------------------------------------------------------------------
# Fitting sampled values back to stored curves
# ---------------------------------------------------------------------------

def _bezier(p, u):
    v = 1.0 - u
    return v * v * v * p[0] + 3 * v * v * u * p[1] + 3 * v * u * u * p[2] + u * u * u * p[3]


def _segment_points(values, a, b, h, ta, tb, left=None):
    """Hermite segment over sample indices [a, b] as Bezier points.

    ta / tb are the times the decoder will actually use for the segment ends
    (key times are u16 fractions of t_end, so they land up to ~25us off the
    sample grid); the end values are slid along their slopes to match, which
    matters on fast curves.

    `left` is the value just before sample b where the source steps there
    (a key after constant interpolation). The format stores P3 and the next
    P0 independently, so the step lands exactly on the key instead of being
    smeared across the segment before it.
    """
    p0 = values[a]
    p3 = values[b] if left is None else left
    n = b - a
    if n >= 2:
        s0 = (-3.0 * p0 + 4.0 * values[a + 1] - values[a + 2]) / (2.0 * h)
        s1 = (3.0 * p3 - 4.0 * values[b - 1] + values[b - 2]) / (2.0 * h)
    else:
        s0 = s1 = (p3 - p0) / h
    p0 += s0 * (ta - a * h)
    p3 += s1 * (tb - b * h)
    dt = tb - ta
    return [p0, p0 + s0 * dt / 3.0, p3 - s1 * dt / 3.0, p3]


def key_time(index, h, t_end):
    """The time a key placed at sample `index` decodes to."""
    return min(65534, int(round(index * h / t_end * 65535.0))) * t_end / 65535.0


def _segment_error(series, segs_for, a, b, ta, tb, h, tols):
    """Worst error/tolerance over samples strictly inside [a, b], and where."""
    span = tb - ta
    worst, at = 0.0, None
    for c, v in enumerate(series):
        p = segs_for[c]
        tol = tols[c]
        for i in range(a + 1, b):
            u = min(max((i * h - ta) / span, 0.0), 1.0)
            e = abs(_bezier(p, u) - v[i]) / tol
            if e > worst:
                worst, at = e, i
    return worst, at


def fit_keys(series, h, keys, tols, adaptive=True, max_keys=4095, lefts=None):
    """Choose key sample indices and Bezier points for a group of channels.

    series  - per channel, values at sample indices 0..N (N * h = t_end)
    keys    - starting key indices (the animator's keyframes); 0 is added
    tols    - per channel maximum error
    lefts   - {sample index: [value just before it, per channel]} where the
              source steps; those indices are kept as keys

    Every channel in the group shares the key times, since one command
    stores one time list. With `adaptive`, a segment whose cubic misses any
    sample by more than its channel's tolerance is split at the worst sample
    until everything fits. Returns (key_indices, [[segment points] per key]
    per channel, max error relative to tolerance).
    """
    n = len(series[0]) - 1
    t_end = n * h
    lefts = lefts or {}
    keys = sorted({k for k in keys if 0 <= k < n} | {k for k in lefts if 0 < k < n} | {0})
    while True:
        bounds = keys + [n]
        times = [key_time(k, h, t_end) for k in keys] + [t_end]
        segs = _build_segments(series, h, bounds, times, lefts)
        splits = []
        worst_all = 0.0
        for j, (a, b) in enumerate(zip(bounds, bounds[1:])):
            worst, at = _segment_error(series, [s[j] for s in segs], a, b,
                                       times[j], times[j + 1], h, tols)
            worst_all = max(worst_all, worst)
            if worst > 1.0 and at is not None:
                splits.append(at)
        if not adaptive or len(keys) + len(splits) > max_keys:
            return keys, segs, worst_all
        if not splits:
            break
        keys = sorted(set(keys) | set(splits))
    return _reduce_keys(series, h, keys, tols, lefts)


def _build_segments(series, h, bounds, times, lefts):
    return [[_segment_points(v, a, b, h, times[j], times[j + 1],
                             lefts[b][c] if b in lefts else None)
             for j, (a, b) in enumerate(zip(bounds, bounds[1:]))]
            for c, v in enumerate(series)]


def _reduce_keys(series, h, keys, tols, lefts, headroom=0.8):
    """Drop keys whose neighbours already describe the motion.

    The starting keys are the animator's, and an imported clip is baked with
    one key per frame; most of those are redundant once the curve is stored
    as cubics. A key goes only if the merged segment stays inside the
    tolerance (with headroom for the 16-bit quantisation still to come), so
    steps and sharp turns keep theirs.
    """
    n = len(series[0]) - 1
    t_end = n * h
    kept = [keys[0]]
    j = 1
    while j < len(keys):
        if keys[j] in lefts:
            kept.append(keys[j])
            j += 1
            continue
        a = kept[-1]
        b_next = keys[j + 1] if j + 1 < len(keys) else n
        ta = key_time(a, h, t_end)
        tb = key_time(b_next, h, t_end) if b_next < n else t_end
        merged = [_segment_points(v, a, b_next, h, ta, tb,
                                  lefts[b_next][c] if b_next in lefts else None)
                  for c, v in enumerate(series)]
        worst, _at = _segment_error(series, merged, a, b_next, ta, tb, h, tols)
        if worst > headroom:
            kept.append(keys[j])
        j += 1
    bounds = kept + [n]
    times = [key_time(k, h, t_end) for k in kept] + [t_end]
    segs = _build_segments(series, h, bounds, times, lefts)
    worst_all = 0.0
    for j, (a, b) in enumerate(zip(bounds, bounds[1:])):
        worst, _at = _segment_error(series, [s[j] for s in segs], a, b,
                                    times[j], times[j + 1], h, tols)
        worst_all = max(worst_all, worst)
    return kept, segs, worst_all


def encode_command_channels(key_indices, segs_per_channel, h, t_end):
    """An opcode-0 command dict from fitted segments (see fit_keys)."""
    n_ch = len(segs_per_channel)
    params, raw_by_channel = [], []
    for segs in segs_per_channel:
        bias, scale = quantize_segments(segs)
        raw_by_channel.append([encode_segment(seg, bias, scale) for seg in segs])
        last = segs[-1]
        span = (t_end - key_time(key_indices[-1], h, t_end)) or 1.0
        params.append([3.0 * (last[3] - last[2]) / span, last[3], bias, scale])
    times = [min(65534, int(round(k * h / t_end * 65535.0))) for k in key_indices]
    return {
        "pre": 0, "post": 0, "opcode": 0,
        "n_channels": n_ch, "n_keys": len(key_indices),
        "t_start": 0.0, "t_end": t_end, "pad": b"",
        "params": params, "times": times,
        "raw": [[raw_by_channel[c][k] for c in range(n_ch)]
                for k in range(len(key_indices))],
    }


# ===========================================================================
# Container backends
# ===========================================================================

_PREFIX_FORMATS = (("<H", 2), ("<I", 4), ("<B", 1))
HEADER_SIZE = 0x3C
MAGIC = 0x45017629


def find_offsetting():
    for name in ("offsetting", "offsetting.exe"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _run(argv):
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("%s failed: %s" % (argv[0], proc.stderr.strip()))


def _find_in_json(node):
    """Locate the ClipDataBlock string inside matryoshka JSON."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "ClipDataBlock" and isinstance(value, str):
                return node, key
            found = _find_in_json(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_in_json(item)
            if found:
                return found
    return None


def read_block_offsetting(exe, path, tmp):
    js = os.path.join(tmp, "in.json")
    _run([exe, "oct", "decode", path, js])
    with open(js) as handle:
        data = json.load(handle)
    parent, key = _find_in_json(data)
    return base64.b64decode(parent[key][len("base64:"):] + "==")


def write_block_offsetting(exe, path, block, out_path, tmp):
    js = os.path.join(tmp, "io.json")
    _run([exe, "oct", "decode", path, js])
    with open(js) as handle:
        data = json.load(handle)
    parent, key = _find_in_json(data)
    parent[key] = "base64:" + base64.b64encode(block).decode().rstrip("=")
    with open(js, "w") as handle:
        json.dump(data, handle)
    _run([exe, "oct", "encode", js, out_path])


def _header(raw):
    magic, _version, _reserved, str_size, data_size = struct.unpack_from("<IfIII", raw, 0)
    if magic != MAGIC:
        raise ValueError("not an Avalanche scene file")
    return str_size, data_size


def find_block(raw, block):
    """Blocks start with a zero flags field, so a plain search can match up
    to three bytes early. Anchor on the length prefix instead."""
    for fmt, width in _PREFIX_FORMATS:
        try:
            needle = struct.pack(fmt, len(block)) + block
        except struct.error:
            continue
        i = raw.find(needle)
        if i >= 0:
            return i, i + width, i + width + len(block), fmt
    raise ValueError("block not found")


def read_block_native(path):
    raw = open(path, "rb").read()
    str_size, data_size = _header(raw)
    start, end = HEADER_SIZE + str_size, HEADER_SIZE + str_size + data_size
    best = None
    o = start
    while o < end - 8:
        for fmt, width in _PREFIX_FORMATS:
            try:
                n = struct.unpack_from(fmt, raw, o)[0]
            except struct.error:
                continue
            if not 24 <= n <= end - o - width:
                continue
            body = o + width
            try:
                duration = _f32(raw, body + 4)
                n_binds, n_cmds = struct.unpack_from("<HH", raw, body + 8)
                pool = _u32(raw, body + 12)
            except struct.error:
                continue
            table_end = 16 + 4 * (n_binds + n_cmds)
            if not (1 <= n_binds <= 16 and n_cmds <= 4096 and 16 <= pool <= n
                    and 0.0 < duration < 600.0 and table_end <= n):
                continue
            offs = struct.unpack_from("<%dI" % (n_binds + n_cmds), raw, body + 16)
            if any(not table_end <= x <= pool for x in offs):
                continue
            if best is None or n > best[1] - best[0]:
                best = (body, body + n)
        o += 1
    if best is None:
        raise ValueError("no ClipDataBlock found in %s" % path)
    return raw[best[0]:best[1]]


def write_block_native(path, block, out_path):
    """Replace the ClipDataBlock, resizing the container when needed.

    The Tupperware node tree is a flat sequence with a depth level per node
    and no absolute offsets, so growing a payload only means fixing the
    node's length prefix, its len_size header field if the prefix must
    widen, and the header's data-tree size.

    Node header (u16, LSB first), per matryoshka:
        bits 0-1 type, bit 2 has-name, bits 3-5 data type,
        bits 6-7 len_size (+1 bytes), bits 8-9 int_size, bits 10-15 level
    """
    raw = open(path, "rb").read()
    old = read_block_native(path)
    i, _s, e, _fmt = find_block(raw, old)

    header_off = i - 4
    header = struct.unpack_from("<H", raw, header_off)[0]
    old_width = ((header >> 6) & 3) + 1

    new_len = len(block)
    for width in (1, 2, 4):
        if new_len < (1 << (8 * width)):
            break
    else:
        raise ValueError("block too large for any length prefix")
    width = max(width, old_width)

    prefix = new_len.to_bytes(width, "little")
    out = bytearray(raw)
    out[i:e] = prefix + block

    if width != old_width:
        header = (header & ~(3 << 6)) | ((width - 1) << 6)
        struct.pack_into("<H", out, header_off, header)

    data_size = struct.unpack_from("<I", raw, 0x10)[0]
    struct.pack_into("<I", out, 0x10,
                     data_size + (len(prefix) + new_len) - (e - i))
    open(out_path, "wb").write(bytes(out))


# ===========================================================================
# Built-in channel maps
# ===========================================================================
#
# Channel indices are assigned when the engine wires a ClipProcessor to a rig
# and appear in no file on disk. These were captured from a running game with
# the DumpOutputNodes hook, which logs each output node's full scene path:
#
#   |Actors|car/McQueen|mcqueen|Interfaces|Facial|OutputNodes|Anchor/Jaw_Ctrl/Transform
#
# The path names the interface, so each map is identified directly rather than
# matched by channel count. Clips name their connection template in their own
# string table, so the right map is chosen per file and applies to every car.
#
# Facial and Eyelids share one 68-channel output list; their .bent filters
# differ (Eyelids is active on the brow and eyelid controls only).

BUILTIN_CHANNEL_MAPS = {
    "car_body": {
        0: "BackEnd", 1: "BackTires", 2: "BLeft_bttm", 3: "BLeft_Center",
        4: "Body2", 5: "Body3", 6: "Body_Baxle", 7: "Body", 8: "BRight_bttm",
        9: "BRight_Center", 10: "FLeft_bttm", 11: "FLeft_Center",
        12: "FRight_bttm", 13: "FRight_Center", 14: "FrontEnd", 15: "FrontTires",
        16: "FX_Marker_Battery_Center", 17: "FX_Marker_Battery_Left",
        18: "FX_Marker_Battery_Right", 19: "FX_Marker_Bumper",
        20: "FX_Marker_Exhaust_Left1", 21: "FX_Marker_Exhaust_Left2",
        22: "FX_Marker_Exhaust_Right1", 23: "FX_Marker_Exhaust_Right2",
        24: "FX_Marker_Headlight_Left", 25: "FX_Marker_Headlight_Right",
        26: "FX_Marker_MachineGun_Left", 27: "FX_Marker_MachineGun_Right",
        28: "FX_Marker_SkateJack", 29: "FX_Marker_SpareTire",
        30: "FX_Marker_WGPCam", 31: "Main_Baxle", 32: "Main", 33: "MainOrient",
        34: "TCable_Pulley", 35: "TowCrane", 36: "TowCraneBase",
    },
    "car_bodyblend": {
        0: "BackTires", 1: "BLeft_bttm", 2: "BLeft_Center", 3: "Body2",
        4: "Body3", 5: "Body", 6: "BRight_bttm", 7: "BRight_Center",
        8: "FLeft_bttm", 9: "FLeft_Center", 10: "FRight_bttm",
        11: "FRight_Center", 12: "FrontEnd", 13: "FrontTires", 14: "Main",
        15: "MainOrient",
    },
    "car_choblender": {
        0: "Body3", 1: "MainOrient",
    },
    "car_choreography": {
        0: "KinematicState", 2: "BackEnd", 3: "BackTires", 4: "BLeft_bttm",
        5: "BLeft_Center", 6: "Body2", 7: "Body3", 8: "Body_Baxle", 9: "Body",
        10: "BRight_bttm", 11: "BRight_Center", 12: "BrowCenter",
        13: "BrowCornerLeft", 14: "BrowCornerRight", 15: "BrowMidLeft",
        16: "BrowMidRight", 17: "CheekLowerLeft", 18: "CheekLowerRight",
        19: "CheekUpperLeft", 20: "CheekUpperRight", 21: "ControlHandLeft",
        22: "ControlHandRight", 23: "Eyes_Ctrl/BTM_CNTR_Notch",
        24: "Eyes_Ctrl/BTM_L_Bow", 25: "Eyes_Ctrl/BTM_L_Bow_Angle",
        26: "Eyes_Ctrl/BTM_L_Bow_InOut", 27: "Eyes_Ctrl/BTM_L_Edge",
        28: "Eyes_Ctrl/BTM_L_Edge_Angle", 29: "Eyes_Ctrl/BTM_L_Notch",
        30: "Eyes_Ctrl/BTM_L_Notch_Angle", 31: "Eyes_Ctrl/BTM_R_Bow",
        32: "Eyes_Ctrl/BTM_R_Bow_Angle", 33: "Eyes_Ctrl/BTM_R_Bow_InOut",
        34: "Eyes_Ctrl/BTM_R_Edge", 35: "Eyes_Ctrl/BTM_R_Edge_Angle",
        36: "Eyes_Ctrl/BTM_R_Notch", 37: "Eyes_Ctrl/BTM_R_Notch_Angle",
        38: "Eyes_Ctrl/EyeLeft", 39: "Eyes_Ctrl/EyeLeftVertical",
        40: "Eyes_Ctrl/EyeRight", 41: "Eyes_Ctrl/Monocle",
        42: "Eyes_Ctrl/MonocleInOut", 43: "Eyes_Ctrl/TOP_CNTR_Notch",
        44: "Eyes_Ctrl/TOP_L_Bow", 45: "Eyes_Ctrl/TOP_L_Bow_Angle",
        46: "Eyes_Ctrl/TOP_L_Bow_InOut", 47: "Eyes_Ctrl/TOP_L_Edge",
        48: "Eyes_Ctrl/TOP_L_Edge_Angle", 49: "Eyes_Ctrl/TOP_L_Notch",
        50: "Eyes_Ctrl/TOP_L_Notch_Angle", 51: "Eyes_Ctrl/TOP_L_Notch_InOut",
        52: "Eyes_Ctrl/TOP_R_Bow", 53: "Eyes_Ctrl/TOP_R_Bow_Angle",
        54: "Eyes_Ctrl/TOP_R_Bow_InOut", 55: "Eyes_Ctrl/TOP_R_Edge",
        56: "Eyes_Ctrl/TOP_R_Edge_Angle", 57: "Eyes_Ctrl/TOP_R_Notch",
        58: "Eyes_Ctrl/TOP_R_Notch_Angle", 59: "Eyes_Ctrl/TOP_R_Notch_InOut",
        60: "Eyes", 61: "Eyes_Frame_Proxy", 62: "FLeft_bttm", 63: "FLeft_Center",
        64: "FRight_bttm", 65: "FRight_Center", 66: "FrontEnd", 67: "FrontTires",
        68: "Jaw", 69: "LipLowerCenter", 70: "LipLowerCornerLeft",
        71: "LipLowerCornerRight", 72: "LipLowerMidLeft", 73: "LipLowerMidRight",
        74: "LipUpperCenter", 75: "LipUpperCornerLeft",
        76: "LipUpperCornerRight", 77: "LipUpperMidLeft", 78: "LipUpperMidRight",
        79: "Main_Baxle", 80: "Main_Ctrl/RotateFront",
        81: "Main_Ctrl/RotateRear", 82: "Main", 83: "MainOrient",
        84: "Mouth_Ctrl/Mth_Corner_L", 85: "Mouth_Ctrl/Mth_Corner_R",
        86: "Mouth_Ctrl/Mth_Expand_L", 87: "Mouth_Ctrl/Mth_Expand_R",
        88: "Mouth_Ctrl/Mth_Offest", 89: "Mouth_Ctrl/Mth_Offset",
        90: "Mouth_Ctrl/vsm_AA", 91: "Mouth_Ctrl/vsm_AH",
        92: "Mouth_Ctrl/vsm_AO", 93: "Mouth_Ctrl/vsm_CH",
        94: "Mouth_Ctrl/vsm_EH", 95: "Mouth_Ctrl/vsm_ER",
        96: "Mouth_Ctrl/vsm_EY", 97: "Mouth_Ctrl/vsm_F", 98: "Mouth_Ctrl/vsm_IH",
        99: "Mouth_Ctrl/vsm_L", 100: "Mouth_Ctrl/vsm_M", 101: "Mouth_Ctrl/vsm_N",
        102: "Mouth_Ctrl/vsm_OY", 103: "Mouth_Ctrl/vsm_W",
        104: "Mouth_Ctrl/vsm_Y", 105: "TCable_Pulley", 106: "TeethLower",
        107: "TeethUpper", 108: "Tongue1", 109: "Tongue2", 110: "TongueRoot",
        111: "TowCrane", 112: "TowCraneBase",
    },
    "car_eyelids": {  # same output list as car_facial; only the .bent filter masks differ
        0: "BrowCenter", 1: "BrowCornerLeft", 2: "BrowCornerRight",
        3: "BrowMidLeft", 4: "BrowMidRight", 5: "CheekLowerLeft",
        6: "CheekLowerRight", 7: "CheekUpperLeft", 8: "CheekUpperRight",
        9: "Eyes_Ctrl/BTM_CNTR_Notch", 10: "Eyes_Ctrl/BTM_L_Bow",
        11: "Eyes_Ctrl/BTM_L_Bow_Angle", 12: "Eyes_Ctrl/BTM_L_Bow_InOut",
        13: "Eyes_Ctrl/BTM_L_Edge", 14: "Eyes_Ctrl/BTM_L_Edge_Angle",
        15: "Eyes_Ctrl/BTM_L_Notch", 16: "Eyes_Ctrl/BTM_L_Notch_Angle",
        17: "Eyes_Ctrl/BTM_R_Bow", 18: "Eyes_Ctrl/BTM_R_Bow_Angle",
        19: "Eyes_Ctrl/BTM_R_Bow_InOut", 20: "Eyes_Ctrl/BTM_R_Edge",
        21: "Eyes_Ctrl/BTM_R_Edge_Angle", 22: "Eyes_Ctrl/BTM_R_Notch",
        23: "Eyes_Ctrl/BTM_R_Notch_Angle", 24: "Eyes_Ctrl/EyeLeft",
        25: "Eyes_Ctrl/EyeLeftVertical", 26: "Eyes_Ctrl/EyeRight",
        27: "Eyes_Ctrl/Monocle", 28: "Eyes_Ctrl/MonocleInOut",
        29: "Eyes_Ctrl/TOP_CNTR_Notch", 30: "Eyes_Ctrl/TOP_L_Bow",
        31: "Eyes_Ctrl/TOP_L_Bow_Angle", 32: "Eyes_Ctrl/TOP_L_Bow_InOut",
        33: "Eyes_Ctrl/TOP_L_Edge", 34: "Eyes_Ctrl/TOP_L_Edge_Angle",
        35: "Eyes_Ctrl/TOP_L_Notch", 36: "Eyes_Ctrl/TOP_L_Notch_Angle",
        37: "Eyes_Ctrl/TOP_L_Notch_InOut", 38: "Eyes_Ctrl/TOP_R_Bow",
        39: "Eyes_Ctrl/TOP_R_Bow_Angle", 40: "Eyes_Ctrl/TOP_R_Bow_InOut",
        41: "Eyes_Ctrl/TOP_R_Edge", 42: "Eyes_Ctrl/TOP_R_Edge_Angle",
        43: "Eyes_Ctrl/TOP_R_Notch", 44: "Eyes_Ctrl/TOP_R_Notch_Angle",
        45: "Eyes_Ctrl/TOP_R_Notch_InOut", 46: "Eyes", 47: "Jaw",
        48: "LipLowerCenter", 49: "LipLowerCornerLeft",
        50: "LipLowerCornerRight", 51: "LipLowerMidLeft", 52: "LipLowerMidRight",
        53: "LipUpperCenter", 54: "LipUpperCornerLeft",
        55: "LipUpperCornerRight", 56: "LipUpperMidLeft", 57: "LipUpperMidRight",
        58: "Mouth_Ctrl/Mth_Corner_L", 59: "Mouth_Ctrl/Mth_Corner_R",
        60: "Mouth_Ctrl/Mth_Expand_L", 61: "Mouth_Ctrl/Mth_Expand_R",
        62: "Mouth_Ctrl/Mth_Offset", 63: "TeethLower", 64: "TeethUpper",
        65: "Tongue1", 66: "Tongue2", 67: "TongueRoot",
    },
    "car_eyes": {
        0: "Eyes_Ctrl/EyeLeft", 1: "Eyes_Ctrl/EyeLeftVertical",
        2: "Eyes_Ctrl/EyeRight", 3: "Eyes",
    },
    "car_facial": {
        0: "BrowCenter", 1: "BrowCornerLeft", 2: "BrowCornerRight",
        3: "BrowMidLeft", 4: "BrowMidRight", 5: "CheekLowerLeft",
        6: "CheekLowerRight", 7: "CheekUpperLeft", 8: "CheekUpperRight",
        9: "Eyes_Ctrl/BTM_CNTR_Notch", 10: "Eyes_Ctrl/BTM_L_Bow",
        11: "Eyes_Ctrl/BTM_L_Bow_Angle", 12: "Eyes_Ctrl/BTM_L_Bow_InOut",
        13: "Eyes_Ctrl/BTM_L_Edge", 14: "Eyes_Ctrl/BTM_L_Edge_Angle",
        15: "Eyes_Ctrl/BTM_L_Notch", 16: "Eyes_Ctrl/BTM_L_Notch_Angle",
        17: "Eyes_Ctrl/BTM_R_Bow", 18: "Eyes_Ctrl/BTM_R_Bow_Angle",
        19: "Eyes_Ctrl/BTM_R_Bow_InOut", 20: "Eyes_Ctrl/BTM_R_Edge",
        21: "Eyes_Ctrl/BTM_R_Edge_Angle", 22: "Eyes_Ctrl/BTM_R_Notch",
        23: "Eyes_Ctrl/BTM_R_Notch_Angle", 24: "Eyes_Ctrl/EyeLeft",
        25: "Eyes_Ctrl/EyeLeftVertical", 26: "Eyes_Ctrl/EyeRight",
        27: "Eyes_Ctrl/Monocle", 28: "Eyes_Ctrl/MonocleInOut",
        29: "Eyes_Ctrl/TOP_CNTR_Notch", 30: "Eyes_Ctrl/TOP_L_Bow",
        31: "Eyes_Ctrl/TOP_L_Bow_Angle", 32: "Eyes_Ctrl/TOP_L_Bow_InOut",
        33: "Eyes_Ctrl/TOP_L_Edge", 34: "Eyes_Ctrl/TOP_L_Edge_Angle",
        35: "Eyes_Ctrl/TOP_L_Notch", 36: "Eyes_Ctrl/TOP_L_Notch_Angle",
        37: "Eyes_Ctrl/TOP_L_Notch_InOut", 38: "Eyes_Ctrl/TOP_R_Bow",
        39: "Eyes_Ctrl/TOP_R_Bow_Angle", 40: "Eyes_Ctrl/TOP_R_Bow_InOut",
        41: "Eyes_Ctrl/TOP_R_Edge", 42: "Eyes_Ctrl/TOP_R_Edge_Angle",
        43: "Eyes_Ctrl/TOP_R_Notch", 44: "Eyes_Ctrl/TOP_R_Notch_Angle",
        45: "Eyes_Ctrl/TOP_R_Notch_InOut", 46: "Eyes", 47: "Jaw",
        48: "LipLowerCenter", 49: "LipLowerCornerLeft",
        50: "LipLowerCornerRight", 51: "LipLowerMidLeft", 52: "LipLowerMidRight",
        53: "LipUpperCenter", 54: "LipUpperCornerLeft",
        55: "LipUpperCornerRight", 56: "LipUpperMidLeft", 57: "LipUpperMidRight",
        58: "Mouth_Ctrl/Mth_Corner_L", 59: "Mouth_Ctrl/Mth_Corner_R",
        60: "Mouth_Ctrl/Mth_Expand_L", 61: "Mouth_Ctrl/Mth_Expand_R",
        62: "Mouth_Ctrl/Mth_Offset", 63: "TeethLower", 64: "TeethUpper",
        65: "Tongue1", 66: "Tongue2", 67: "TongueRoot",
    },
    "car_frontend_frontend": {
        0: "BackEnd", 1: "BackTires", 2: "BLeft_bttm", 3: "BLeft_Center",
        4: "Body2", 5: "Body3", 6: "Body_Baxle", 7: "Body", 8: "BRight_bttm",
        9: "BRight_Center", 10: "BrowCenter", 11: "BrowCornerLeft",
        12: "BrowCornerRight", 13: "BrowMidLeft", 14: "BrowMidRight",
        15: "CheekLowerLeft", 16: "CheekLowerRight", 17: "CheekUpperLeft",
        18: "CheekUpperRight", 19: "ControlArmLeft", 20: "ControlArmRight",
        21: "ControlBrowCenter", 22: "ControlBrowCornerLeft",
        23: "ControlBrowCornerRight", 24: "ControlBrowMidLeft",
        25: "ControlBrowMidRight", 26: "ControlHandLeft", 27: "ControlHandRight",
        28: "ControlStacheLeft", 29: "ControlStacheRight",
        30: "Eyes_Ctrl/BTM_CNTR_Notch", 31: "Eyes_Ctrl/BTM_L_Bow",
        32: "Eyes_Ctrl/BTM_L_Bow_Angle", 33: "Eyes_Ctrl/BTM_L_Bow_InOut",
        34: "Eyes_Ctrl/BTM_L_Edge", 35: "Eyes_Ctrl/BTM_L_Edge_Angle",
        36: "Eyes_Ctrl/BTM_L_Notch", 37: "Eyes_Ctrl/BTM_L_Notch_Angle",
        38: "Eyes_Ctrl/BTM_R_Bow", 39: "Eyes_Ctrl/BTM_R_Bow_Angle",
        40: "Eyes_Ctrl/BTM_R_Bow_InOut", 41: "Eyes_Ctrl/BTM_R_Edge",
        42: "Eyes_Ctrl/BTM_R_Edge_Angle", 43: "Eyes_Ctrl/BTM_R_Notch",
        44: "Eyes_Ctrl/BTM_R_Notch_Angle", 45: "Eyes_Ctrl/EyeLeft",
        46: "Eyes_Ctrl/EyeLeftVertical", 47: "Eyes_Ctrl/EyeRight",
        48: "Eyes_Ctrl/Monocle", 49: "Eyes_Ctrl/MonocleInOut",
        50: "Eyes_Ctrl/TOP_CNTR_Notch", 51: "Eyes_Ctrl/TOP_L_Bow",
        52: "Eyes_Ctrl/TOP_L_Bow_Angle", 53: "Eyes_Ctrl/TOP_L_Bow_InOut",
        54: "Eyes_Ctrl/TOP_L_Edge", 55: "Eyes_Ctrl/TOP_L_Edge_Angle",
        56: "Eyes_Ctrl/TOP_L_Notch", 57: "Eyes_Ctrl/TOP_L_Notch_Angle",
        58: "Eyes_Ctrl/TOP_L_Notch_InOut", 59: "Eyes_Ctrl/TOP_R_Bow",
        60: "Eyes_Ctrl/TOP_R_Bow_Angle", 61: "Eyes_Ctrl/TOP_R_Bow_InOut",
        62: "Eyes_Ctrl/TOP_R_Edge", 63: "Eyes_Ctrl/TOP_R_Edge_Angle",
        64: "Eyes_Ctrl/TOP_R_Notch", 65: "Eyes_Ctrl/TOP_R_Notch_Angle",
        66: "Eyes_Ctrl/TOP_R_Notch_InOut", 67: "Eyes", 68: "FLeft_bttm",
        69: "FLeft_Center", 70: "FRight_bttm", 71: "FRight_Center",
        72: "FrontEnd", 73: "FrontTires", 74: "Jaw", 75: "LipLowerCenter",
        76: "LipLowerCornerLeft", 77: "LipLowerCornerRight",
        78: "LipLowerMidLeft", 79: "LipLowerMidRight", 80: "LipUpperCenter",
        81: "LipUpperCornerLeft", 82: "LipUpperCornerRight",
        83: "LipUpperMidLeft", 84: "LipUpperMidRight", 85: "Main_Baxle",
        86: "Main_Ctrl/RotateFront", 87: "Main_Ctrl/RotateRear", 88: "Main",
        89: "Main_CtrlGrp", 90: "MainOrient", 91: "Mouth_Ctrl/Mth_Corner_L",
        92: "Mouth_Ctrl/Mth_Corner_R", 93: "Mouth_Ctrl/Mth_Expand_L",
        94: "Mouth_Ctrl/Mth_Expand_R", 95: "Mouth_Ctrl/Mth_Offest",
        96: "Mouth_Ctrl/Mth_Offset", 97: "Mouth_Ctrl/vsm_AA",
        98: "Mouth_Ctrl/vsm_IH", 99: "Mouth_Ctrl/vsm_M",
        100: "SideMirrorMountLeft", 101: "SideMirrorMountRight",
        102: "TCable_Pulley", 103: "TeethLower", 104: "TeethUpper",
        105: "Tongue1", 106: "Tongue2", 107: "TongueRoot", 108: "TowCrane",
        109: "TowCraneBase",
    },
    "car_frontend_mouth": {
        0: "BrowCenter", 1: "BrowCornerLeft", 2: "BrowCornerRight",
        3: "BrowMidLeft", 4: "BrowMidRight", 5: "CheekLowerLeft",
        6: "CheekLowerRight", 7: "CheekUpperLeft", 8: "CheekUpperRight",
        9: "Jaw", 10: "LipLowerCenter", 11: "LipLowerCornerLeft",
        12: "LipLowerCornerRight", 13: "LipLowerMidLeft", 14: "LipLowerMidRight",
        15: "LipUpperCenter", 16: "LipUpperCornerLeft",
        17: "LipUpperCornerRight", 18: "LipUpperMidLeft", 19: "LipUpperMidRight",
        20: "Mouth_Ctrl/Mth_Corner_L", 21: "Mouth_Ctrl/Mth_Corner_R",
        22: "Mouth_Ctrl/Mth_Expand_L", 23: "Mouth_Ctrl/Mth_Expand_R",
        24: "Mouth_Ctrl/Mth_Offest", 25: "Mouth_Ctrl/Mth_Offset",
        26: "Mouth_Ctrl/vsm_AA", 27: "Mouth_Ctrl/vsm_IH", 28: "Mouth_Ctrl/vsm_M",
        29: "TeethLower", 30: "TeethUpper", 31: "Tongue1", 32: "Tongue2",
        33: "TongueRoot",
    },
    "car_leanclipset": {
        0: "BackEnd", 1: "BackTires", 2: "BLeft_bttm", 3: "BLeft_Center",
        4: "Body2", 5: "Body3", 6: "Body", 7: "BRight_bttm", 8: "BRight_Center",
        9: "ControlLeftBarrels", 10: "ControlRightBarrels", 11: "FLeft_bttm",
        12: "FLeft_Center", 13: "FRight_bttm", 14: "FRight_Center",
        15: "FrontEnd", 16: "FrontTires", 17: "Main", 18: "MainOrient",
        19: "TowCraneBase",
    },
    "car_mouth": {
        0: "CheekLowerLeft", 1: "CheekLowerRight", 2: "CheekUpperLeft",
        3: "CheekUpperRight", 4: "Jaw", 5: "LipLowerCenter",
        6: "LipLowerCornerLeft", 7: "LipLowerCornerRight", 8: "LipLowerMidLeft",
        9: "LipLowerMidRight", 10: "LipUpperCenter", 11: "LipUpperCornerLeft",
        12: "LipUpperCornerRight", 13: "LipUpperMidLeft", 14: "LipUpperMidRight",
        15: "Mouth_Ctrl/Mth_Corner_L", 16: "Mouth_Ctrl/Mth_Corner_R",
        17: "Mouth_Ctrl/Mth_Expand_L", 18: "Mouth_Ctrl/Mth_Expand_R",
        19: "Mouth_Ctrl/Mth_Offset", 20: "TeethLower", 21: "TeethUpper",
        22: "Tongue1", 23: "Tongue2", 24: "TongueRoot",
    },
    "car_rattles": {
        0: "BackEnd", 1: "BackTires", 2: "BLeft_bttm", 3: "BLeft_Center",
        4: "Body2", 5: "Body3", 6: "Body", 7: "BRight_bttm", 8: "BRight_Center",
        9: "BrowCenter", 10: "BrowCornerLeft", 11: "BrowCornerRight",
        12: "BrowMidLeft", 13: "BrowMidRight", 14: "CheekLowerLeft",
        15: "CheekLowerRight", 16: "CheekUpperLeft", 17: "CheekUpperRight",
        18: "FLeft_bttm", 19: "FLeft_Center", 20: "FRight_bttm",
        21: "FRight_Center", 22: "FrontEnd", 23: "FrontTires", 24: "Jaw",
        25: "LipLowerCenter", 26: "LipLowerCornerLeft",
        27: "LipLowerCornerRight", 28: "LipLowerMidLeft", 29: "LipLowerMidRight",
        30: "LipUpperCenter", 31: "LipUpperCornerLeft",
        32: "LipUpperCornerRight", 33: "LipUpperMidLeft", 34: "LipUpperMidRight",
        35: "Main_Ctrl/RotateFront", 36: "Main_Ctrl/RotateRear", 37: "Main",
        38: "MainOrient", 39: "TeethLower", 40: "TeethUpper", 41: "Tongue1",
        42: "Tongue2", 43: "TongueRoot",
    },
}


def templates_in_file(path):
    """Connection templates a clip binds to, in LayerBind order.

    Bind block i uses template i. String-table order matches LayerBind
    order, and the counts agree on every shipped clip checked (147/147).
    """
    raw = open(path, "rb").read()
    if struct.unpack_from("<I", raw, 0)[0] != MAGIC:
        return []
    size = struct.unpack_from("<I", raw, 0x0C)[0]
    strings = raw[HEADER_SIZE:HEADER_SIZE + size].split(b"\0")
    found = []
    for chunk in strings:
        name = chunk.decode("latin1", "replace")
        if name in BUILTIN_CHANNEL_MAPS and name not in found:
            found.append(name)
    return found



# ===========================================================================
# Bindings
# ===========================================================================
#
# A command does not drive a bone directly. Commands write their channel
# values into a shared buffer, sequentially, in the order listed by the bind
# block's command-index list. Binding records then read that buffer by slot
# and scatter the values onto output nodes:
#
#   block:  u16 slotCount   (sum of channels over the listed commands)
#           u16 nCmd, u16 commandIndex[nCmd]
#           u16 staticOffset (from block start), u16 nAnimated
#           animated records, then at staticOffset: u16 nStatic, records
#   record: [u16 subCount | flags, u16 targetChannel] then subCount sub-ops.
#           flags 0x20 marks a scalar (slider) target; transforms carry 0.
#   sub-op: word 0 category in bits 12-15, operation in bits 0-3,
#           operand source in the bits above each operand
#
# Source selection per operand k: ((word >> (4 - 2*k)) & 3) == 1 reads the
# command output buffer, anything else reads the constant pool (an index
# into the pool's floats).
#
# Each bind block carries two record streams. The game's clip set-up
# walks the one at its recOffset (the static pose); playback walks the one
# immediately after the command-index list (the animated bindings). Reading
# only the first is why an earlier version scattered curves onto unrelated
# bones and fed rotation values into locations.

# Sub-op -> (property, component count). Component index is the operand's
# position for multi-slot ops, or fixed for the single-component forms.
FIELD_OPS = {
    0: ("location", (0, 1, 2)),
    1: ("location", (0,)),
    2: ("location", (1,)),
    3: ("location", (2,)),
    4: ("rotation_quaternion", (1, 2, 3, 0)),   # stored xyzw, Blender wxyz
    5: ("scale", (0, 1, 2)),
    6: ("scale", (0,)),
    7: ("scale", (1,)),
    8: ("scale", (2,)),
    9: ("custom", (0,)),
}


def parse_bind_block(blob):
    """(command_indices, static_records, animated_records).

    A record is (target, [(sub_word, [operand, ...]), ...]).
    """
    u16 = lambda o: struct.unpack_from("<H", blob, o)[0]
    n_cmd = u16(2)
    command_indices = [u16(4 + 2 * i) for i in range(n_cmd)]
    base = 4 + n_cmd * 2
    static_off = u16(base)
    animated_count = u16(base + 2)

    def walk(offset, count):
        records = []
        p = offset
        for _ in range(count):
            word, target = u16(p), u16(p + 2)
            p += 4
            subs = []
            for _ in range(word & 0xF):
                sub = u16(p)
                if (sub & 0xF000) in (0x0000, 0x3000):
                    subs.append((sub, [u16(p + 2)]))
                    p += 4
                else:
                    subs.append((sub, [u16(p + 2), u16(p + 4), u16(p + 6)]))
                    p += 8
            records.append((target, subs))
        return records

    static = walk(static_off + 2, u16(static_off)) if static_off else []
    animated = walk(base + 4, animated_count)
    return command_indices, static, animated


def buffer_slots(clip, command_indices):
    """Output buffer slot -> (command, channel), in evaluation order."""
    slots = []
    for index in command_indices:
        if index >= len(clip["commands"]):
            continue
        cmd = clip["commands"][index]
        for channel in range(cmd["n_channels"]):
            slots.append((cmd, channel))
    return slots


def buffer_slot_indices(clip, command_indices):
    """Output buffer slot -> (command index, channel), in evaluation order.

    Same walk as buffer_slots(), but yielding the command's index rather than
    the command itself, so the exporter can address commands by position.
    """
    slots = []
    for index in command_indices:
        if index >= len(clip["commands"]):
            continue
        for channel in range(clip["commands"][index]["n_channels"]):
            slots.append((index, channel))
    return slots


def operand_from_buffer(sub_word, k):
    return ((sub_word >> (4 - 2 * k)) & 3) == 1


def sub_op_targets(sub_word, operands):
    """[(property, component, slot, operand_index), ...] a sub-op writes.

    operand_index is which of the sub-op's operands the slot came from, so
    the caller can test that operand's source bit.
    """
    category = sub_word & 0xF000
    if category == 0x1000:
        # Three angles combined into a quaternion, rotation order in bits 6-8
        # (see euler_order_of).
        return [("rotation_euler", i, operands[i], i) for i in range(3)]
    if category == 0x2000:
        return [("rotation_quaternion", i + 1, operands[i], i) for i in range(3)]
    if category != 0x0000:
        return []

    entry = FIELD_OPS.get(sub_word & 0xF)
    if entry is None:
        return []
    prop, components = entry
    slot = operands[0]
    if len(components) == 1:
        return [(prop, components[0], slot, 0)]
    # Multi-component forms read consecutive slots from one operand.
    return [(prop, comp, slot + i, 0) for i, comp in enumerate(components)]


# ---------------------------------------------------------------------------
# Writing bind blocks
# ---------------------------------------------------------------------------
#
# Only sub-op forms that occur in shipped clips are emitted:
#   static   0x0000 location xyz (3 pool floats)   0x0001-3 one location axis
#            0x0004 quaternion xyzw (4 pool floats) 0x0005 scale xyz
#            0x0009 scalar
#   animated 0x0011-3 one location axis   0x0016-8 one scale axis
#            0x0019 scalar
#            0x1000 | order << 6 | sources  Euler (order per bone, default 2 = zxy),
#                              each axis from the buffer or the pool

RECORD_SCALAR = 0x20
OP_EULER = 0x1080


def _record_bytes(target, subs, scalar=False):
    out = bytearray(struct.pack("<HH", len(subs) | (RECORD_SCALAR if scalar else 0),
                                target))
    for word, operands in subs:
        if len(operands) == 1:
            out += struct.pack("<HH", word, operands[0])
        else:
            out += struct.pack("<4H", word, *operands)
    return bytes(out)


def build_bind_block(slot_count, command_indices, animated, static):
    """animated / static: [(target, [(sub_word, [operands])], is_scalar)]."""
    head = struct.pack("<HH", slot_count, len(command_indices))
    head += struct.pack("<%dH" % len(command_indices), *command_indices)
    anim = b"".join(_record_bytes(t, subs, sc) for t, subs, sc in animated)
    stat = b"".join(_record_bytes(t, subs, sc) for t, subs, sc in static)
    static_off = len(head) + 4 + len(anim)
    blob = head + struct.pack("<HH", static_off, len(animated)) + anim
    blob += struct.pack("<H", len(static)) + stat
    return blob + b"\xee" * (-len(blob) % 4)


# ===========================================================================
# Action API compatibility
# ===========================================================================
#
# Blender 4.4 introduced slotted actions and 5.x removed Action.fcurves.
# Curves now live in a channelbag reached through layers -> strips -> slot.
# These helpers work on both layouts so the add-on runs on 3.x through 5.x.

def action_curves(action):
    """Every F-curve on an action, whichever API version is in use."""
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                out.extend(bag.fcurves)
    return out


def action_slot(action, obj):
    """The action's slot, created if absent. None on pre-4.4 Blender."""
    if hasattr(action, "fcurves"):
        return None
    if action.slots:
        return action.slots[0]
    name = obj.name if obj is not None else "Object"
    try:
        return action.slots.new(id_type="OBJECT", name=name)
    except TypeError:
        return action.slots.new("OBJECT", name)


def action_container(action, obj, create=True):
    """Object exposing .fcurves for reading or creating curves."""
    if hasattr(action, "fcurves"):
        return action

    layer = action.layers[0] if action.layers else (
        action.layers.new("Layer") if create else None)
    if layer is None:
        return None
    strip = layer.strips[0] if layer.strips else (
        layer.strips.new(type="KEYFRAME") if create else None)
    if strip is None:
        return None

    slot = action_slot(action, obj)
    if slot is None:
        return None
    try:
        return strip.channelbag(slot, ensure=create)
    except TypeError:
        return strip.channelbag(slot)


def assign_action(obj, action):
    """Attach an action, binding the slot on 4.4+ so it actually evaluates."""
    if obj.animation_data is None:
        obj.animation_data_create()
    obj.animation_data.action = action
    slot = action_slot(action, obj)
    if slot is not None and hasattr(obj.animation_data, "action_slot"):
        obj.animation_data.action_slot = slot


def find_target_armature(context):
    """The armature a freshly imported model left in the scene.

    Run the model importer, then import a clip: this picks up the rig without
    needing anything selected. Preference order is active object, the rig
    driving the active mesh, the only armature present, then the most recently
    added one.
    """
    obj = context.object
    if obj is not None and obj.type == "ARMATURE":
        return obj

    if obj is not None and obj.type == "MESH":
        for mod in obj.modifiers:
            if mod.type == "ARMATURE" and mod.object is not None:
                return mod.object
        if obj.parent is not None and obj.parent.type == "ARMATURE":
            return obj.parent

    armatures = [o for o in context.scene.objects if o.type == "ARMATURE"]
    if len(armatures) == 1:
        return armatures[0]
    if armatures:
        selected = [o for o in armatures if o.select_get()]
        if len(selected) == 1:
            return selected[0]
        return armatures[-1]
    return None


def bone_candidates(channel_name):
    """Blender bone names to try for a channel, most specific first.

    Node names come through as scene paths for the sub-controls, e.g.
    "Eyes_Ctrl/BTM_L_Bow". Rigs may expose the leaf, the flattened path, or
    the authoring name with its _Ctrl / _CtrlGrp suffix, so try each.
    """
    leaf = channel_name.rsplit("/", 1)[-1]
    flat = channel_name.replace("/", "_")
    out = []
    # "_Ctrl" first: on a rig that carries both the game's control bones and
    # its skin bones (Main_Ctrl and Main, Jaw_Ctrl and Jaw, ...), the clip
    # drives the control and the skin bone follows it. Trying the bare name
    # first put control values straight onto the skin bones instead. Rigs
    # without control bones fall through to the bare name as before.
    for base in (channel_name, flat, leaf):
        for form in (base + "_Ctrl", base, base + "_CtrlGrp"):
            if form not in out:
                out.append(form)
    return out


def load_channel_maps(path, filepath=None, n_binds=1):
    """One channel map per bind block, in bind order.

    Each LayerBind names its own ConnectionTemplate and each template is a
    separate index space. Merging them into a single dict let the later
    template overwrite the earlier one's indices; since car_body is always
    bind 0, the body and wheel channels were the ones that got clobbered
    (FLeft_Center read as BrowCornerRight, Main as LipUpperCornerRight, and
    so on) on every clip with more than one bind block.

    A user-supplied channels.json still overrides. Tables keyed by a known
    template name apply to that template only; any other table applies to
    every block, since it carries no template context.
    """
    templates = templates_in_file(filepath) if filepath else []

    override = {}
    if path and os.path.exists(path):
        with open(path) as handle:
            data = json.load(handle)
        for name, table in data.items():
            if isinstance(table, dict):
                override[name] = {int(k): v for k, v in table.items()}

    maps = []
    for i in range(max(n_binds, 1)):
        template = templates[i] if i < len(templates) else None
        table = dict(BUILTIN_CHANNEL_MAPS.get(template, {}))
        for name, tbl in override.items():
            if name not in BUILTIN_CHANNEL_MAPS:
                table.update(tbl)
        if template and template in override:
            table.update(override[template])
        maps.append(table)
    return maps


def read_block(path, tmp_dir):
    exe = find_offsetting()
    if exe:
        try:
            return read_block_offsetting(exe, path, tmp_dir)
        except Exception:
            pass
    return read_block_native(path)


def write_block(path, block, out_path):
    write_block_native(path, block, out_path)



def _segment(keys, inside):
    """Index of the segment containing `inside` - a point known to lie within
    the interval being evaluated. Frame times land fractionally off key times
    (u16 quantisation), so picking by the sample time alone can straddle a
    boundary and, with the slope discontinuity there, produce nonsense."""
    lo, hi = 0, len(keys) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if keys[mid][0] <= inside:
            lo = mid
        else:
            hi = mid
    return lo


def sample_at(keys, time, inside):
    """Value of the stored curve, with the same segment disambiguation."""
    if not keys:
        return 0.0
    if time <= keys[0][0]:
        return keys[0][1][0]
    if time >= keys[-1][0]:
        return keys[-1][1][0]
    lo = _segment(keys, inside)
    t0, pts = keys[lo]
    t1 = keys[lo + 1][0]
    # Key times are u16 fractions of t_end, so a frame can sit a few
    # microseconds outside the segment `inside` picked. Extending that
    # segment's cubic across the sliver keeps one-sided slopes one-sided and
    # still returns the true value; clamping u instead returned the key's
    # value from the wrong time, about slope * 25us off on fast curves.
    u = 0.0 if t1 == t0 else min(max((time - t0) / (t1 - t0), -0.05), 1.05)
    p0, p1, p2, p3 = pts
    v = 1.0 - u
    return (v * v * v * p0 + 3 * v * v * u * p1
            + 3 * v * u * u * p2 + u * u * u * p3)



# ===========================================================================
# Mouth relationship: the Mth_* and vsm_* sliders
# ===========================================================================
#
# In-game, each mouth bone's world transform is copied (UberConstraint) from
# its X_Ctrl node, which sits under an X_Proxy node. The clip animates the
# X_Ctrl nodes; the X_Proxy nodes are driven by a RelationshipProcessor from
# 21 slider values - Mth_Corner_L/R, Mth_Expand_L/R, Mth_Offset and the 16
# vsm_* visemes. Without this the jaw and lip keys apply but the expression
# the sliders shape never forms (on select_idle the corners sit still while
# the middle of the mouth drops, and the mesh creases between them).
#
# The RelationshipDataBlock, decoded from McQueen and checked end to end:
#   section A (21 curves, 1 channel each): slider value -> time, a linear
#       remap of [0,10] (visemes) or [-100,100] (Mth_*) onto [0, 1/3]
#   section B (21 curves): each slider's channels over that time; every one
#       of the 663 channels is zero (to 1e-5) at the slider's neutral value
#   section B header record: a bind program over the 663 channels (every
#       channel used exactly once) targeting the processor's 22 outputs, in
#       begin/accumulate/finish ops that sum contributions per component
# A curve's stored index is the slider it belongs to; its position in the
# record table is only packing order.

REL_SECTION_A = 0


def _rel_curve(buf, a, length):
    idx, w, t0, t1 = struct.unpack_from("<IIff", buf, a)
    n_keys, n_ch = w >> 20, (w >> 9) & 0x7FF
    p = a + 16
    params = [struct.unpack_from("<4f", buf, p + 16 * c) for c in range(n_ch)]
    p += 16 * n_ch
    times = list(struct.unpack_from("<%dH" % n_keys, buf, p))
    p += 2 * n_keys                      # key data follows directly; padding is at the end
    raw = [[struct.unpack_from("<4h", buf, p + (k * n_ch + c) * 8)
            for c in range(n_ch)] for k in range(n_keys)]
    span = t1 - t0
    starts = [t0 + span * times[k] / 65535.0 for k in range(n_keys)]
    return {"index": idx, "t0": t0, "t1": t1, "n_keys": n_keys, "n_ch": n_ch,
            "params": params, "times": times, "raw": raw, "starts": starts}


def _rel_records(buf, base):
    offs = list(struct.unpack_from("<22I", buf, base + 20))
    size = struct.unpack_from("<I", buf, base + 16)[0]
    ends = offs[1:] + [size]
    return offs, [(base + offs[k], ends[k] - offs[k]) for k in range(1, len(offs))]


def _rel_eval(c, ch, t):
    _pre, _post, bias, scale = c["params"][ch]
    t = min(max(t, c["t0"]), c["t1"])
    starts = c["starts"]
    k = 0
    while k + 1 < c["n_keys"] and starts[k + 1] <= t + 1e-12:
        k += 1
    end = starts[k + 1] if k + 1 < c["n_keys"] else c["t1"]
    u = 0.0 if end <= starts[k] else (t - starts[k]) / (end - starts[k])
    d = c["raw"][k][ch]
    P = [d[0], d[0] + d[1], d[0] + d[1] + d[2], d[0] + d[1] + d[2] + d[3]]
    v = 1.0 - u
    return bias + scale * (v * v * v * P[0] + 3 * v * v * u * P[1]
                           + 3 * v * u * u * P[2] + u * u * u * P[3])


def _rel_program(rec):
    n_cmd = struct.unpack_from("<H", rec, 2)[0]
    base = 4 + 2 * n_cmd
    _static_off, n_anim = struct.unpack_from("<HH", rec, base)
    p = base + 4
    out = []
    for _ in range(n_anim):
        n_sub, target = struct.unpack_from("<HH", rec, p)
        p += 4
        subs = []
        for _ in range(n_sub):
            code = struct.unpack_from("<H", rec, p)[0]
            p += 2
            cat = code & 0xF000
            if cat in (0x1000, 0x2000, 0x3000):        # translation component
                slot = struct.unpack_from("<H", rec, p)[0]
                p += 6                                 # slot + inline float (always 0)
                subs.append(("loc", (code & 0xF) - 1, slot))
            elif cat in (0x4000, 0x5000, 0x6000):      # euler, + inline identity quat
                ops = struct.unpack_from("<3H", rec, p)
                p += 22
                for i, o in enumerate(ops):
                    if ((code >> (4 - 2 * i)) & 3) == 1:
                        subs.append(("rot", i, o))
            else:
                raise ValueError("unknown relationship sub-op 0x%04x" % code)
        out.append((target, subs))
    return out


def load_relationship(obj):
    """Relationship model stored on the armature by the model importer, or None."""
    if obj is None:
        return None
    try:
        buf = base64.b64decode(obj["cars2_relationship"])
        inputs = json.loads(obj["cars2_relationship_inputs"])
        outputs = json.loads(obj["cars2_relationship_outputs"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        a_offs, a_recs = _rel_records(buf, REL_SECTION_A)
        b_base = struct.unpack_from("<I", buf, 8)[0]    # section A's "next" = section B
        b_offs, b_recs = _rel_records(buf, b_base)
        remap = [_rel_curve(buf, a, L) for a, L in a_recs]
        curves = [_rel_curve(buf, a, L) for a, L in b_recs]
        program = _rel_program(buf[b_base + b_offs[0]:b_base + b_offs[1]])
    except (struct.error, ValueError, IndexError) as exc:
        print("relationship: could not decode (%s); mouth sliders ignored" % exc)
        return None
    owner = [(pos, ch) for pos, c in enumerate(curves) for ch in range(c["n_ch"])]
    # Section A is packed in its own order too; look remaps up by the slider
    # index each curve stores, never by list position.
    remap = {c["index"]: c for c in remap}
    return {"inputs": inputs, "outputs": outputs, "remap": remap,
            "curves": curves, "program": program, "owner": owner}


def relationship_pose(model, sliders):
    """{slider: value} -> {bone: ([loc x,y,z], [euler x,y,z])}, parent axes."""
    times = {}
    for pos, c in enumerate(model["curves"]):
        name = model["inputs"][c["index"]] if c["index"] < len(model["inputs"]) else None
        value = sliders.get(name, None)
        if value is None:                       # neutral: 0 for every slider
            value = 0.0
        remap = model["remap"].get(c["index"])
        times[pos] = _rel_eval(remap, 0, value) if remap else 0.0
    pose = {}
    for target, subs in model["program"]:
        bone = model["outputs"][target] if target < len(model["outputs"]) else ""
        if not bone:
            continue
        loc, rot = pose.setdefault(bone, ([0.0] * 3, [0.0] * 3))
        for kind, comp, slot in subs:
            if slot >= len(model["owner"]):
                continue
            pos, ch = model["owner"][slot]
            value = _rel_eval(model["curves"][pos], ch, times[pos])
            (loc if kind == "loc" else rot)[comp] += value
    return pose


# ===========================================================================
# Rig conventions
# ===========================================================================
#
# Channels whose labels are scene paths ("Eyes_Ctrl/TOP_L_Bow",
# "Mouth_Ctrl/Mth_Corner_L", "Main_Ctrl/RotateFront", ...) are scalar
# sliders: in every shipped clip they are written with the scalar sub-op and
# never as transforms. They live on the armature as custom properties of the
# same name, so they can be keyed like anything else. Everything else is a
# bone, found through bone_candidates().

def is_scalar_label(label):
    return "/" in label


# Eyelids with no clip keys. Every eyelid slider rests at 0, which puts both
# lids on the strip's middle line: eyes shut. In the game an eyelid clip
# always runs on top, so that never shows. In Blender a clip without eyelid
# keys (a body-only clip such as shared_quickstart_go2) left the eyes shut.
# So when no clip keys the lids they show a game pose instead: select_idle's
# first frame, made symmetric (left and right averaged, angles 0) - open eyes
# with the lids meeting at the centre notch and sloping at the outer corners.
# (v1.66/v1.67 used flat lids at the eyeballs' edges: square corners.)
# BTM_CNTR_Notch is the bottom lid's centre point on single-strip eyes
# (car_eyes: Finn, McQueen, Flo...); double-eye cars never key it. At 0 it
# sat on the middle line and drew a triangle up between the eyes. -84.5 is
# the median of the first frames of the single-eye select_idle clips that ship
# with the game (71 clips, -100..-36; Finn -79.8). (v1.86)
# A clip that keys any lid keeps the game's 0 for the lids it leaves unkeyed
# (settle_eyelids). Only the shown pose changes - Export Clip writes keyed
# sliders only.
# v1.89: the top lid is FLAT at 45. The select_idle-derived top lid (edges 83,
# bows 67.6, notches 19.4, centre 0) dipped towards the middle - an angry V on
# every car whose file rests are all 0 (McQueen and variants, Cruz, Sally,
# Holly, King, Bobby, Brick, Rich Mixon, Zundapp...). The cars whose files do
# carry a rest pose almost all use one flat value for the whole top lid
# (35..52: Mater 45.5, Finn 36.2, Luigi 52.6), so this follows them. Checked
# on renders of 76 imported characters, single and double eyes.
EYELID_DEFAULT = {
    "TOP_L_Edge": 45.0, "TOP_R_Edge": 45.0, "TOP_L_Bow": 45.0, "TOP_R_Bow": 45.0,
    "TOP_L_Notch": 45.0, "TOP_R_Notch": 45.0, "TOP_CNTR_Notch": 45.0,
    # v1.91: the bottom lid is flat too. With bows at -105.9 and the centre
    # notch at -84.5 the centre stood up as a hump between the eyes (Holly);
    # -95 everywhere keeps the lid just below the strip on every car checked.
    "BTM_L_Edge": -95.0, "BTM_R_Edge": -95.0, "BTM_L_Bow": -95.0, "BTM_R_Bow": -95.0,
    "BTM_L_Notch": -95.0, "BTM_R_Notch": -95.0, "BTM_CNTR_Notch": -95.0,
}


SLIDER_REST_PROP = "cars2_slider_rest"     # same key as c2_model's


def scalar_rest(label, obj=None):
    """The value a slider shows when no clip keys it: the value the model got
    when it was imported (stored on the armature since v1.88 - the model's
    own rest where its file has one), else the generic open-eyes pose."""
    if obj is not None:
        try:
            stored = json.loads(obj.get(SLIDER_REST_PROP, "{}"))
        except (TypeError, ValueError, AttributeError):
            stored = {}
        if label in stored:
            return float(stored[label])
    if not label.startswith("Eyes_Ctrl/"):
        return 0.0
    return EYELID_DEFAULT.get(label.rsplit("/", 1)[-1], 0.0)


def _is_lid_pos(label):
    """An eyelid slider (TOP_* / BTM_*, angles included)."""
    return label.startswith("Eyes_Ctrl/") and label.rsplit("/", 1)[-1].startswith(("TOP_", "BTM_"))


def settle_eyelids(target, action):
    """A clip that keys any eyelid position shows its lids exactly as the game
    does: the lid sliders it leaves unkeyed sit at their rest 0 (the open
    pose is only for clips that key no lid at all)."""
    if target is None or action is None:
        return
    keyed = set()
    for fc in action_curves(action):
        m = _SCALAR_PATH.match(fc.data_path)
        if m:
            keyed.add(m.group(1))
    if not any(_is_lid_pos(k) for k in keyed):
        return
    for key in list(target.keys()):
        if _is_lid_pos(key) and key not in keyed:
            try:
                target[key] = 0.0
            except TypeError:
                pass


def scalar_soft_range(label):
    leaf = label.rsplit("/", 1)[-1]
    if leaf.startswith("vsm_"):
        return 0.0, 10.0                 # visemes
    if leaf.endswith("_Angle"):
        return -1.0, 1.0
    return -100.0, 100.0                 # Mth_*, eyelid shapes, RotateFront/Rear


def scalar_path(label):
    return '["%s"]' % label.replace('"', "")


def ensure_scalar(obj, name, value=None):
    """Create (or set) a slider property on the armature, with a UI range."""
    if obj is None:
        return
    if name not in obj.keys():
        obj[name] = scalar_rest(name, obj)
    if value is not None:
        obj[name] = float(value)
    try:
        lo, hi = scalar_soft_range(name)
        obj.id_properties_ui(name).update(
            soft_min=lo, soft_max=hi,
            description="Cars 2 animation slider (keyframe it like a bone)")
    except (AttributeError, TypeError):
        pass


def bone_for_label(obj, label):
    """(pose_bone, rank) for a channel label, or (None, None)."""
    if obj is None or obj.type != "ARMATURE":
        return None, None
    for rank, name in enumerate(bone_candidates(label)):
        pb = obj.pose.bones.get(name)
        if pb is not None:
            return pb, rank
    return None, None


def rest_local_matrix(pose_bone):
    """The bone's full rest transform relative to its parent, as a 4x4."""
    local = pose_bone.bone.matrix_local
    if pose_bone.parent is not None:
        return pose_bone.parent.bone.matrix_local.inverted() @ local
    return local.copy()


# Stored values are each control node's offset from rest, in its parent's
# axes: location added to the rest position, rotation applied before the rest
# rotation. Blender pose channels are in bone space, so with R the bone's
# rest rotation relative to its parent:
#
#     basis location = R^-1 @ stored location
#     basis rotation = R^-1 @ stored rotation @ R
#
# (Checked over every channel of every shipped clip that resolves to a bone:
# components that sit still at the rig's rest pose read zero, never the
# bone's rest offset.) Mouth bones additionally sit under a slider-driven
# _Proxy offset p, q: the game chain is Proxy (sliders) -> Ctrl (clip), so
# the combined offset is T(p + q c) R(q r). Import composes it in; export
# takes it back out. These two functions are exact inverses.

# Clips store rotations two ways, with two conventions:
#  - Euler records (animated rotations, e.g. Body / Main in a two-wheel
#    stunt) are plain angles, as Maya wrote them - read as-is
#    (shared_2wheelleftstart: inverted, McQueen balanced on the wrong wheels).
#  - Quaternion records (constant rotations, e.g. a face pose) are stored
#    conjugated, like the rig's own rotors - their inverse is the rotation
#    (shrd_face_emo_air_trick_*: read as-is, the jaw swung up into the head
#    and the tongue went through the lips; conjugated, the mouth opens).
# Import conjugates quaternion records; Export Clip writes constant
# rotations conjugated - clips round-trip exactly.
CLIP_QUATERNION_CONJUGATED = True


def stored_to_basis(rest, rest_inv, loc, rot, proxy=None):
    if proxy is not None:
        p_loc, p_rot = proxy
        loc = p_loc + p_rot @ loc
        rot = p_rot @ rot
    return rest_inv @ loc, rest_inv @ rot @ rest


def basis_to_stored(rest, rest_inv, loc, rot, proxy=None):
    loc = rest @ loc
    rot = rest @ rot @ rest_inv
    if proxy is not None:
        p_loc, p_rot = proxy
        p_inv = p_rot.transposed()
        loc = p_inv @ (loc - p_loc)
        rot = p_inv @ rot
    return loc, rot


# ===========================================================================
# Import
# ===========================================================================

def reset_pose(target):
    """Return every pose bone and slider to rest before a new action is applied.

    Blender does not reset a bone (or a custom property) that the incoming
    action has nothing to say about, so whatever the last clip left behind
    persists - import a spinout once and the lips stay displaced under every
    clip loaded afterwards.
    """
    if target is None or target.type != "ARMATURE":
        return
    # Cars 2 Toolkit: bones the user added (the importer marks every bone it
    # made with "oct_rest") are never animated by game clips, so a clip
    # import must not wipe how they were posed - e.g. a raised spoiler.
    has_notes = any("oct_rest" in b for b in target.data.bones)
    for bone in target.pose.bones:
        if has_notes and "oct_rest" not in bone.bone:
            continue
        bone.location = (0.0, 0.0, 0.0)
        bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        bone.rotation_euler = (0.0, 0.0, 0.0)
        bone.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
        bone.scale = (1.0, 1.0, 1.0)
    for key in list(target.keys()):
        if is_scalar_label(key):
            try:
                target[key] = scalar_rest(key, target)
            except TypeError:
                pass


class _SliderProxy:
    """Per-frame relationship offsets for the mouth bones, from slider values.

    `source(time, inside)` returns {slider name: value} - sampled clip curves
    on import, the action's slider properties on export.
    """

    def __init__(self, model, target, source):
        self.model, self.source = model, source
        self.bones = {b for b in model["outputs"]
                      if b and target.pose.bones.get(b) is not None}
        self._cache = {}

    def __call__(self, time, inside=None):
        key = (round(time, 7), None if inside is None else round(inside, 7))
        pose = self._cache.get(key)
        if pose is None:
            pose = relationship_pose(self.model, self.source(time, inside))
            self._cache[key] = pose
        return pose

    def offset(self, bone, time, inside=None):
        from mathutils import Euler, Vector
        loc, rot = self(time, inside).get(bone, ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)))
        return Vector(loc), Euler(rot, EULER_ORDER).to_matrix()


def _slider_name(label):
    """'Mouth_Ctrl/Mth_Corner_R' -> 'Mth_Corner_R'; None for anything else."""
    head, _, tail = label.partition("/")
    return tail if head == "Mouth_Ctrl" and tail else None


def new_curve(container, path, index, group=None):
    try:
        curve = container.fcurves.new(data_path=path, index=index)
    except RuntimeError:
        return container.fcurves.find(path, index=index)
    if group:
        try:
            grp = container.groups.get(group) or container.groups.new(group)
            curve.group = grp
        except (AttributeError, RuntimeError, TypeError):
            pass
    return curve


# How far before a step its extra "left value" key sits, in frames. Blender
# merges keys closer than 0.01 frames.
STEP_LEAD = 0.02


def _steps_at(prev_value, value, spread):
    """A real cut, as opposed to the hairline breaks that rounding leaves
    between neighbouring segments (up to ~0.01% of a channel's range)."""
    return abs(value - prev_value) > max(1e-7, 1e-3 * spread)


def fill_curve(curve, keys, fps):
    """Write Bezier keys, mapping stored control points onto handles.

    The format fixes handles at 1/3 and 2/3 across each segment in time and
    stores only their value, so the X positions are reconstructed here.
    Where a segment ends somewhere other than where the next one starts (a
    step - the format keeps P3 and the next P0 separately), a key just
    before the step carries the left-hand value with constant interpolation,
    so the cut stays a cut.
    """
    spread = (max(p for _t, pts in keys for p in pts)
              - min(p for _t, pts in keys for p in pts)) if keys else 0.0
    points = []      # (frame, value, left handle, right handle, interpolation)
    for k, (time, pts) in enumerate(keys):
        frame = time * fps
        if k:
            prev_time, prev_pts = keys[k - 1]
            span = (time - prev_time) * fps
            left = (frame - span / 3.0, prev_pts[2])
            if _steps_at(prev_pts[3], pts[0], spread):
                lead = frame - min(STEP_LEAD, span / 4.0)
                points.append((lead, prev_pts[3], left, (lead, prev_pts[3]), "CONSTANT"))
                left = (frame - 1.0, pts[0])
        else:
            left = (frame - 1.0, pts[0])
        if k + 1 < len(keys):
            span = (keys[k + 1][0] - time) * fps
            right = (frame + span / 3.0, pts[1])
        else:
            right = (frame + 1.0, pts[0])
        points.append((frame, pts[0], left, right, "BEZIER"))
    curve.keyframe_points.add(len(points))
    for point, (frame, value, left, right, interp) in zip(curve.keyframe_points, points):
        point.co = (frame, value)
        point.interpolation = interp
        point.handle_left_type = "FREE"
        point.handle_right_type = "FREE"
        point.handle_left = left
        point.handle_right = right
    curve.update()


def constant_curve(curve, value, frame=0.0):
    curve.keyframe_points.add(1)
    point = curve.keyframe_points[0]
    point.co = (frame, value)
    point.interpolation = "LINEAR"
    curve.update()


def bake_bone_transforms(container, target, groups, constants, fps, frames,
                         proxy=None, euler_orders=None):
    """Sample stored transforms and write them as bone-space pose keys.

    A bind block's static records hold whatever this clip keeps still, its
    animated records whatever moves; between them they describe each bound
    component. See stored_to_basis() for the space conversion.

    Sampling is on the frame grid. Each baked key gets explicit handles from
    the slope of the real curve, which reproduces the stored cubic far
    better than Blender's automatic handles (about 6% overshoot) or linear
    keys (about 10% undershoot between samples). A curve that never moves is
    written as a single key, so held bones don't fill the graph editor.
    """
    from mathutils import Euler, Matrix, Quaternion, Vector

    written = 0
    proxy_bones = proxy.bones if proxy is not None else set()

    for bone_name in sorted(set(groups) | set(constants) | proxy_bones):
        pb = target.pose.bones.get(bone_name)
        if pb is None:
            continue
        channels = groups.get(bone_name, {})
        fixed = constants.get(bone_name, {})
        present = set(channels) | set(fixed)
        has_proxy = bone_name in proxy_bones
        has_euler = any(p == "rotation_euler" for p, _ in present)
        has_quat = any(p == "rotation_quaternion" for p, _ in present)
        has_loc = has_proxy or any(p == "location" for p, _ in present)
        has_rot = has_proxy or has_euler or has_quat
        has_scale = any(p == "scale" for p, _ in present)

        rest = rest_local_matrix(pb).to_3x3()
        rest_inv = rest.inverted()

        def comp(prop, i, time, inside, default=0.0):
            keys = channels.get((prop, i))
            if keys:
                return sample_at(keys, time, inside)
            return fixed.get((prop, i), default)

        def pose_at(time, inside, ref):
            loc = Vector([comp("location", i, time, inside) for i in range(3)])
            if has_euler:
                rot = Euler([comp("rotation_euler", i, time, inside)
                             for i in range(3)],
                            (euler_orders or {}).get(bone_name, EULER_ORDER)).to_matrix()
            elif has_quat:
                q = [comp("rotation_quaternion", i, time, inside, None)
                     for i in range(4)]
                if q[0] is None:      # xyz-only form: w from unit length
                    x, y, z = (v or 0.0 for v in q[1:])
                    q[0] = math.sqrt(max(0.0, 1.0 - x * x - y * y - z * z))
                rot = Quaternion([v or 0.0 for v in q]).normalized().to_matrix()
                if CLIP_QUATERNION_CONJUGATED:          # stored conjugated
                    rot = rot.transposed()
            else:
                rot = Matrix.Identity(3)
            px = proxy.offset(bone_name, time, inside) if has_proxy else None
            out_loc, out_rot = stored_to_basis(rest, rest_inv, loc, rot, px)
            quat = out_rot.to_quaternion()
            if ref is not None and quat.dot(ref) < 0.0:
                quat.negate()
            vals = []
            if has_loc:
                vals += list(out_loc)
            if has_rot:
                vals += [quat.w, quat.x, quat.y, quat.z]
            if has_scale:
                vals += [comp("scale", i, time, inside, 1.0) for i in range(3)]
            return vals, quat

        paths = []
        if has_loc:
            paths += [("location", i) for i in range(3)]
        if has_rot:
            paths += [("rotation_quaternion", i) for i in range(4)]
            pb.rotation_mode = "QUATERNION"
        if has_scale:
            paths += [("scale", i) for i in range(3)]
        if not paths:
            continue

        # Keys per frame. A quaternion key per frame cannot carry more than
        # half a turn between keys - the wheels in frontend_exit turn about
        # 300 degrees a frame - so bones that rotate fast get sub-frame keys,
        # enough to keep each step under 45 degrees.
        # Segment choice is biased a fiftieth of a frame into the interval
        # on each side: far past the u16 key-time slop (~0.002 frames at the
        # longest clip lengths), far short of the closest keys an export can
        # write (1/8 frame), so a sample never straddles a key.
        bias = 1.0 / (50.0 * fps)
        steps = 1
        if has_rot:
            probe = 8
            ref = None
            fastest = 0.0
            for i in range(frames * probe + 1):
                t = i / (fps * probe)
                _v, quat = pose_at(t, t + bias, ref)
                if ref is not None:
                    fastest = max(fastest, 2.0 * math.acos(min(1.0, abs(quat.dot(ref)))))
                ref = quat
            steps = max(1, min(probe, int(math.ceil(fastest * probe / (math.pi / 4)))))

        span = 1.0 / (fps * steps)
        delta = bias / 4.0
        ref = None
        values, rises, falls, lefts = [], [], [], []
        for frame in range(frames * steps + 1):
            time = frame * span
            right_ref = time + bias
            left_ref = max(time - bias, 0.0)
            value, ref = pose_at(time, right_ref, ref)
            r1, _ = pose_at(time + delta, right_ref, ref)
            r2, _ = pose_at(time + 2.0 * delta, right_ref, ref)
            back, _ = pose_at(time, left_ref, ref)
            l1, _ = pose_at(max(time - delta, 0.0), left_ref, ref)
            l2, _ = pose_at(max(time - 2.0 * delta, 0.0), left_ref, ref)
            values.append(value)
            lefts.append(back)
            # Slopes in value per key step.
            rises.append([(-3.0 * value[i] + 4.0 * r1[i] - r2[i]) * span / (2.0 * delta)
                          for i in range(len(value))])
            falls.append([(3.0 * back[i] - 4.0 * l1[i] + l2[i]) * span / (2.0 * delta)
                          for i in range(len(value))])

        group = bone_name
        for c, (prop, index) in enumerate(paths):
            curve = new_curve(container, 'pose.bones["%s"].%s' % (bone_name, prop),
                              index, group)
            if curve is None:
                continue
            column = [v[c] for v in values]
            spread = max(column) - min(column)
            if spread <= 1e-9 * (1.0 + abs(column[0])):
                constant_curve(curve, column[0])
            else:
                third = 1.0 / (3.0 * steps)
                points = []
                for k, value in enumerate(column):
                    frame = k / float(steps)
                    left = (frame - third, value - falls[k][c] / 3.0)
                    arrive = lefts[k][c]
                    if k and _steps_at(arrive, value, spread):
                        # A cut. Held pose before it: make the previous key
                        # constant. Moving into it: add a key just before.
                        prev = points[-1]
                        if (abs(prev[1] - arrive) <= 1e-4 * spread
                                and abs(rises[k - 1][c]) <= 1e-4 * spread):
                            points[-1] = prev[:3] + ((prev[0] + third, prev[1]), "CONSTANT")
                        else:
                            lead = frame - STEP_LEAD / steps
                            points.append((lead, arrive,
                                           (lead - third, arrive - falls[k][c] / 3.0),
                                           (lead, arrive), "CONSTANT"))
                        left = (frame - third, value)
                    points.append((frame, value, left,
                                   (frame + third, value + rises[k][c] / 3.0), "BEZIER"))
                curve.keyframe_points.add(len(points))
                for point, (frame, value, hl, hr, interp) in zip(curve.keyframe_points, points):
                    point.co = (frame, value)
                    point.interpolation = interp
                    point.handle_left_type = "FREE"
                    point.handle_right_type = "FREE"
                    point.handle_left = hl
                    point.handle_right = hr
                curve.update()
            written += 1
    return written


def import_clip(context, filepath, channel_map_path, fps, target=None,
                bake=True):
    """Load a clip onto an armature, following the binding records.

    Curves are placed by walking each bind block's animated and static
    records: a record names the output channel (hence the bone or slider)
    and its sub-ops say which property and component each value feeds. Each
    block is read with its own template's channel map.

    Returns (action, curves written, unmatched channel labels).
    """
    with tempfile.TemporaryDirectory(prefix="cars2oct_") as tmp:
        block = read_block(filepath, tmp)
    clip = decode_clip(block)
    frames = max(1, int(round(clip["duration"] * fps)))

    name = os.path.splitext(os.path.basename(filepath))[0]
    action = bpy.data.actions.new(name)
    action["cars2_source"] = filepath
    action["cars2_duration"] = clip["duration"]
    action["cars2_fps"] = float(fps)
    try:
        action.use_frame_range = True
        action.frame_start = 0
        action.frame_end = frames
    except AttributeError:
        pass
    container = action_container(action, target)

    maps = load_channel_maps(channel_map_path, filepath, clip["n_binds"])
    pool = clip["pool"]
    pool_floats = list(struct.unpack("<%df" % (len(pool) // 4),
                                     pool[:len(pool) // 4 * 4]))

    groups = {}          # bone -> {(prop, component): keys}
    constants = {}       # bone -> {(prop, component): float}
    scalars = {}         # slider label -> keys or float
    used = {}            # bone -> {"prop:component"} this clip drives
    unmatched = set()
    raw = []             # bake=False: (bone, prop, component, keys or float)
    orders = {}          # bone -> Euler order its rotation sub-op uses

    def place(label, prop, component, value, order=None):
        if prop == "custom":
            scalars[label] = value
            return
        pb, _rank = bone_for_label(target, label)
        if pb is None:
            unmatched.add(label)
            return
        used.setdefault(pb.name, set()).add("%s:%d" % (prop, component))
        if order is not None and prop == "rotation_euler":
            orders[pb.name] = order
        if not bake:
            raw.append((pb, prop, component, value))
        elif isinstance(value, float):
            constants.setdefault(pb.name, {})[(prop, component)] = value
        else:
            groups.setdefault(pb.name, {})[(prop, component)] = value

    for bind_index, blob in enumerate(clip["binds"]):
        try:
            command_indices, static, animated = parse_bind_block(blob)
        except struct.error:
            continue
        channel_map = maps[bind_index] if bind_index < len(maps) else {}
        slots = buffer_slots(clip, command_indices)
        for records in (static, animated):
            for record_target, subs in records:
                label = channel_map.get(record_target, "channel_%d" % record_target)
                for sub_word, operands in subs:
                    order = (euler_order_of(sub_word)
                             if (sub_word & 0xF000) == 0x1000 else None)
                    for prop, comp, slot, k in sub_op_targets(sub_word, operands):
                        if operand_from_buffer(sub_word, k):
                            if slot >= len(slots):
                                continue
                            cmd, channel = slots[slot]
                            place(label, prop, comp, channel_curve(cmd, channel),
                                  order)
                        elif slot < len(pool_floats):
                            # Includes the pool-sourced axes of an animated
                            # Euler sub-op, which an earlier version dropped.
                            place(label, prop, comp, float(pool_floats[slot]),
                                  order)

    action["cars2_euler_orders"] = json.dumps(orders)

    made = 0
    for label, value in sorted(scalars.items()):
        first = value if isinstance(value, float) else value[0][1][0]
        ensure_scalar(target, label, first)
        curve = new_curve(container, scalar_path(label), 0, "Sliders")
        if curve is None:
            continue
        if isinstance(value, float):
            constant_curve(curve, value)
        else:
            fill_curve(curve, value, fps)
        made += 1

    if not bake:
        for pb, prop, comp, value in raw:
            if prop == "rotation_euler":
                pb.rotation_mode = orders.get(pb.name, EULER_ORDER)
            curve = new_curve(container, 'pose.bones["%s"].%s' % (pb.name, prop),
                              comp, pb.name)
            if curve is None:
                continue
            if isinstance(value, float):
                constant_curve(curve, value)
            else:
                fill_curve(curve, value, fps)
            made += 1
        action["cars2_raw"] = True
        return action, made, sorted(unmatched)

    # The expression sliders drive the mouth _Proxy nodes through the rig's
    # relationship data, stored on the armature by the model importer.
    proxy = None
    sliders = {_slider_name(l): v for l, v in scalars.items() if _slider_name(l)}
    if target is not None and sliders:
        model = load_relationship(target)
        if model is None:
            print("mouth sliders present but the armature has no relationship "
                  "data - re-import the model with the current importer")
        else:
            def source(time, inside):
                return {n: (v if isinstance(v, float) else sample_at(v, time, inside))
                        for n, v in sliders.items()}
            proxy = _SliderProxy(model, target, source)

    if target is not None and (groups or constants or proxy is not None):
        made += bake_bone_transforms(container, target, groups, constants,
                                     fps, frames, proxy, orders)
    action["cars2_channels"] = json.dumps({b: sorted(c) for b, c in used.items()})
    return action, made, sorted(unmatched)


# ===========================================================================
# Export
# ===========================================================================
#
# The exporter rebuilds the whole ClipDataBlock from the action - commands,
# bind blocks and constant pool - and borrows only the container from a
# template clip: its LayerBind list (each bind's connection template, i.e.
# which channel numbering it uses) and everything around the block. So any
# timing, any duration, new bones, static poses and sliders all export; the
# template just has to bind the right connection templates. Use the game clip
# the new animation replaces: its file name and internal name stay as they
# are.
#
# What gets written for each bind block: every channel of its template that
# the action drives. A bone counts as driven when the action has F-curves on
# it; within it, a component is written when the clip being edited bound it
# (recorded at import) or when it moves or sits away from rest. Sliders are
# written whenever they have F-curves. Components that never move go to the
# constant pool as static records; the rest are fitted into commands.

_BONE_PATH = re.compile(r'^pose\.bones\["(.+)"\]\.(\w+)$')
_SCALAR_PATH = re.compile(r'^\["(.+)"\]$')
_ROT_PROPS = ("rotation_quaternion", "rotation_euler", "rotation_axis_angle")

# Absolute floor for each kind of value; the working tolerance is the larger
# of this and 0.1% of the channel's range, both scaled by the precision
# setting. "Constant" means the whole channel stays within the floor.
_TOL_FLOOR = {"location": 1e-5, "rotation_euler": 1e-5, "scale": 1e-5,
              "custom": 1e-4}
_TOL_RANGE = 1e-3


def action_inventory(action):
    """({bone: {prop: {index: fcurve}}}, {slider: fcurve}) for an action."""
    bones, scalars = {}, {}
    for fc in action_curves(action):
        m = _BONE_PATH.match(fc.data_path)
        if m:
            bones.setdefault(m.group(1), {}).setdefault(m.group(2), {})[
                fc.array_index] = fc
            continue
        m = _SCALAR_PATH.match(fc.data_path)
        if m and fc.array_index == 0:
            scalars[m.group(1)] = fc
    return bones, scalars


def templates_covering(bone_name):
    """Built-in connection templates with a channel that resolves to a bone."""
    return [name for name, cmap in sorted(BUILTIN_CHANNEL_MAPS.items())
            if any(bone_name in bone_candidates(label)
                   for label in cmap.values() if not is_scalar_label(label))]


def plan_targets(obj, maps, bones_keyed, scalars_keyed, recorded):
    """Which rig targets each bind block writes, and what nothing claims.

    Returns ([[(channel_index, label, kind, name)] per bind], unplaced
    bones, unplaced sliders). kind is "bone" or "scalar".
    """
    plans = []
    placed_bones, placed_scalars = set(), set()
    for cmap in maps:
        entries = []
        best = {}
        for index, label in sorted(cmap.items()):
            if is_scalar_label(label):
                if label in scalars_keyed:
                    entries.append((index, label, "scalar", label))
                    placed_scalars.add(label)
                continue
            pb, rank = bone_for_label(obj, label)
            if pb is None or (pb.name not in bones_keyed and pb.name not in recorded):
                continue
            # Two labels can resolve to one bone (Main and Main_CtrlGrp on a
            # rig with only one of them); the closer name wins.
            key = (rank, pb.name != label, index)
            if pb.name not in best or key < best[pb.name][0]:
                best[pb.name] = (key, index, label)
        for name, (_key, index, label) in best.items():
            entries.append((index, label, "bone", name))
            placed_bones.add(name)
        entries.sort()
        plans.append(entries)
    unplaced_bones = sorted(b for b in bones_keyed if b not in placed_bones)
    unplaced_scalars = sorted(s for s in scalars_keyed if s not in placed_scalars)
    return plans, unplaced_bones, unplaced_scalars


def sample_rig(context, obj, action, bone_names, scalar_names, frames, visual):
    """Bone bases (loc, rot 3x3, scale) and slider values at each frame.

    Keyframe mode evaluates the action's F-curves directly (fast; ignores
    constraints, drivers and NLA). Visual mode steps the scene and reads the
    final evaluated pose, so IK, constraints and drivers are baked in.
    Unkeyed channels in keyframe mode use the bone's current value.
    """
    from mathutils import Euler, Matrix, Quaternion, Vector

    out_b = {n: [] for n in bone_names}
    out_s = {n: [] for n in scalar_names}

    if not visual:
        curves = {(fc.data_path, fc.array_index): fc for fc in action_curves(action)}
        for frame in frames:
            def ev(path, i, default):
                fc = curves.get((path, i))
                return fc.evaluate(frame) if fc is not None else default
            for n in bone_names:
                pb = obj.pose.bones[n]
                base = 'pose.bones["%s"].' % n
                loc = Vector([ev(base + "location", i, pb.location[i]) for i in range(3)])
                mode = pb.rotation_mode
                if mode == "QUATERNION":
                    q = Quaternion([ev(base + "rotation_quaternion", i,
                                       pb.rotation_quaternion[i]) for i in range(4)])
                    rot = (q.normalized().to_matrix() if q.magnitude > 1e-12
                           else Matrix.Identity(3))
                elif mode == "AXIS_ANGLE":
                    aa = [ev(base + "rotation_axis_angle", i, pb.rotation_axis_angle[i])
                          for i in range(4)]
                    axis = Vector(aa[1:])
                    rot = (Matrix.Rotation(aa[0], 3, axis.normalized())
                           if axis.length > 1e-12 else Matrix.Identity(3))
                else:
                    rot = Euler([ev(base + "rotation_euler", i, pb.rotation_euler[i])
                                 for i in range(3)], mode).to_matrix()
                scale = Vector([ev(base + "scale", i, pb.scale[i]) for i in range(3)])
                out_b[n].append((loc, rot, scale))
            for n in scalar_names:
                try:
                    current = float(obj.get(n, 0.0))
                except (TypeError, ValueError):
                    current = 0.0
                out_s[n].append(ev(scalar_path(n), 0, current))
        return out_b, out_s

    scene = context.scene
    keep = (scene.frame_current, scene.frame_subframe)
    try:
        for frame in frames:
            whole = math.floor(frame)
            scene.frame_set(int(whole), subframe=frame - whole)
            for n in bone_names:
                pb = obj.pose.bones[n]
                local = obj.convert_space(pose_bone=pb, matrix=pb.matrix,
                                          from_space="POSE", to_space="LOCAL")
                loc, quat, scale = local.decompose()
                out_b[n].append((loc, quat.to_matrix(), scale))
            for n in scalar_names:
                try:
                    out_s[n].append(float(obj.get(n, 0.0)))
                except (TypeError, ValueError):
                    out_s[n].append(0.0)
    finally:
        scene.frame_set(keep[0], subframe=keep[1])
    return out_b, out_s


def _tolerance(prop, columns, precision):
    """Fit tolerance shared by the components of one property.

    Relative to the property's largest motion, so interpolation leakage
    between components (a bone travelling 18 units in X and Z wobbles about
    2e-4 in Y between baked frames) neither becomes a channel of its own nor
    forces extra keys. Rotation ranges count at most one turn, so a spinning
    wheel does not loosen its steering axes.
    """
    span = max(max(c) - min(c) for c in columns)
    if prop.startswith("rotation"):
        span = min(span, 2.0 * math.pi)
    return max(_TOL_FLOOR.get(prop, 1e-5), _TOL_RANGE * span) * precision


class _Target:
    """One bone or slider's channels, ready to become records."""

    def __init__(self, kind, name):
        self.kind, self.name = kind, name
        self.animated = []      # [(prop, component, values)] in slot order
        self.constant = {}      # (prop, component) -> value
        self.rotation = None    # None | "static" | "animated"
        self.static_quat = None
        self.euler_order = EULER_ORDER
        self.command = None     # index into the clip's command list
        self.keys = 0
        self.hints = set()      # sample indices of the animator's keyframes
        self.lefts = {}         # sample index -> values just before a step
        self._pool = None

    def records(self, slot_base, pool):
        """(animated sub-ops, static sub-ops), adding constants to `pool`."""
        if self._pool is None:
            self._pool = {}

            def put(values):
                # Share runs already in the pool, as the game's exporter did.
                vals = [struct.unpack("<f", struct.pack("<f", float(v)))[0]
                        for v in values]
                width = len(vals)
                for i in range(len(pool) - width + 1):
                    if pool[i:i + width] == vals:
                        return i
                pool.extend(vals)
                return len(pool) - width

            c = self.constant
            if self.kind == "scalar":
                if ("custom", 0) in c:
                    self._pool["custom"] = put([c[("custom", 0)]])
            else:
                loc = [i for i in range(3) if ("location", i) in c]
                if len(loc) == 3:
                    self._pool["loc3"] = put([c[("location", i)] for i in range(3)])
                for i in loc:
                    self._pool[("location", i)] = put([c[("location", i)]])
                if self.rotation == "static":
                    self._pool["quat"] = put(self.static_quat)
                for i in range(3):
                    if ("rotation_euler", i) in c:
                        self._pool[("rotation_euler", i)] = put([c[("rotation_euler", i)]])
                if any(("scale", i) in c for i in range(3)):
                    self._pool["scale3"] = put([c.get(("scale", i), 1.0) for i in range(3)])

        slot = {(p, i): slot_base + n for n, (p, i, _v) in enumerate(self.animated)}
        anim, static = [], []
        if self.kind == "scalar":
            if ("custom", 0) in slot:
                anim.append((0x0019, [slot[("custom", 0)]]))
            elif "custom" in self._pool:
                static.append((0x0009, [self._pool["custom"]]))
            return anim, static

        for i in range(3):
            if ("location", i) in slot:
                anim.append((0x0011 + i, [slot[("location", i)]]))
        if "loc3" in self._pool:
            static.append((0x0000, [self._pool["loc3"]]))
        else:
            for i in range(3):
                if ("location", i) in self._pool:
                    static.append((0x0001 + i, [self._pool[("location", i)]]))
        if self.rotation == "animated":
            word = 0x1000 | (ORDER_CODE.get(self.euler_order, 2) << 6)
            operands = []
            for k in range(3):
                if ("rotation_euler", k) in slot:
                    word |= 1 << (4 - 2 * k)
                    operands.append(slot[("rotation_euler", k)])
                else:
                    operands.append(self._pool[("rotation_euler", k)])
            anim.append((word, operands))
        elif self.rotation == "static":
            static.append((0x0004, [self._pool["quat"]]))
        for i in range(3):
            if ("scale", i) in slot:
                anim.append((0x0016 + i, [slot[("scale", i)]]))
        if "scale3" in self._pool:
            static.append((0x0005, [self._pool["scale3"]]))
        return anim, static


def export_clip(context, filepath, template, obj, action, fps, frame_range,
                visual=False, adaptive=True, precision=1.0, substeps=None):
    """Rebuild a clip from an action, inside a template clip's container.

    Returns a report dict (see the operator for how it is shown).
    """
    if action.get("cars2_raw"):
        raise ValueError("this action was imported without 'Convert to bone "
                         "space'; re-import it with that on to export")

    with tempfile.TemporaryDirectory(prefix="cars2oct_") as tmp:
        template_block = read_block(template, tmp)
    template_clip = decode_clip(template_block)
    n_binds = template_clip["n_binds"]
    maps = load_channel_maps(None, template, n_binds)
    template_names = templates_in_file(template)

    # Samples per frame. Fitting is only checked at samples, so a fit could
    # bulge between two of them around a sharp corner; 1/8 frame keeps that
    # well inside tolerance. Visual sampling steps the whole scene per
    # sample, so it trades a little of that margin for speed.
    if substeps is None:
        substeps = 4 if visual else 8
    start, end = frame_range
    frames = max(1, int(round(end - start)))
    t_end = frames / float(fps)
    n = frames * substeps
    h = t_end / n
    sample_frames = [start + i / float(substeps) for i in range(n + 1)]

    bones_keyed, scalars_keyed = action_inventory(action)
    try:
        recorded = json.loads(action.get("cars2_channels", "{}"))
    except (TypeError, ValueError):
        recorded = {}
    try:
        euler_orders = json.loads(action.get("cars2_euler_orders", "{}"))
    except (TypeError, ValueError):
        euler_orders = {}
    plans, unplaced_bones, unplaced_scalars = plan_targets(
        obj, maps, bones_keyed, scalars_keyed, recorded)

    bone_names = sorted({name for plan in plans for _i, _l, kind, name in plan
                         if kind == "bone"})
    scalar_names = sorted({name for plan in plans for _i, _l, kind, name in plan
                           if kind == "scalar"})
    # Mouth sliders feed the proxy whether or not the template binds them.
    proxy_inputs = sorted(s for s in scalars_keyed if _slider_name(s))
    sampled_b, sampled_s = sample_rig(context, obj, action, bone_names,
                                      sorted(set(scalar_names) | set(proxy_inputs)),
                                      sample_frames, visual)

    proxy = None
    model = load_relationship(obj) if proxy_inputs else None
    if model is not None:
        values = {_slider_name(s): sampled_s[s] for s in proxy_inputs}

        def source(time, _inside):
            i = min(n, max(0, int(round(time / h))))
            return {k: v[i] for k, v in values.items()}
        proxy = _SliderProxy(model, obj, source)

    def key_hints(fcurve_sets):
        hints = set()
        for fc in fcurve_sets:
            for point in fc.keyframe_points:
                hints.add(int(round((point.co[0] - start) * substeps)))
        return hints

    def step_indices(fcurve_sets):
        """Samples where a curve steps: the key after a Constant one."""
        steps = set()
        for fc in fcurve_sets:
            pts = sorted(fc.keyframe_points, key=lambda p: p.co[0])
            for p, q in zip(pts, pts[1:]):
                if p.interpolation == "CONSTANT" and q.co[1] != p.co[1]:
                    i = int(round((q.co[0] - start) * substeps))
                    if 0 < i < n:
                        steps.add(i)
        return steps

    # Just before a step, for its left-hand value: well inside the sample
    # spacing, well outside float noise in frame times.
    before = 1e-3

    def bone_curves(name):
        return [fc for prop in bones_keyed.get(name, {}).values() for fc in prop.values()]

    def to_stored(name, rest, rest_inv, samples, index_of, compat_of):
        locs, eulers, scales = [], [], []
        moved = 0.0
        prev = None
        for j, (loc, rot, scale) in enumerate(samples):
            i = index_of(j)
            px = (proxy.offset(name, i * h)
                  if proxy is not None and name in proxy.bones else None)
            s_loc, s_rot = basis_to_stored(rest, rest_inv, loc, rot, px)
            compat = compat_of(j, prev)
            order = euler_orders.get(name, EULER_ORDER)
            euler = (s_rot.to_euler(order, compat) if compat is not None
                     else s_rot.to_euler(order))
            prev = euler
            moved = max(moved, s_rot.to_quaternion().angle)
            locs.append(s_loc)
            eulers.append(euler)
            scales.append(scale)
        return locs, eulers, scales, moved

    targets = {}
    skipped = []
    worst = 0.0

    for name in bone_names:
        pb = obj.pose.bones[name]
        rest = rest_local_matrix(pb).to_3x3()
        rest_inv = rest.inverted()
        rec = set(recorded.get(name, []))
        locs, eulers, scales, moved_rot = to_stored(
            name, rest, rest_inv, sampled_b[name], lambda j: j, lambda j, prev: prev)
        steps = sorted(step_indices(bone_curves(name)))
        left_at = {}
        if steps:
            lb, _ls = sample_rig(context, obj, action, [name], [],
                                 [start + i / float(substeps) - before for i in steps],
                                 visual)
            l_locs, l_eulers, l_scales, _m = to_stored(
                name, rest, rest_inv, lb[name], lambda j: steps[j],
                lambda j, prev: eulers[steps[j]])
            for j, i in enumerate(steps):
                left_at[i] = {("location", c): l_locs[j][c] for c in range(3)}
                left_at[i].update({("rotation_euler", c): l_eulers[j][c] for c in range(3)})
                left_at[i].update({("scale", c): l_scales[j][c] for c in range(3)})

        t = _Target("bone", name)
        t.euler_order = euler_orders.get(name, EULER_ORDER)
        t.tols = {}
        loc_cols = [[v[i] for v in locs] for i in range(3)]
        tol = _tolerance("location", loc_cols, precision)
        for i, vals in enumerate(loc_cols):
            if "location:%d" % i in rec or max(abs(x) for x in vals) > tol:
                if max(vals) - min(vals) <= tol:
                    t.constant[("location", i)] = (max(vals) + min(vals)) * 0.5
                else:
                    t.animated.append(("location", i, vals))
                    t.tols[("location", i)] = tol
        axes = [[e[k] for e in eulers] for k in range(3)]
        tol = _tolerance("rotation_euler", axes, precision)
        rot_rec = any(r.split(":")[0] in _ROT_PROPS for r in rec)
        if rot_rec or moved_rot > tol:
            if all(max(a) - min(a) <= tol for a in axes):
                t.rotation = "static"
                q = eulers[0].to_quaternion()
                if CLIP_QUATERNION_CONJUGATED:          # written conjugated
                    q = q.conjugated()
                t.static_quat = (q.x, q.y, q.z, q.w)
            else:
                t.rotation = "animated"
                for k, a in enumerate(axes):
                    if max(a) - min(a) <= tol:
                        t.constant[("rotation_euler", k)] = (max(a) + min(a)) * 0.5
                    else:
                        t.animated.append(("rotation_euler", k, a))
                        t.tols[("rotation_euler", k)] = tol
        scale_cols = [[v[i] for v in scales] for i in range(3)]
        tol = _tolerance("scale", scale_cols, precision)
        scale_on = [("scale:%d" % i in rec) or
                    max(abs(x - 1.0) for x in scale_cols[i]) > tol
                    for i in range(3)]
        if any(scale_on):
            moving = [max(c) - min(c) > tol for c in scale_cols]
            if any(moving):
                # Static single-axis scale never occurs in shipped clips, so
                # a partly animated scale goes out as three curves.
                for i in range(3):
                    t.animated.append(("scale", i, scale_cols[i]))
                    t.tols[("scale", i)] = tol
            else:
                for i in range(3):
                    t.constant[("scale", i)] = scale_cols[i][0]
        if not t.animated and not t.constant and t.rotation is None:
            skipped.append(name)
            continue
        t.hints = key_hints(bone_curves(name))
        t.lefts = {i: [vals[(p, c)] for p, c, _v in t.animated]
                   for i, vals in left_at.items()}
        targets[("bone", name)] = t

    for name in scalar_names:
        vals = sampled_s[name]
        t = _Target("scalar", name)
        tol = _tolerance("custom", [vals], precision)
        if max(vals) - min(vals) <= tol:
            t.constant[("custom", 0)] = (max(vals) + min(vals)) * 0.5
        else:
            t.animated.append(("custom", 0, vals))
            t.tols = {("custom", 0): tol}
            steps = sorted(step_indices([scalars_keyed[name]]))
            if steps:
                _lb, ls = sample_rig(context, obj, action, [], [name],
                                     [start + i / float(substeps) - before for i in steps],
                                     visual)
                t.lefts = {i: [ls[name][j]] for j, i in enumerate(steps)}
        t.hints = key_hints([scalars_keyed[name]])
        targets[("scalar", name)] = t

    commands = []
    n_animated = n_static = n_keys = 0
    every_frame = set(range(0, n, substeps))
    for key in sorted(targets):
        t = targets[key]
        n_static += len(t.constant) + (1 if t.rotation == "static" else 0)
        if not t.animated:
            continue
        series = [v for _p, _i, v in t.animated]
        tols = [t.tols[(p, i)] for p, i, _v in t.animated]
        hints = t.hints if adaptive else every_frame
        keys, segs, err = fit_keys(series, h, hints, tols, adaptive,
                                   lefts=t.lefts)
        cmd = encode_command_channels(keys, segs, h, t_end)
        # Measure what the game will actually see: decode and resample.
        decoded = decode_command(encode_command(cmd), 0)
        for c, values in enumerate(series):
            curve = channel_curve(decoded, c)
            for i in range(n + 1):
                tt = i * h
                e = abs(sample_at(curve, tt, min(tt + h * 0.5, t_end)) - values[i])
                err = max(err, e / tols[c])
        worst = max(worst, err)
        t.command = len(commands)
        t.keys = len(keys)
        commands.append(cmd)
        n_animated += len(series)
        n_keys += len(keys)

    pool = []
    binds = []
    for plan in plans:
        cmd_indices, anim_recs, static_recs = [], [], []
        slot = 0
        for index, _label, kind, name in plan:
            t = targets.get((kind, name))
            if t is None:
                continue
            base = slot
            if t.command is not None:
                cmd_indices.append(t.command)
                slot += len(t.animated)
            anim, static = t.records(base, pool)
            if anim:
                anim_recs.append((index, anim, kind == "scalar"))
            if static:
                static_recs.append((index, static, kind == "scalar"))
        binds.append(build_bind_block(slot, cmd_indices, anim_recs, static_recs))

    clip = {"flags": 0, "duration": t_end, "n_binds": n_binds,
            "commands": commands, "binds": binds,
            "pool": struct.pack("<%df" % len(pool), *pool)}
    block = encode_clip(clip)
    write_block(template, block, filepath)
    if read_block_native(filepath) != block:
        raise RuntimeError("container rewrite did not read back; output is unsafe")

    return {
        "templates": template_names,
        "duration": t_end, "frames": frames,
        "bones": sum(1 for k in targets if k[0] == "bone"),
        "sliders": sum(1 for k in targets if k[0] == "scalar"),
        "animated": n_animated, "static": n_static, "keys": n_keys,
        "unplaced_bones": unplaced_bones, "unplaced_sliders": unplaced_scalars,
        "skipped": skipped, "worst": worst,
        "proxy": proxy is not None,
        "proxy_missing": bool(proxy_inputs) and model is None,
    }


# ===========================================================================
# Operators and UI
# ===========================================================================

_FPS_ITEMS = [("30", "30 fps", "The game's native rate"),
              ("60", "60 fps", ""), ("24", "24 fps", "")]

_template_cache = {}


def template_summary(path):
    """[(template name, channel count)] per bind of a template clip (cached)."""
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return None
    hit = _template_cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        with tempfile.TemporaryDirectory(prefix="cars2oct_") as tmp:
            n_binds = decode_clip(read_block(path, tmp))["n_binds"]
        names = templates_in_file(path)
        summary = [(names[i] if i < len(names) else "?",
                    len(BUILTIN_CHANNEL_MAPS.get(names[i], {})) if i < len(names) else 0)
                   for i in range(n_binds)]
    except Exception:
        summary = None
    _template_cache[path] = (stamp, summary)
    return summary


class CARS2_OT_import(bpy.types.Operator, ImportHelper):
    """Import a Cars 2 animation clip onto the selected (or only) armature"""
    bl_idname = "import_scene.cars2_oct"
    bl_label = "Import Cars 2 Animation"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".oct"
    filter_glob: StringProperty(default="*.oct", options={"HIDDEN"})
    channel_map: StringProperty(
        name="Channel map",
        description="Optional channels.json to extend or override the "
                    "built-in maps",
        subtype="FILE_PATH")
    bone_space: BoolProperty(
        name="Convert to bone space",
        description="Rebuild each bone's transform in Blender's bone space. "
                    "Turn off only to inspect the raw stored values; raw "
                    "imports cannot be exported",
        default=True)
    fps: EnumProperty(name="Frame rate", items=_FPS_ITEMS, default="30",
                      description="Clips are authored on a 30 fps key grid")
    set_scene: BoolProperty(
        name="Set scene range and rate",
        description="Match the scene's frame range and frame rate to the clip",
        default=True)

    def execute(self, context):
        return apply_clip_file(self, context, self.filepath)


def apply_clip_file(op, context, filepath, extra_note=""):
    """Import a Cars 2 clip file onto the target armature - shared by the
    Cars 2 and the Cars 3 import (which converts first)."""
    self = op
    if True:
        target = find_target_armature(context) or context.object
        fps = float(self.fps)
        if target is not None and target.type == "ARMATURE" and \
                not any(n in target.data.bones for n in ("Main_Ctrl", "Body_Ctrl", "FrontEnd_Ctrl")) and                 not any(b.name.endswith("_Ctrl") for b in target.data.bones):     # misc rigs: ControlMain_Ctrl ...
            # Clips drive the controls (Body_Ctrl, Jaw_Ctrl, the lip and eye
            # controls, the wheel controls), not the game bones: on an armature
            # without them (Import Cars 2 Character with Import Control Rig off,
            # or a plain skeleton) a clip moves a few bones the wrong way and
            # leaves the eyes and mouth dead.
            self.report({"ERROR"}, "%s has no control rig, so clips cannot play on it. Import the "
                                   "character with 'Import Control Rig' ticked (File > Import > Cars 2 "
                                   "Character)" % target.name)
            return {"CANCELLED"}
        if target is not None:
            # Clear first: bones and sliders this clip has no channel for
            # would otherwise keep the pose left by the previous import.
            reset_pose(target)
        try:
            action, count, unmatched = import_clip(
                context, filepath, self.channel_map, fps, target,
                self.bone_space)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        if target is not None:
            assign_action(target, action)
            context.view_layer.objects.active = target
            settle_eyelids(target, action)

        if self.set_scene:
            scene = context.scene
            scene.render.fps = int(fps)
            scene.render.fps_base = 1.0
            scene.frame_start = 0
            scene.frame_end = int(round(action["cars2_duration"] * fps))

        extra_note += bake_cars3_chains(context, target)
        if target is not None and target.type == "ARMATURE":
            note = ""
            if unmatched:
                note = "  (%d channels have no bone, e.g. %s)" % (
                    len(unmatched), ", ".join(unmatched[:3]))
            self.report({"INFO"}, "Applied '%s' to '%s' - %d curves%s%s"
                        % (action.name, target.name, count, note, extra_note))
        else:
            self.report({"WARNING"},
                        "No armature found - only slider curves were created "
                        "on '%s'" % action.name)
        return {"FINISHED"}


# --------------------------------------------------------------- Cars 3 clips
# Cars 3 (Driven to Win) clips go through cars3_to_cars2.py (core/, see
# CARS3_ANIM_NOTES.md): the curves are the same format in both games; the
# channel addressing, record layout, one field op and the header are
# translated, Cars 3-only channels dropped and reported.

_C3_MODE_ITEMS = [
    ("auto", "Auto", "Frontend for *_fe_* / select_* clips, otherwise Game"),
    ("game", "Game", "car_body + car_facial (driving, tricks, emotes ...)"),
    ("frontend", "Frontend", "car_frontend_frontend (character select)"),
    ("custom", "Templates", "The Cars 2 templates listed in Templates, e.g. car_body,car_rattles "
                            "(Rattles, Eyes, Eyelids, BodyBlend, Lean, ChoBlender or Mouth clip sets)"),
]


def layer_actions(base, top, target):
    """Lay `top` over `base`: every curve of `top` replaces the base curve
    with the same data path and index (or is added); the base keeps the
    curves `top` doesn't drive. Returns (curves replaced, curves added,
    base curves kept)."""
    cont = action_container(base, target)
    top_curves = action_curves(top)
    wanted = {(fc.data_path, fc.array_index) for fc in top_curves}
    replaced = 0
    for fc in list(action_curves(base)):
        if (fc.data_path, fc.array_index) in wanted:
            cont.fcurves.remove(fc)
            replaced += 1
    kept = len(action_curves(base))
    for fc in top_curves:
        group = fc.group.name if getattr(fc, "group", None) else ""
        try:
            new = cont.fcurves.new(fc.data_path, index=fc.array_index, action_group=group)
        except TypeError:
            new = cont.fcurves.new(fc.data_path, index=fc.array_index)
        new.keyframe_points.add(len(fc.keyframe_points))
        for a, b in zip(fc.keyframe_points, new.keyframe_points):
            b.co = a.co
            b.interpolation = a.interpolation
            b.handle_left_type, b.handle_right_type = a.handle_left_type, a.handle_right_type
            b.handle_left, b.handle_right = a.handle_left, a.handle_right
        new.update()
    return replaced, len(top_curves) - replaced, kept


def clip_templates(path):
    """The connection templates a Cars 2 clip file binds (LayerBind names)."""
    import c2oct
    sn = c2oct.load(path)['SubNetworkPool']['SubNetwork#0']
    return [b.get('Name') for b in (sn.get('LayerBindPool') or {}).values()]


# --------------------------------------------------------------- Cars 3 cutscenes
# A Cars 3 cutscene (assets/choreographies/cc/ccl_*.zip) gives every actor two
# clips: motions/cars3_<actor>_clip.oct, the rig animation (characters/car/cars3.ct,
# converted like any Cars 3 clip), and motions/cars3_<actor>_pn_clip.oct, its
# placement in the set (template Choreographies/<cho>/placement.ct, AttachName =
# the actor): port 0 = location x/y/z + an Euler rotation, port 1 = visibility.
# The game hangs the character under that placement node, so the import builds
# the same chain: an empty per cutscene (the Y-up -> Z-up turn the armature
# normally carries itself), an empty per actor with the placement curves as
# stored, and the armature under it.

PROP_CUTSCENE = "cars2_cutscene"            # on the cutscene root empty: choreography name
PROP_PLACEMENT = "cars2_placement"          # on an actor's placement empty: the actor
PROP_ACTOR_ROOT = "cars2_actor_root"       # on an actor's root-motion empty (under its placement)
PROP_CUT_PARENT = "cars2_cutscene_actor"    # on the armature: it hangs under a placement


def _clip_block_of(raw):
    """(ClipDataBlock bytes, LayerBind dict) of a clip .oct's bytes."""
    import octread
    sn = octread.decode(raw)[0]["SubNetworkPool"]["SubNetwork#0"]
    blk = sn["DataNodePool"]["DataNode#0"]["ClipDataBlock"]
    if isinstance(blk, str):
        import cars3_to_cars2
        blk = cars3_to_cars2.b64_decode(blk)
    bind = next(iter((sn.get("LayerBindPool") or {}).values()), {})
    return bytes(blk), bind


def _choreography_dirs(filepath, target):
    """Folders that may hold the cutscene zips: the choreographies folder above the
    clip, else the one of the game the Cars 3 character was imported from."""
    starts = [os.path.dirname(os.path.abspath(filepath))]
    try:
        from . import cars3
        src = target.get(cars3.PROP_SOURCE) if target is not None else None
        if src:
            starts.append(os.path.dirname(src))
    except Exception:
        pass
    found = []
    for start in starts:
        d = start
        while True:
            for cand in (d, os.path.join(d, "choreographies"), os.path.join(d, "assets", "choreographies")):
                if os.path.basename(cand).lower() == "choreographies" and os.path.isdir(cand) \
                        and cand not in found:
                    found.append(cand)
            up = os.path.dirname(d)
            if up == d:
                break
            d = up
        if found:
            break
    return found


def find_placement_clip(filepath, target=None):
    """(bytes, where) of the cutscene placement clip that goes with a Cars 3 actor
    clip, or (None, None) - next to the clip (an unpacked cutscene) or, for a clip
    taken out of one, in the cutscene zip holding a byte-identical copy of it."""
    stem, ext = os.path.splitext(os.path.basename(filepath))
    if not stem.lower().endswith("_clip") or stem.lower().endswith("_pn_clip"):
        return None, None
    pn_name = stem[:-len("_clip")] + "_pn_clip" + ext
    side = os.path.join(os.path.dirname(filepath), pn_name)
    if os.path.isfile(side):
        with open(side, "rb") as f:
            return f.read(), side
    import zipfile
    with open(filepath, "rb") as f:
        mine = f.read()
    want, want_pn = ("motions/" + stem + ext).lower(), ("motions/" + pn_name).lower()
    for folder in _choreography_dirs(filepath, target):
        for root, _dirs, files in os.walk(folder):
            for name in files:
                if not name.lower().endswith(".zip"):
                    continue
                zpath = os.path.join(root, name)
                try:
                    with zipfile.ZipFile(zpath) as z:          # read only
                        names = {n.lower(): n for n in z.namelist()}
                        if want in names and want_pn in names and z.read(names[want]) == mine:
                            return z.read(names[want_pn]), "%s:%s" % (zpath, names[want_pn])
                except (zipfile.BadZipFile, OSError, KeyError):
                    continue
    return None, None


def port0_curves(raw):
    """The transform port 0 of a Cars 3 clip's first bind: ({(prop, index): keys or
    value}, rotation order, LayerBind). Keys are channel_curve() lists, values floats.
    In a placement clip port 0 is the placement; in a cutscene actor clip it is the
    character's own root (the converter drops it: Cars 2 clips have no such channel)."""
    blk, bind = _clip_block_of(raw)
    import cars3_to_cars2
    clip = decode_clip(blk)
    bind_c3 = cars3_to_cars2.parse_bind_c3(clip["binds"][0])
    slots = buffer_slots(clip, bind_c3["cmds"])
    pool = clip["pool"]
    floats = struct.unpack("<%df" % (len(pool) // 4), pool[:len(pool) // 4 * 4])
    channels, order = {}, None
    for records, animated in ((bind_c3["static"], False), (bind_c3["animated"], True)):
        for port, subs in records:
            if port != 0:                   # placement port 1: visibility
                continue
            for sw, ops in subs:
                if sw & 0xF000 == 0x1000:
                    order = euler_order_of(sw)
                for prop, comp, slot, k in sub_op_targets(sw, ops):
                    if prop not in ("location", "rotation_euler", "rotation_quaternion", "scale"):
                        continue
                    if animated and operand_from_buffer(sw, k) and slot < len(slots):
                        channels[(prop, comp)] = channel_curve(*slots[slot])
                    elif slot < len(floats):
                        value = float(floats[slot])
                        if prop == "rotation_quaternion" and comp and CLIP_QUATERNION_CONJUGATED:
                            value = -value          # stored conjugated, like clip quaternions
                        channels[(prop, comp)] = value
    return channels, order, bind


def placement_curves(raw):
    """Read a placement clip: (choreography, actor, channels, rotation order)."""
    channels, order, bind = port0_curves(raw)
    template = bind.get("ConnectionTemplate", "")
    parts = template.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0].lower() != "choreographies":
        raise ValueError("not a cutscene placement clip (template %r)" % template)
    return parts[1], bind.get("AttachName") or "Actor", channels, order


def _cutscene_root(cho, collection):
    for obj in bpy.data.objects:
        if obj.type == "EMPTY" and obj.get(PROP_CUTSCENE) == cho:
            return obj
    from . import c2_model
    root = bpy.data.objects.new(cho, None)
    root.empty_display_type = "ARROWS"
    root.rotation_euler = (math.radians(c2_model.GLOBAL_ROTATE_X_DEGREES), 0.0, 0.0)
    root[PROP_CUTSCENE] = cho
    collection.objects.link(root)
    return root


def _animated_empty(parent, tag, actor, name, display, collection, channels, order, fps, action_name):
    """The child empty of `parent` tagged `tag` = actor, keyed with `channels`."""
    empty = next((o for o in parent.children if o.type == "EMPTY" and o.get(tag) == actor), None)
    if empty is None:
        empty = bpy.data.objects.new(name, None)
        empty.empty_display_type = display
        empty[tag] = actor
        collection.objects.link(empty)
        empty.parent = parent
    if empty.animation_data and empty.animation_data.action:
        old = empty.animation_data.action
        empty.animation_data.action = None
        if old.users == 0:
            bpy.data.actions.remove(old)
    empty.matrix_parent_inverse.identity()
    empty.location, empty.scale = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
    quat = any(p == "rotation_quaternion" for p, _ in channels) and         not any(p == "rotation_euler" for p, _ in channels)
    empty.rotation_mode = "QUATERNION" if quat else (order or "XYZ")
    empty.rotation_euler = (0.0, 0.0, 0.0)
    empty.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    action = bpy.data.actions.new(action_name)
    container = action_container(action, empty)
    for (prop, comp), value in sorted(channels.items()):
        if isinstance(value, float):
            getattr(empty, prop)[comp] = value
        elif value:
            fill_curve(new_curve(container, prop, comp, "Placement"), value, fps)
    assign_action(empty, action)
    return empty


def apply_placement(context, target, raw, fps, clip_raw=None):
    """Hang `target` under its cutscene placement, animated as the game does:
    <cutscene> -> <actor>_PN (the placement clip) -> <actor>_Root (port 0 of the
    actor clip `clip_raw`, when it moves the character) -> the armature.
    Returns (actor, choreography, root keyed)."""
    cho, actor, channels, order = placement_curves(raw)
    collection = target.users_collection[0] if target.users_collection else context.scene.collection
    root = _cutscene_root(cho, collection)
    pn = _animated_empty(root, PROP_PLACEMENT, actor, actor + "_PN", "SINGLE_ARROW", collection,
                         channels, order, fps, "%s_%s_PN" % (cho, actor))
    own, own_order = (port0_curves(clip_raw)[:2]) if clip_raw else ({}, None)
    parent = pn
    old_root = next((o for o in pn.children if o.type == "EMPTY" and o.get(PROP_ACTOR_ROOT) == actor), None)
    if own:
        parent = _animated_empty(pn, PROP_ACTOR_ROOT, actor, actor + "_Root", "PLAIN_AXES", collection,
                                 own, own_order, fps, "%s_%s_Root" % (cho, actor))
    elif old_root is not None:
        bpy.data.objects.remove(old_root)
    # the cutscene root carries the Y-up -> Z-up turn now, so the armature sits at identity
    if not target.get(PROP_CUT_PARENT):
        target[PROP_CUT_PARENT + "_rest"] = [list(r) for r in target.matrix_basis]
    target.parent = parent
    target.matrix_parent_inverse.identity()
    target.matrix_basis.identity()
    target[PROP_CUT_PARENT] = actor
    return actor, cho, bool(own)


def leave_cutscene(target):
    """A non-cutscene clip on an armature left under a placement: put it back."""
    if target is None or not target.get(PROP_CUT_PARENT):
        return False
    from mathutils import Matrix
    rest = target.get(PROP_CUT_PARENT + "_rest")
    target.parent = None
    target.matrix_parent_inverse.identity()
    if rest:
        target.matrix_basis = Matrix([list(r) for r in rest])
    del target[PROP_CUT_PARENT]
    return True


class _Cars3ClipOptions:
    mode: EnumProperty(name="Mode", items=_C3_MODE_ITEMS, default="auto",
                       description="Which Cars 2 templates the clip is written for")
    templates: StringProperty(name="Templates", default="",
                              description="Mode Templates: comma list of Cars 2 templates, e.g. car_body,car_rattles")
    # v2.0.56: Port map / Cars 3 parts no longer in the dialogs (user). The
    # built-in port map is complete; a character's own parts (Fritter's blade)
    # come from its Cars 3 armature in the scene, stored there by Export Cars 3
    # as Cars 2 (store_part_ports). Both stay as hidden options for scripts.
    port_map: StringProperty(name="Port map", subtype="FILE_PATH", default="", options={"HIDDEN", "SKIP_SAVE"},
                             description='Optional JSON overrides for the Cars 3 port map: '
                                         '{"163": ["car_body", 32, "Main"], "15": null}')
    # v2.0.56: Scale ops / Keep rest statics removed from the dialogs (user) -
    # always scale ops on face bones only, Cars 3 rest-pose statics dropped
    scale = "face"
    keep_rest = False
    cars2_names: BoolProperty(name="Use Cars 2 names", default=True,
                              description="Name the converted clip the way Cars 2 names that kind of clip "
                                          "(shrd_ -> shared_, trk_ -> truck_ ...; the rules are in "
                                          "core/clip_names.json) - the file and the clip inside it")
    part_ports: StringProperty(name="Cars 3 parts", subtype="FILE_PATH", default="", options={"HIDDEN", "SKIP_SAVE"},
                               description="The <name>.cars3_ports.json Export Cars 3 as Cars 2 wrote for this "
                                           "character: Cars 3 parts it gave their own channels (Fritter's blade) - "
                                           "their animation is kept instead of dropped")

    def convert(self, src, parts=None):
        import c3anim
        if self.part_ports:
            parts = c3anim.load_part_ports(bpy.path.abspath(self.part_ports))
        return c3anim.convert(src, self.mode, self.templates,
                              bpy.path.abspath(self.port_map) if self.port_map else None,
                              self.scale, self.keep_rest, parts, self.cars2_names)

    def draw_cars3(self, layout):
        box = layout.box()
        box.label(text="Cars 3 clip")
        box.prop(self, "mode")
        if self.mode == "custom":
            box.prop(self, "templates")
        box.prop(self, "cars2_names")


_MODEL_PORTS = {}
PROP_PART_PORTS = "cars2tk_part_ports"


def store_part_ports(arm, ports):
    """v2.0.56: Export Cars 3 as Cars 2 keeps {port: {template: [slot, name]}}
    on the Cars 3 armature (removed when the export gave no part a channel)."""
    if ports:
        arm[PROP_PART_PORTS] = json.dumps({str(k): v for k, v in ports.items()})
    elif PROP_PART_PORTS in arm.keys():
        del arm[PROP_PART_PORTS]


def stored_part_ports(arm):
    """{port: {template: [slot, name]}} stored by the last export, or None."""
    raw = arm.get(PROP_PART_PORTS) if arm is not None else None
    if not raw:
        return None
    try:
        return {int(p): v for p, v in json.loads(raw).items()}
    except (ValueError, TypeError, AttributeError):
        return None


def _cars3_char_dir(arm):
    """The Cars 3 character folder of an imported Cars 3 armature (assets/characters/cars3_fritter)."""
    from . import cars3
    src = arm.get(cars3.PROP_SOURCE)
    if not src:
        return ""
    d = os.path.dirname(os.path.normcase(os.path.abspath(bpy.path.abspath(src))))
    # the model can sit one folder down (cars3_fritter/cars3_fritter/cars3_fritter.oct)
    up = os.path.dirname(d)
    return up if os.path.basename(up).lower().startswith("cars3_") else d


def armature_for_clips(context, folder):
    """The Cars 3 armature the clips in `folder` belong to: the one whose
    character folder holds them (Fritter's clips under characters/cars3_fritter,
    FE clips included), else the active Cars 3 armature, else the only one."""
    from . import cars3
    arms = [o for o in context.scene.objects if o.type == "ARMATURE" and cars3.PROP_SOURCE in o.keys()]
    folder = os.path.normcase(os.path.abspath(folder))
    best = None
    for a in arms:
        d = _cars3_char_dir(a)
        if d and (folder == d or folder.startswith(d + os.sep)):
            if best is None or len(d) > len(_cars3_char_dir(best)):
                best = a
    if best is not None:
        return best
    act = context.active_object
    for o in (act, act.parent if act is not None else None):
        if o in arms:
            return o
    return arms[0] if len(arms) == 1 else None


def bake_cars3_chains(context, target):
    """v2.0.53: a Cars 3 model's dynamic chains (Fritter's) simulated over the new clip, as in the game."""
    if target is None or target.type != "ARMATURE":
        return ""
    try:
        from . import c3chains
        return c3chains.bake(context, target)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return "  [chains not simulated: %s]" % exc


def _cars3_model_ports(path):
    """{port: 'Anchor/X_Ctrl/Transform'} of a Cars 3 model (cached per file)."""
    key = (path, os.path.getmtime(path))
    if key not in _MODEL_PORTS:
        from . import cars3
        import c3toc2
        _MODEL_PORTS[key] = {p: path_ for path_, p in c3toc2.cars3_ports(cars3.read_cars3(path)[0]).items()}
    return _MODEL_PORTS[key]


class CARS2_OT_import_cars3(bpy.types.Operator, ImportHelper, _Cars3ClipOptions):
    """Import a Cars 3 (Driven to Win) animation clip onto the armature - converted to Cars 2 first"""
    bl_idname = "import_scene.cars3_oct"
    bl_label = "Import Cars 3 Animation"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".oct"
    filter_glob: StringProperty(default="*.oct;*.json", options={"HIDDEN"})
    channel_map: StringProperty(name="Channel map", subtype="FILE_PATH",
                                description="Optional channels.json to extend or override the built-in maps")
    bone_space: BoolProperty(name="Convert to bone space", default=True,
                             description="Rebuild each bone's transform in Blender's bone space")
    fps: EnumProperty(name="Frame rate", items=_FPS_ITEMS, default="30",
                      description="Clips are authored on a 30 fps key grid")
    set_scene: BoolProperty(name="Set scene range and rate", default=True,
                            description="Match the scene's frame range and frame rate to the clip")
    base_clip: StringProperty(
        name="Cars 2 base clip", subtype="FILE_PATH", default="",
        description="Optional: the Cars 2 clip for this slot (e.g. the character's own select idle). "
                    "It is loaded first - Cars 2's setup for every channel it has - and the Cars 3 "
                    "clip is laid on top, replacing only the channels it drives. Export Clip then "
                    "writes the result with this clip's Cars 2 bindings")

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "fps")
        layout.prop(self, "set_scene")
        layout.prop(self, "bone_space")
        layout.prop(self, "channel_map")
        self.draw_cars3(layout)
        box = layout.box()
        box.label(text="Cars 2 setup underneath")
        box.prop(self, "base_clip")

    def execute(self, context):
        import c3anim
        try:
            doc, info = self.convert(self.filepath)
        except Exception as exc:
            self.report({"ERROR"}, "Cars 3 clip not converted: %s" % exc)
            return {"CANCELLED"}
        folder = os.path.join(tempfile.gettempdir(), "cars2tk_cars3_clips")
        own = None
        try:
            own = self.own_parts(context, info, folder)
        except Exception as exc:
            print("Cars 3 clip: own parts not routed: %s" % exc)
        saved_map = self.channel_map
        if own is not None:
            doc, info, self.channel_map, names = own
        path = c3anim.write_oct(doc, os.path.join(folder, info["name"] + ".oct"))
        note = "  [Cars 3, %s: %s]" % (info["mode"], info["summary"])
        if own is not None:
            note += "  [+ the model's own parts: %s]" % ", ".join(names)
        base = bpy.path.abspath(self.base_clip) if self.base_clip else ""
        try:
            if not base:
                result = apply_clip_file(self, context, path, note)
            else:
                result = self.apply_layered(context, base, path, info, note)
        finally:
            self.channel_map = saved_map
        if "FINISHED" in result:
            self.place_in_cutscene(context)
        return result

    def own_parts(self, context, info, folder):
        """v2.0.52: a Cars 3 model in Blender plays the clip channels Cars 2 has no slot for (Fritter's
        blade) on its own bones. The model's port table names each dropped port (Anchor/Blade_Ctrl/Transform);
        when the armature has that bone, the port borrows a template slot the armature has no bone for
        (Mater's crane slots first, as the exporter does) and a channel map points that slot at the bone.
        Returns (doc, info, channel map path, part names) or None."""
        if self.part_ports or not info.get("dropped"):
            return None
        target = find_target_armature(context) or context.object
        if target is None or target.type != "ARMATURE":
            return None
        from . import cars3
        src = target.get(cars3.PROP_SOURCE)
        if not src or not os.path.isfile(bpy.path.abspath(src)):
            return None
        ports = _cars3_model_ports(bpy.path.abspath(src))
        parts = []
        for port in info["dropped"]:
            m = re.match(r"^Anchor/(.+)_Ctrl/Transform$", ports.get(port, ""))
            if m and bone_for_label(target, m.group(1))[0] is not None:
                parts.append((port, m.group(1)))
        if not parts:
            return None
        import c3anim
        import c3toc2
        import cars3_to_cars2 as c32
        part_ports, chan = {}, {}
        stored = stored_part_ports(target)
        if stored:                  # v2.0.56: the slots Export Cars 3 as Cars 2 gave these parts
            for port, bone in parts:
                for tm, (slot, _name) in (stored.get(port) or {}).items():
                    if tm in info["templates"]:
                        part_ports.setdefault(port, {})[tm] = [int(slot), bone]
                        chan.setdefault(tm, {})[str(slot)] = bone
        if not part_ports:          # not exported yet: borrow free slots for the preview
            taken = {}
            for entries in c32.load_portmap(None, info["templates"]).values():
                for e in entries:
                    taken.setdefault(e[0], set()).add(int(e[1]))
            borrow = [b[:-len("_Ctrl")] if b.endswith("_Ctrl") else b for b in c3toc2.BORROW_FIRST]
            for tm in info["templates"]:
                table = BUILTIN_CHANNEL_MAPS.get(tm) or {}
                free = [i for lab in borrow for i, l in table.items() if l == lab
                        and i not in taken.get(tm, ()) and bone_for_label(target, l)[0] is None]
                for (port, bone), slot in zip(parts, free):
                    part_ports.setdefault(port, {})[tm] = [slot, bone]
                    chan.setdefault(tm, {})[str(slot)] = bone
        if not part_ports:
            return None
        doc, info2 = c3anim.convert(self.filepath, self.mode, self.templates,
                                    bpy.path.abspath(self.port_map) if self.port_map else None,
                                    self.scale, self.keep_rest, part_ports, self.cars2_names)
        user = bpy.path.abspath(self.channel_map) if self.channel_map else ""
        if user and os.path.isfile(user):
            with open(user) as f:
                extra = json.load(f)
            for tm, tbl in extra.items():
                if isinstance(tbl, dict):
                    chan.setdefault(tm, {}).update({str(k): v for k, v in tbl.items()})
        os.makedirs(folder, exist_ok=True)
        cm = os.path.join(folder, "%s.parts_channels.json" % info2["name"])
        with open(cm, "w") as f:
            json.dump(chan, f, indent=1)
        names = sorted({b for per in part_ports.values() for _s, b in per.values()})
        return doc, info2, cm, names

    def place_in_cutscene(self, context):
        """A cutscene actor clip also moves the car through the set: its placement
        clip (<name>_pn_clip) is found and put on an empty the armature hangs under."""
        target = context.view_layer.objects.active
        if target is None or target.type != "ARMATURE":
            return
        src = bpy.path.abspath(self.filepath)
        try:
            raw, where = find_placement_clip(src, target)
        except Exception as exc:
            raw, where = None, None
            print("Cars 3 cutscene placement not searched: %s" % exc)
        if raw is None:
            if leave_cutscene(target):
                self.report({"INFO"}, "%s is no longer placed in a cutscene (this clip has none)" % target.name)
            return
        try:
            with open(src, "rb") as f:
                clip_raw = f.read()
            actor, cho, own = apply_placement(context, target, raw, float(self.fps), clip_raw)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.report({"WARNING"}, "Cutscene placement not applied: %s" % exc)
            return
        self.report({"INFO"}, "Cutscene %s: %s follows %s's path through the set (%s%s)"
                    % (cho, target.name, actor, os.path.basename(where),
                       " + the clip's own root motion" if own else ""))

    def apply_layered(self, context, base, top_path, info, note):
        """The Cars 2 base clip first, the converted Cars 3 clip on top."""
        if not os.path.isfile(base):
            self.report({"ERROR"}, "Cars 2 base clip not found: %s" % base)
            return {"CANCELLED"}
        target = find_target_armature(context) or context.object
        if target is None or target.type != "ARMATURE":
            self.report({"ERROR"}, "Select the character's armature first")
            return {"CANCELLED"}
        fps = float(self.fps)
        reset_pose(target)
        try:
            base_action, _n, _u = import_clip(context, base, self.channel_map, fps, target, self.bone_space)
            top_action, _n2, unmatched = import_clip(context, top_path, self.channel_map, fps, target,
                                                     self.bone_space)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        replaced, added, kept = layer_actions(base_action, top_action, target)
        # timed by the Cars 3 clip; exported with the base clip's Cars 2 bindings
        base_action["cars2_duration"] = top_action["cars2_duration"]
        base_action["cars2_source"] = base
        try:
            base_action.frame_start, base_action.frame_end = top_action.frame_start, top_action.frame_end
        except AttributeError:
            pass
        base_action.name = "%s_on_%s" % (info["name"], os.path.splitext(os.path.basename(base))[0])
        bpy.data.actions.remove(top_action)
        assign_action(target, base_action)
        context.view_layer.objects.active = target
        if self.set_scene:
            scene = context.scene
            scene.render.fps = int(fps)
            scene.render.fps_base = 1.0
            scene.frame_start = 0
            scene.frame_end = int(round(base_action["cars2_duration"] * fps))
        note += bake_cars3_chains(context, target)
        mismatch = ""
        try:
            bt = clip_templates(base)
            if sorted(bt) != sorted(info["templates"]):
                mismatch = "  WARNING: the base clip binds %s, the Cars 3 clip was converted for %s - " \
                           "pick a base clip of the same kind (or set Mode)" % ("+".join(bt), "+".join(info["templates"]))
        except Exception:
            pass
        self.report({"WARNING"} if mismatch else {"INFO"},
                    "Applied '%s' to '%s': Cars 2 base kept %d curves, Cars 3 replaced %d and added %d%s%s%s"
                    % (base_action.name, target.name, kept, replaced, added,
                       ("  (%d channels have no bone)" % len(unmatched)) if unmatched else "", note, mismatch))
        return {"FINISHED"}


class CARS2_OT_convert_cars3(bpy.types.Operator, ImportHelper, _Cars3ClipOptions):
    """Convert Cars 3 (Driven to Win) clips to Cars 2 clip files (.oct) for the game"""
    bl_idname = "import_scene.cars3_oct_convert"
    bl_label = "Convert Cars 3 Clips to Cars 2"
    bl_options = {"REGISTER"}

    filename_ext = ".oct"
    filter_glob: StringProperty(default="*.oct;*.json", options={"HIDDEN"})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})
    # v2.0.56: Write to / Also write .json removed (user) - the clips always go
    # to a 'cars2_clips' folder next to them, .oct only

    def draw(self, context):
        self.draw_cars3(self.layout)

    def execute(self, context):
        import c3anim
        import cars3_to_cars2 as c32
        names = [f.name for f in self.files if f.name] or [os.path.basename(self.filepath)]
        folder = self.directory or os.path.dirname(self.filepath)
        out = os.path.join(folder, "cars2_clips")
        lines, ok, failed = [], 0, 0
        # v2.0.56: the character's extra Cars 3 parts (Fritter's blade) - from its
        # Cars 3 armature in the scene, stored there by Export Cars 3 as Cars 2
        parts, arm = None, None
        if not self.part_ports:
            arm = armature_for_clips(context, folder)
            parts = stored_part_ports(arm)
            if parts:
                lines.append("own parts of %s: %s" % (arm.name, ", ".join(sorted(
                    {n for per in parts.values() for _s, n in per.values()}))))
        lost = set()
        for nm in names:
            src = os.path.join(folder, nm)
            try:
                doc, info = self.convert(src, parts)
                lost.update(info.get("dropped") or ())
            except Exception as exc:
                failed += 1
                lines.append("SKIP %s: %s" % (nm, exc))
                continue
            stem = os.path.splitext(nm)[0]
            if self.cars2_names:                    # shrd_ -> shared_, trk_ -> truck_ ...
                stem = c3anim.cars2_name(stem)
            try:
                c3anim.write_oct(doc, os.path.join(out, stem + ".oct"))
            except OSError as exc:
                failed += 1
                lines.append("SKIP %s: could not write to %s (%s)" % (nm, out, exc))
                continue
            ok += 1
            lines.append("%-40s %s | %s" % (nm, "+".join(info["templates"]), info["summary"]))
        warn = ""
        if arm is not None and not parts and lost:
            # the clips move parts of this model Cars 2 has no channel for, and it was never exported
            try:
                from . import cars3
                mports = _cars3_model_ports(bpy.path.abspath(arm[cars3.PROP_SOURCE]))
                own = sorted({m.group(1) for p in lost
                              for m in [re.match(r"^Anchor/(.+)_Ctrl/Transform$", mports.get(p, ""))]
                              if m and bone_for_label(arm, m.group(1))[0] is not None})
            except Exception:
                own = []
            if own:
                warn = ("%s's %s not animated: export the character first (Export Cars 3 as Cars 2), "
                        "then convert the clips" % (arm.name, ", ".join(own)))
                lines.insert(0, "WARNING: " + warn)
        text = bpy.data.texts.get("Cars3ClipReport") or bpy.data.texts.new("Cars3ClipReport")
        text.clear()
        text.write("Cars 3 -> Cars 2 clips, written to %s\n\n%s\n\nconverted %d, skipped %d\n"
                   % (out, "\n".join(lines), ok, failed))
        if not ok:
            self.report({"ERROR"}, "No clip converted - see the Cars3ClipReport text")
            return {"CANCELLED"}
        if warn:
            self.report({"WARNING"}, warn)
        self.report({"INFO"}, "Converted %d clip(s) to %s%s - details in the Cars3ClipReport text"
                    % (ok, out, (", %d skipped" % failed) if failed else ""))
        return {"FINISHED"}


class CARS2_OT_export(bpy.types.Operator, ExportHelper):
    """Export the active armature's action as a Cars 2 animation clip"""
    bl_idname = "export_scene.cars2_oct"
    bl_label = "Export Cars 2 Animation"
    bl_options = {"REGISTER"}

    filename_ext = ".oct"
    filter_glob: StringProperty(default="*.oct", options={"HIDDEN"})
    template: StringProperty(
        name="Template clip",
        description="A game clip whose bindings to reuse - normally the clip "
                    "this animation replaces. Only its container and which "
                    "connection templates it binds are kept; everything "
                    "animated is rebuilt from your action. Leave empty to use "
                    "the clip the action was imported from",
        subtype="FILE_PATH")
    frame_range: EnumProperty(
        name="Frame range",
        items=[("ACTION", "Action", "The action's frame range (manual range "
                "if set, otherwise its keyframes)"),
               ("SCENE", "Scene", "The scene's start and end frames")],
        default="ACTION")
    sampling: EnumProperty(
        name="Sample",
        items=[("KEYS", "Keyframes", "Evaluate the action's F-curves directly "
                "(fast; ignores constraints, drivers and NLA)"),
               ("VISUAL", "Visual pose", "Step through the scene and export "
                "the final pose, including IK, constraints and drivers")],
        default="KEYS")
    key_mode: EnumProperty(
        name="Keys",
        items=[("ADAPTIVE", "Fit to curves", "Keep your keyframes and add keys "
                "only where needed to match the motion"),
               ("FRAMES", "Every frame", "One key per frame")],
        default="ADAPTIVE")
    precision: EnumProperty(
        name="Precision",
        items=[("2", "High", "Tighter fit, more keys"),
               ("1", "Normal", ""),
               ("0.25", "Compact", "Looser fit, fewer keys")],
        default="1")
    fps: EnumProperty(name="Frame rate", items=_FPS_ITEMS, default="30")

    def _rig(self, context):
        obj = find_target_armature(context) or context.object
        if obj is None or obj.animation_data is None or obj.animation_data.action is None:
            return None, None
        return obj, obj.animation_data.action

    def _template_path(self, action):
        if self.template:
            return bpy.path.abspath(self.template)
        return action.get("cars2_source", "") if action is not None else ""

    def invoke(self, context, event):
        obj, action = self._rig(context)
        if action is not None:
            src = action.get("cars2_source", "")
            folder = os.path.dirname(src) if src else os.path.dirname(bpy.data.filepath)
            self.filepath = os.path.join(folder or os.path.expanduser("~"),
                                         bpy.path.clean_name(action.name) + ".oct")
            if "cars2_fps" in action:
                self.fps = str(int(action["cars2_fps"])) \
                    if str(int(action["cars2_fps"])) in ("24", "30", "60") else "30"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        obj, action = self._rig(context)
        layout.prop(self, "template")
        box = layout.box()
        if action is None:
            box.label(text="No animated armature selected", icon="ERROR")
        else:
            box.label(text="%s: %s" % (obj.name, action.name), icon="ACTION")
            path = self._template_path(action)
            summary = template_summary(path) if path else None
            if not path:
                box.label(text="Pick a template clip", icon="ERROR")
            elif summary is None:
                box.label(text="Template is not a readable clip", icon="ERROR")
            else:
                maps = [BUILTIN_CHANNEL_MAPS.get(n, {}) for n, _c in summary]
                bones, sliders = action_inventory(action)
                try:
                    recorded = json.loads(action.get("cars2_channels", "{}"))
                except (TypeError, ValueError):
                    recorded = {}
                plans, lost_b, lost_s = plan_targets(obj, maps, bones, sliders, recorded)
                for (name, count), plan in zip(summary, plans):
                    box.label(text="%s: %d of %d channels" % (name, len(plan), count),
                              icon="LINKED")
                for lost in (lost_b, lost_s):
                    if lost:
                        box.label(text="Not in template: %s%s" % (
                            ", ".join(lost[:3]), "..." if len(lost) > 3 else ""),
                            icon="ERROR")
        col = layout.column()
        col.prop(self, "frame_range")
        col.prop(self, "fps")
        col.prop(self, "sampling")
        col.prop(self, "key_mode")
        col.prop(self, "precision")

    def execute(self, context):
        obj, action = self._rig(context)
        if action is None:
            self.report({"ERROR"}, "Select an armature with an action to export")
            return {"CANCELLED"}
        template = self._template_path(action)
        if not template or not os.path.exists(template):
            self.report({"ERROR"}, "Pick a template clip - normally the game clip "
                                   "this animation replaces")
            return {"CANCELLED"}
        if self.frame_range == "SCENE":
            rng = (context.scene.frame_start, context.scene.frame_end)
        else:
            rng = tuple(action.frame_range)
        try:
            rep = export_clip(context, self.filepath, template, obj, action,
                              float(self.fps), rng,
                              visual=self.sampling == "VISUAL",
                              adaptive=self.key_mode == "ADAPTIVE",
                              precision=1.0 / float(self.precision))
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        level = {"INFO"}
        notes = []
        for bone in rep["unplaced_bones"][:4]:
            cover = templates_covering(bone)
            notes.append("'%s' isn't bound by %s%s" % (
                bone, "/".join(rep["templates"]) or "this template",
                (" (it is in %s)" % ", ".join(cover[:3])) if cover else ""))
        if rep["unplaced_bones"] or rep["unplaced_sliders"]:
            level = {"WARNING"}
            if rep["unplaced_sliders"]:
                notes.append("sliders not in template: %s"
                             % ", ".join(rep["unplaced_sliders"][:4]))
        if rep["proxy_missing"]:
            level = {"WARNING"}
            notes.append("mouth sliders are keyed but the armature has no "
                         "relationship data, so mouth bones keep the slider "
                         "shapes baked in")
        if rep["bones"] + rep["sliders"] == 0:
            if rep["unplaced_bones"] or rep["unplaced_sliders"]:
                os.remove(self.filepath)
                self.report({"ERROR"}, "Nothing exported: no keyed bone or slider "
                            "matches %s. %s" % ("/".join(rep["templates"]),
                                                "; ".join(notes)))
                return {"CANCELLED"}
            self.report({"WARNING"}, "The action keys nothing, so the clip is "
                        "empty (%.2fs)" % rep["duration"])
            return {"FINISHED"}
        self.report(level, "Exported %.2fs (%d frames): %d bones, %d sliders - "
                    "%d animated channels in %d keys, %d constants%s"
                    % (rep["duration"], rep["frames"], rep["bones"], rep["sliders"],
                       rep["animated"], rep["keys"], rep["static"],
                       ("  |  " + "; ".join(notes)) if notes else ""))
        return {"FINISHED"}


def _slider_template_items(self, context):
    return [(name, name, "%d sliders" % sum(1 for l in cmap.values()
                                            if is_scalar_label(l)))
            for name, cmap in sorted(BUILTIN_CHANNEL_MAPS.items())
            if any(is_scalar_label(l) for l in cmap.values())]


class CARS2_OT_add_sliders(bpy.types.Operator):
    """Add a connection template's sliders (eyelid shapes, mouth controls,
    visemes) to the armature as keyable properties"""
    bl_idname = "pose.cars2_add_sliders"
    bl_label = "Add Cars 2 Sliders"
    bl_options = {"REGISTER", "UNDO"}

    template: EnumProperty(name="Template", items=_slider_template_items)
    key: BoolProperty(name="Key at current frame",
                      description="Insert a key on each new slider so it is "
                                  "part of the action (and gets exported)",
                      default=False)

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and obj.type == "ARMATURE"

    def execute(self, context):
        obj = context.object
        added = 0
        for label in BUILTIN_CHANNEL_MAPS[self.template].values():
            if not is_scalar_label(label):
                continue
            if label not in obj.keys():
                added += 1
            ensure_scalar(obj, label)
            if self.key:
                obj.keyframe_insert(data_path=scalar_path(label), group="Sliders")
        self.report({"INFO"}, "Added %d sliders to '%s' (Object properties > "
                    "Custom Properties)" % (added, obj.name))
        return {"FINISHED"}


def _is_clip_action(action):
    """An action made by Import Clip / Import Cars 3 Clip (they tag it with its source file)."""
    return action is not None and "cars2_source" in action


def _remove_cutscene_rig(target):
    """Take the armature out of a cutscene placement and delete the placement
    empties (and their actions) that no longer hold anything."""
    actor = target.get(PROP_CUT_PARENT)
    parent = target.parent
    left = leave_cutscene(target)
    removed = 0
    obj = parent
    while obj is not None and obj.type == "EMPTY" and (
            obj.get(PROP_ACTOR_ROOT) == actor or obj.get(PROP_PLACEMENT) == actor or obj.get(PROP_CUTSCENE)):
        if obj.children:
            break
        up = obj.parent
        act = obj.animation_data.action if obj.animation_data else None
        bpy.data.objects.remove(obj)
        if act is not None and act.users == 0:
            bpy.data.actions.remove(act)
        removed += 1
        obj = up
    return left, removed


def rest_pose_all(target):
    """Every pose bone back to its rest (bind) pose - user-added bones too - and every
    slider to the value it shows without a clip (eyelids open)."""
    for bone in target.pose.bones:
        bone.location = (0.0, 0.0, 0.0)
        bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        bone.rotation_euler = (0.0, 0.0, 0.0)
        bone.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
        bone.scale = (1.0, 1.0, 1.0)
    for key in list(target.keys()):
        if is_scalar_label(key):
            try:
                target[key] = scalar_rest(key, target)
            except TypeError:
                pass


class CARS2_OT_remove_clip(bpy.types.Operator):
    """Remove the imported animation from the armature and put the rig back in its rest pose"""
    bl_idname = "pose.cars2_remove_clip"
    bl_label = "Remove Clip"
    bl_options = {"REGISTER", "UNDO"}

    scope: EnumProperty(
        name="Remove",
        items=[("ALL", "All imported clips", "Delete every clip imported with this add-on from the .blend "
                "(and any other action on this armature)"),
               ("ACTIVE", "This clip only", "Delete only the armature's current animation")],
        default="ALL")
    # The bin icon on the panel's clip row removes that clip at once (v1.87);
    # the dialog (all clips / this clip) stays for F3 search and scripts.
    ask: BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        obj = find_target_armature(context)
        return obj is not None and obj.type == "ARMATURE"

    def invoke(self, context, event):
        if not self.ask:
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        target = find_target_armature(context)
        current = target.animation_data.action if target.animation_data else None
        doomed = {current} if current is not None else set()
        if self.scope == "ALL":
            doomed |= {a for a in bpy.data.actions if _is_clip_action(a)}
        if target.animation_data is not None:
            target.animation_data.action = None
            for track in list(target.animation_data.nla_tracks):
                for strip in track.strips:
                    if strip.action is not None:
                        doomed.add(strip.action)
                target.animation_data.nla_tracks.remove(track)
        left_cut, empties = _remove_cutscene_rig(target) if target.get(PROP_CUT_PARENT) else (False, 0)
        # actions still used by another object (e.g. a second car playing the same clip) stay
        users = {o.animation_data.action for o in bpy.data.objects
                 if o is not target and o.animation_data and o.animation_data.action}
        removed, kept = 0, 0
        for action in doomed:
            if action in users:
                kept += 1
                continue
            bpy.data.actions.remove(action)
            removed += 1
        rest_pose_all(target)
        context.view_layer.update()
        msg = "Removed %d animation%s from the file; %s is in its rest pose" % (
            removed, "" if removed == 1 else "s", target.name)
        if kept:
            msg += " (%d still used by another object, kept)" % kept
        if left_cut:
            msg += "; taken out of its cutscene placement (%d empt%s deleted)" % (empties, "y" if empties == 1 else "ies")
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class CARS2_PT_panel(bpy.types.Panel):
    bl_label = "Cars 2 Animation"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cars 2 Character"

    def draw(self, context):
        layout = self.layout
        obj = find_target_armature(context)
        col = layout.column(align=True)
        col.operator(CARS2_OT_import.bl_idname, text="Import Clip", icon="IMPORT")
        col.operator(CARS2_OT_export.bl_idname, text="Export Clip", icon="EXPORT")
        col = layout.column(align=True)
        col.operator(CARS2_OT_import_cars3.bl_idname, text="Import Cars 3 Clip", icon="IMPORT")
        col.operator(CARS2_OT_convert_cars3.bl_idname, text="Convert Cars 3 Clips to Cars 2", icon="FILE_REFRESH")
        if obj is None:
            layout.label(text="No armature in the scene", icon="INFO")
            return
        box = layout.box()
        box.label(text=obj.name, icon="ARMATURE_DATA")
        action = obj.animation_data.action if obj.animation_data else None
        if action is not None:
            row = box.row(align=True)
            row.label(text=action.name, icon="ACTION")
            op = row.operator(CARS2_OT_remove_clip.bl_idname, text="", icon="TRASH")
            op.scope = "ACTIVE"
            op.ask = False
            src = action.get("cars2_source")
            if src:
                box.label(text="Template: %s" % os.path.basename(src))


class CARS2_PT_sliders(bpy.types.Panel):
    """The sliders (eyes, eyelids...) in their own panel that opens and closes"""
    bl_label = "Sliders"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cars 2 Character"
    bl_parent_id = "CARS2_PT_panel"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return find_target_armature(context) is not None

    def draw_header(self, context):
        obj = find_target_armature(context)
        n = sum(1 for k in obj.keys() if is_scalar_label(k)) if obj is not None else 0
        if n:
            self.layout.label(text="(%d)" % n)

    def draw(self, context):
        layout = self.layout
        obj = find_target_armature(context)
        layout.operator_menu_enum(CARS2_OT_add_sliders.bl_idname, "template",
                                  text="Add Sliders", icon="ADD")
        col = layout.column()
        for name in sorted(k for k in obj.keys() if is_scalar_label(k)):
            col.prop(obj, scalar_path(name), text=name.rsplit("/", 1)[-1])


def menu_import(self, context):
    self.layout.operator(CARS2_OT_import.bl_idname, text="Cars 2 Animation (.oct)")
    self.layout.operator(CARS2_OT_import_cars3.bl_idname, text="Cars 3 Animation (.oct)")


def menu_export(self, context):
    self.layout.operator(CARS2_OT_export.bl_idname, text="Cars 2 Animation (.oct)")


CLASSES = (CARS2_OT_import, CARS2_OT_import_cars3, CARS2_OT_convert_cars3, CARS2_OT_export,
           CARS2_OT_add_sliders, CARS2_OT_remove_clip, CARS2_PT_panel, CARS2_PT_sliders)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
