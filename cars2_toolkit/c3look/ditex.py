#!/usr/bin/env python3
r"""
ditex -- A script that converts tbody to dds textures & vice versa for Disney Infinity and Cars 3 Driven To Win (PS4)         

===========================================================================
USAGE :
===========================================================================
    # Disney Infinity
    python ditex.py 2dds <input_folder> <output_folder>
    python ditex.py 2tbody <dds_input_folder> <tbody_output_folder>

    # Cars 3: Driven to Win (PS4)
    python ditex.py c3 2dds <input_folder> <output_folder>
    python ditex.py c3 2tbody <dds_input_folder> <tbody_output_folder>

    # Cars 3: Driven to Win (Xbox One) -- headerless .tbody; the DDS+XBOX
    # header of each texture lives in the owning .mtb (TEXB section). The
    # .mtb is found automatically (searched from the bundle's _root_ / extracted
    # folder), or point at it / a folder of them with --mtb.
    python ditex.py x1 2dds <textures_folder> <output_folder> [--mtb cars3_sally.mtb]
    python ditex.py x1 2tbody <dds_input_folder> <tbody_output_folder> [--mtb ...]

    # purple (tangent-space RGB) normal maps for Blender etc.
    python ditex.py c3 2dds <in> <out> --normals
    python ditex.py x1 2dds <in> <out> --normals

    # singular files also work! yay
    python ditex.py 2dds mytexture.tbody mytexture.dds

Commands:
    2dds               convert .tbody -> .dds
    2tbody             convert .dds -> .tbody

`c3`, if given, must come first and switches to Cars 3: Driven to Win
(PS4/GNF) tiling instead of the default Disney Infinity plain-DDS
passthrough. `x1` (also first) switches to Cars 3: Driven to Win Xbox One:
same GCN 1D_THIN tiling as PS4 (tileMode 13), but no header in the file.

--normals (2dds, c3/x1): Cars 3 normal maps are packed BC3 "DXT5nm" style
(X in alpha, Y in green, red = 1, blue = an unrelated mask) so they look
orange. With --normals every such texture also gets <name>_normal.png:
RGB = (X, Y, rebuilt Z), the usual purple normal map. The .dds itself stays
the game's packed original so it can be converted back.

Options (per-command, see `python ditex.py 2dds -h` / `2tbody -h`):
    --no-recursive     folder mode only: skip subfolders
    --no-srgb-fix      For DI. 2tbody only, skips SRGB fix (if ever needed)                    
    --force            D: copy even if the size, if something fails, (warns instead of skipping)
    -q, --quiet        suppress the per-file info line
"""

import argparse
import math
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

DDS_MAGIC = b"DDS "
GNF_MAGIC = b"GNF "

# ===========================================================================
# Disney Infinity -- plain DDS passthrough + validation + SRGB fix
# ===========================================================================

DDS_HEADER_SIZE = 128          # magic(4) + standard DDS_HEADER(124)
DX10_HEADER_SIZE = 20          # additional, only present if fourCC == 'DX10'
DX10_DXGI_FORMAT_OFFSET = 128  # dxgiFormat is the first field of the DX10 header

# FourCC -> bytes-per-4x4-block for the compressed formats we expect to see.
BLOCK_BYTES = {
    b"DXT1": 8,  b"BC1\x00": 8,  b"BC1": 8,
    b"DXT2": 16, b"DXT3": 16, b"DXT4": 16, b"DXT5": 16,
    b"BC2\x00": 16, b"BC3\x00": 16,
    b"BC4U": 8,  b"BC4S": 8,  b"ATI1": 8,
    b"BC5U": 16, b"BC5S": 16, b"ATI2": 16,
}

# DDSCAPS2 cubemap flags
DDSCAPS2_CUBEMAP = 0x200
CUBEMAP_FACE_BITS = (0x400, 0x800, 0x1000, 0x2000, 0x4000, 0x8000)  # +X-X+Y-Y+Z-Z

DDPF_FOURCC = 0x4
DDPF_RGB = 0x40

# DXGI_FORMAT ids: _SRGB variant -> equivalent linear UNORM variant.
# (Block size / bytes-per-pixel is identical between each pair -- only the
# colorspace tag changes, so this never affects the payload size math.)
DXGI_SRGB_TO_UNORM = {
    29: 28,  # R8G8B8A8_UNORM_SRGB      -> R8G8B8A8_UNORM
    72: 71,  # BC1_UNORM_SRGB           -> BC1_UNORM
    75: 74,  # BC2_UNORM_SRGB           -> BC2_UNORM
    78: 77,  # BC3_UNORM_SRGB           -> BC3_UNORM
    91: 90,  # B8G8R8A8_UNORM_SRGB      -> B8G8R8A8_UNORM
    93: 92,  # B8G8R8X8_UNORM_SRGB      -> B8G8R8X8_UNORM
    99: 98,  # BC7_UNORM_SRGB           -> BC7_UNORM
}
DXGI_FORMAT_NAMES = {
    28: "R8G8B8A8_UNORM", 29: "R8G8B8A8_UNORM_SRGB",
    71: "BC1_UNORM", 72: "BC1_UNORM_SRGB",
    74: "BC2_UNORM", 75: "BC2_UNORM_SRGB",
    77: "BC3_UNORM", 78: "BC3_UNORM_SRGB",
    90: "B8G8R8A8_UNORM", 91: "B8G8R8A8_UNORM_SRGB",
    92: "B8G8R8X8_UNORM", 93: "B8G8R8X8_UNORM_SRGB",
    98: "BC7_UNORM", 99: "BC7_UNORM_SRGB",
}


def face_count(caps2: int) -> int:
    """How many sub-images the payload holds: N (however many face bits
    are set, usually 6) for a cubemap, otherwise 1 for a plain 2D texture."""
    if not (caps2 & DDSCAPS2_CUBEMAP):
        return 1
    return sum(1 for bit in CUBEMAP_FACE_BITS if caps2 & bit) or 1


def mip_chain_bytes_compressed(width: int, height: int, mipcount: int, block_bytes: int) -> int:
    total = 0
    w, h = max(width, 1), max(height, 1)
    for _ in range(max(mipcount, 1)):
        bw = max(1, (w + 3) // 4)
        bh = max(1, (h + 3) // 4)
        total += bw * bh * block_bytes
        if w == 1 and h == 1:
            break
        w, h = max(1, w // 2), max(1, h // 2)
    return total


def mip_chain_bytes_uncompressed(width: int, height: int, mipcount: int, bytes_per_pixel: int) -> int:
    total = 0
    w, h = max(width, 1), max(height, 1)
    for _ in range(max(mipcount, 1)):
        total += w * h * bytes_per_pixel
        if w == 1 and h == 1:
            break
        w, h = max(1, w // 2), max(1, h // 2)
    return total


def parse_dds(data: bytes) -> dict:
    if len(data) < DDS_HEADER_SIZE:
        raise ValueError(f"Too small to be a DDS ({len(data)} bytes)")
    if data[0:4] != DDS_MAGIC:
        raise ValueError(f"Missing 'DDS ' magic (got {data[0:4]!r})")

    hdr = struct.unpack_from("<31I", data, 4)
    dwSize = hdr[0]
    dwHeight, dwWidth = hdr[2], hdr[3]
    dwMipMapCount = hdr[6]
    pf_flags = hdr[19]
    fourcc = struct.pack("<I", hdr[20])
    rgb_bitcount = hdr[21]
    caps2 = hdr[27]

    if dwSize != 124:
        raise ValueError(f"DDS_HEADER.dwSize expected 124, got {dwSize}")

    is_dx10 = bool(pf_flags & DDPF_FOURCC) and fourcc == b"DX10"
    is_cubemap = bool(caps2 & DDSCAPS2_CUBEMAP)
    pixel_data_offset = DDS_HEADER_SIZE + (DX10_HEADER_SIZE if is_dx10 else 0)

    dxgi_format = None
    if is_dx10 and len(data) >= DX10_DXGI_FORMAT_OFFSET + 4:
        dxgi_format = struct.unpack_from("<I", data, DX10_DXGI_FORMAT_OFFSET)[0]

    return dict(
        width=dwWidth, height=dwHeight, mipcount=max(dwMipMapCount, 1),
        pf_flags=pf_flags, fourcc=fourcc, rgb_bitcount=rgb_bitcount,
        is_cubemap=is_cubemap, faces=face_count(caps2),
        is_dx10=is_dx10, dxgi_format=dxgi_format,
        pixel_data_offset=pixel_data_offset,
    )


def apply_srgb_fix(data: bytes):
    info = parse_dds(data)
    if not info["is_dx10"] or info["dxgi_format"] not in DXGI_SRGB_TO_UNORM:
        return data, None
    old_fmt = info["dxgi_format"]
    new_fmt = DXGI_SRGB_TO_UNORM[old_fmt]
    patched = bytearray(data)
    struct.pack_into("<I", patched, DX10_DXGI_FORMAT_OFFSET, new_fmt)
    old_name = DXGI_FORMAT_NAMES.get(old_fmt, str(old_fmt))
    new_name = DXGI_FORMAT_NAMES.get(new_fmt, str(new_fmt))
    return bytes(patched), f"SRGB fix applied: {old_name} -> {new_name}"


def validate_di(data: bytes):
    info = parse_dds(data)
    notes = []
    hard_fail = False
    payload_len = len(data) - info["pixel_data_offset"]
    cube_note = f", cubemap x{info['faces']}" if info["faces"] > 1 else ""

    if info["is_dx10"]:
        notes.append(
            f"DX10 extended header (dxgiFormat={info['dxgi_format']}) -- payload size "
            f"not verified, this script hasn't been confirmed against a real DX10 .tbody.")
    elif info["pf_flags"] & DDPF_FOURCC:
        block_bytes = BLOCK_BYTES.get(info["fourcc"])
        if block_bytes is None:
            notes.append(
                f"fourCC {info['fourcc']!r} is not a recognized compressed format -- "
                f"size not verified, this script hasn't been tested against it.")
        else:
            expected = mip_chain_bytes_compressed(
                info["width"], info["height"], info["mipcount"], block_bytes) * info["faces"]
            if expected != payload_len:
                notes.append(
                    f"pixel data size mismatch: expected {expected} bytes for a "
                    f"{info['width']}x{info['height']} {info['fourcc'].decode(errors='replace')} "
                    f"({info['mipcount']} mips{cube_note}), but found {payload_len} bytes after "
                    f"the header. The file may be truncated, padded, or tiled/swizzled.")
                hard_fail = True
    elif info["pf_flags"] & DDPF_RGB:
        bytes_per_pixel = max(1, info["rgb_bitcount"] // 8)
        expected = mip_chain_bytes_uncompressed(
            info["width"], info["height"], info["mipcount"], bytes_per_pixel) * info["faces"]
        if expected != payload_len:
            notes.append(
                f"pixel data size mismatch: expected {expected} bytes for a "
                f"{info['width']}x{info['height']} uncompressed texture "
                f"({info['mipcount']} mips{cube_note}), but found {payload_len} bytes after "
                f"the header. The file may be truncated, padded, or tiled/swizzled.")
            hard_fail = True
    else:
        notes.append("unrecognized pixel format (neither FourCC nor RGB) -- size not verified.")

    return info, notes, hard_fail


def describe_di(info: dict) -> str:
    cube = f" cubemap x{info['faces']}" if info["faces"] > 1 else ""
    if info["is_dx10"]:
        fmt = DXGI_FORMAT_NAMES.get(info["dxgi_format"], f"DXGI#{info['dxgi_format']}")
    elif info["pf_flags"] & DDPF_FOURCC:
        fmt = info["fourcc"].decode(errors="replace")
    else:
        fmt = "uncompressed"
    return f"{info['width']}x{info['height']} {fmt} mips={info['mipcount']}{cube}"


def convert_one_di(src_path: Path, dst_path: Path, to: str, force: bool, quiet: bool, srgb_fix: bool) -> bool:
    """Returns True on success (file written)."""
    data = src_path.read_bytes()

    srgb_note = None
    if to == "tbody" and srgb_fix:
        try:
            data, srgb_note = apply_srgb_fix(data)
        except ValueError:
            pass  # not a valid DDS at all -- validate_di() below will report it and KILL YOU!!!

    try:
        info, notes, hard_fail = validate_di(data)
    except ValueError as e:
        if force:
            print(f"  [force-copy] {src_path.name} -- {e}")
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            dst_path.write_bytes(data)
            return True
        print(f"  [skip] {src_path.name} -- {e}  (use --force to copy anyway)")
        return False

    if not quiet:
        print(f"  {src_path.name}: {describe_di(info)}")
    if srgb_note:
        print(f"    {srgb_note}")
    for n in notes:
        tag = "WARNING" if hard_fail else "note"
        print(f"    {tag}: {n}")

    if hard_fail and not force:
        print(f"  [skip] {src_path.name} -- failed sanity check  (use --force to copy anyway)")
        return False

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_bytes(data)
    return True


# ===========================================================================
# Cars 3: Driven to Win mode PS4/GNF
# ===========================================================================

def _microtile_swizzle_xy():
    """8x8 PS4 microtile morton-order (x,y) offsets for the 64 local indices."""
    local = np.arange(64)
    x = np.zeros(64, dtype=np.int64)
    y = np.zeros(64, dtype=np.int64)
    for i in range(3):
        x |= ((local >> (2 * i)) & 1) << i
        y |= ((local >> (2 * i + 1)) & 1) << i
    return x, y


def c3_detile_mip(raw: bytes, block_w: int, block_h: int, elem_size: int) -> bytes:
    """Un-swizzle a PS4/GNF tiled mip level into linear row-major block order."""
    tiles_x = (block_w + 7) // 8
    tiles_y = (block_h + 7) // 8
    n_tiles = tiles_x * tiles_y

    raw_arr = np.frombuffer(raw, dtype=np.uint8, count=n_tiles * 64 * elem_size)
    raw_arr = raw_arr.reshape(n_tiles, 64, elem_size)

    x, y = _microtile_swizzle_xy()

    out = np.zeros((block_h, block_w, elem_size), dtype=np.uint8)
    tile_idx = 0
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            tile_data = raw_arr[tile_idx]
            tile_idx += 1
            bx = tx * 8 + x
            by = ty * 8 + y
            valid = (bx < block_w) & (by < block_h)
            out[by[valid], bx[valid]] = tile_data[valid]

    return out.reshape(-1, elem_size).tobytes()


def c3_retile_mip(linear: bytes, block_w: int, block_h: int, elem_size: int) -> bytes:
    """Inverse of c3_detile_mip: linear row-major block data -> PS4-tiled
    (swizzled), microtile-padded bytes."""
    tiles_x = (block_w + 7) // 8
    tiles_y = (block_h + 7) // 8
    n_tiles = tiles_x * tiles_y

    linear_arr = np.frombuffer(linear, dtype=np.uint8, count=block_w * block_h * elem_size)
    linear_arr = linear_arr.reshape(block_h, block_w, elem_size)

    x, y = _microtile_swizzle_xy()

    out = np.zeros((n_tiles, 64, elem_size), dtype=np.uint8)
    tile_idx = 0
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            bx = tx * 8 + x
            by = ty * 8 + y
            valid = (bx < block_w) & (by < block_h)
            out[tile_idx, valid] = linear_arr[by[valid], bx[valid]]
            tile_idx += 1

    return out.tobytes()


def c3_compute_mips(width: int, height: int, elem_size: int, mip_count: int = None):
    """Returns (mip_list, total_bytes) for compressed (4x4-block) formats.
    Each mip's tiled storage is padded up to a full 8x8-block microtile
    (64 * elem_size bytes). Stops after mip_count levels if given, otherwise
    goes all the way down to 1x1."""
    mips = []
    w, h = width, height
    off = 0
    while True:
        bw = max(1, (w + 3) // 4)
        bh = max(1, (h + 3) // 4)
        tiles_x = (bw + 7) // 8
        tiles_y = (bh + 7) // 8
        padded = tiles_x * tiles_y * 64 * elem_size
        mips.append((w, h, bw, bh, off, padded))
        off += padded
        if mip_count is not None and len(mips) >= mip_count:
            break
        if w == 1 and h == 1:
            break
        w, h = max(1, w // 2), max(1, h // 2)
    return mips, off


def c3_compute_mips_raw(width: int, height: int, elem_size: int, mip_count: int = None):
    """Same as c3_compute_mips, but for uncompressed pixel data -- each
    'block' is a single pixel rather than a 4x4 compressed block."""
    mips = []
    w, h = width, height
    off = 0
    while True:
        tiles_x = (w + 7) // 8
        tiles_y = (h + 7) // 8
        padded = tiles_x * tiles_y * 64 * elem_size
        mips.append((w, h, w, h, off, padded))
        off += padded
        if mip_count is not None and len(mips) >= mip_count:
            break
        if w == 1 and h == 1:
            break
        w, h = max(1, w // 2), max(1, h // 2)
    return mips, off


def c3_build_dds_header_uncompressed(width: int, height: int, mipcount: int, pitch: int) -> bytes:
    """DDS header for uncompressed 32-bit BGRA pixel data (no FourCC, uses
    DDPF_RGB with standard ARGB8888 bit masks)."""
    dwFlags = 0x1 | 0x2 | 0x4 | 0x8 | 0x1000 | 0x20000  # CAPS|HEIGHT|WIDTH|PITCH|PIXELFORMAT|MIPMAPCOUNT
    dwCaps = 0x8 | 0x1000 | 0x400000  # COMPLEX|TEXTURE|MIPMAP
    header = struct.pack(
        '<7I44x8I5I',
        124, dwFlags, height, width, pitch, 0, mipcount,
        32, 0x40 | 0x1, 0, 32,
        0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000,
        dwCaps, 0, 0, 0, 0,
    )
    return DDS_MAGIC + header


def c3_build_dds_header(width: int, height: int, mipcount: int, fourcc: bytes, top_level_size: int) -> bytes:
    """Standard 128-byte DDS file header (magic + DDS_HEADER) for a
    BC-compressed, mipmapped 2D texture."""
    dwFlags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000  # CAPS|HEIGHT|WIDTH|PIXELFORMAT|MIPMAPCOUNT|LINEARSIZE
    dwCaps = 0x8 | 0x1000 | 0x400000  # COMPLEX|TEXTURE|MIPMAP
    header = struct.pack(
        '<7I44x8I5I',
        124, dwFlags, height, width, top_level_size, 0, mipcount,
        32, 0x4, struct.unpack('<I', fourcc)[0], 0, 0, 0, 0, 0,
        dwCaps, 0, 0, 0, 0,
    )
    return DDS_MAGIC + header


def c3_reconstruct_gnf_header(width: int, height: int, filesize: int, payload_size: int, fmt: str) -> bytes:
    """Builds a full 256-byte GNF header from scratch. Verified byte-for-byte
    identical to real game files across every sample tested."""
    lastmip = int(math.log2(max(width, height)))
    exp = max(5, lastmip)

    h = bytearray(256)
    h[0:4] = GNF_MAGIC
    struct.pack_into('<I', h, 4, 0xF8)
    h[8] = 2
    h[9] = 1
    h[10] = 8
    h[11] = 0
    struct.pack_into('<I', h, 12, filesize)
    struct.pack_into('<I', h, 16, 0)
    dword5 = 0x26300008 if fmt == 'BC1' else 0x26500008
    struct.pack_into('<I', h, 20, dword5)
    dword6 = 0x70000000 | ((height - 1) << 14) | (width - 1)
    struct.pack_into('<I', h, 24, dword6)
    dword7 = 0x96000fac | ((208 + lastmip) << 16)
    struct.pack_into('<I', h, 28, dword7)
    dword8 = (((1 << exp) - 1) << 13) | 0x2000
    struct.pack_into('<I', h, 32, dword8)
    struct.pack_into('<I', h, 36, 0)
    struct.pack_into('<I', h, 40, 0)
    struct.pack_into('<I', h, 44, payload_size)
    return bytes(h)


def c3_tbody_to_dds(data: bytes):
    """Returns (out_bytes, info_string) or raises ValueError."""
    if data[0:4] != GNF_MAGIC:
        raise ValueError("not a GNF file (bad magic)")

    dw = struct.unpack_from('<12I', data, 0)
    d6 = dw[6]
    width = (d6 & 0x3FFF) + 1
    height = ((d6 >> 14) & 0x3FFF) + 1
    payload = data[256:]

    # last mip level is encoded in dword7's 3rd byte as (208 + lastMipLevel)
    last_mip_level = data[28:32][2] - 208
    mip_count = max(1, last_mip_level + 1)

    mips8, total8 = c3_compute_mips(width, height, 8, mip_count)
    mips16, total16 = c3_compute_mips(width, height, 16, mip_count)
    mips_raw, total_raw = c3_compute_mips_raw(width, height, 4, mip_count)

    if total8 == len(payload):
        elem_size, mips, mode = 8, mips8, 'BC1'
    elif total16 == len(payload):
        elem_size, mips, mode = 16, mips16, 'BC3'
    elif total_raw == len(payload):
        elem_size, mips, mode = 4, mips_raw, 'RAW'
    else:
        raise ValueError(
            f"unrecognized layout (size mismatch: bc1={total8} bc16={total16} "
            f"raw={total_raw} payload={len(payload)})")

    # Note: 'BC3' here is a size-based guess -- BC5/BC7 use the same 16
    # bytes/block and can't be told apart from the header alone (see the
    # CARS 3 section of this file's docstring). Not surfaced per-file since
    # it doesn't affect most conversions; if a texture (especially a normal
    # map) looks wrong after converting, that mismatch is why.
    codec_note = {
        'BC1': "BC1",
        'BC3': "BC3",
        'RAW': "uncompressed BGRA",
    }[mode]

    mip_chunks = [c3_detile_mip(payload[moff:moff + msize], bw, bh, elem_size)
                  for (w, h, bw, bh, moff, msize) in mips]

    out = bytearray()
    if mode == 'RAW':
        pitch = mips[0][2] * elem_size
        out += c3_build_dds_header_uncompressed(width, height, len(mips), pitch)
    else:
        top_level_size = mips[0][2] * mips[0][3] * elem_size
        out += c3_build_dds_header(width, height, len(mips), b'DXT1' if mode == 'BC1' else b'DXT5', top_level_size)
    for chunk in mip_chunks:
        out += chunk

    return bytes(out), f"{width}x{height} {codec_note}"


def c3_dds_to_tbody(data: bytes):
    """Returns (out_bytes, info_string) or raises ValueError."""
    if data[0:4] != DDS_MAGIC:
        raise ValueError("not a DDS file (bad magic)")
    dwHeight = struct.unpack_from('<I', data, 12)[0]
    dwWidth = struct.unpack_from('<I', data, 16)[0]
    dwMipMapCount = struct.unpack_from('<I', data, 28)[0]
    fourcc = data[84:88]
    if fourcc == b'DXT1':
        fmt, elem_size = 'BC1', 8
    elif fourcc == b'DXT5':
        fmt, elem_size = 'BC3', 16
    else:
        raise ValueError(f"not a recognized BC1/DXT1 or BC3/DXT5 DDS file (fourCC {fourcc!r})")

    width, height, mipcount = dwWidth, dwHeight, max(1, dwMipMapCount)
    pixel_data = data[128:]
    mips, _ = c3_compute_mips(width, height, elem_size)

    tiled_chunks = []
    src_off = 0
    for (w, h, bw, bh, _, _) in mips:
        tight_size = bw * bh * elem_size
        chunk = pixel_data[src_off:src_off + tight_size]
        if len(chunk) < tight_size:
            raise ValueError(
                f"DDS pixel data shorter than expected (mip {w}x{h}) -- mip count or "
                f"dimensions may not match a valid chain")
        src_off += tight_size
        tiled_chunks.append(c3_retile_mip(chunk, bw, bh, elem_size))

    payload = b''.join(tiled_chunks)
    filesize = 256 + len(payload)
    header = c3_reconstruct_gnf_header(width, height, filesize, len(payload), fmt)

    return header + payload, f"{width}x{height} {fmt}"


def convert_one_c3(src_path: Path, dst_path: Path, to: str, quiet: bool, normals: bool = False) -> bool:
    """Returns True on success (file written)."""
    data = src_path.read_bytes()
    try:
        if to == "dds":
            out_bytes, info = c3_tbody_to_dds(data)
        else:
            out_bytes, info = c3_dds_to_tbody(data)
    except ValueError as e:
        print(f"  [skip] {src_path.name} -- {e}")
        return False

    if not quiet:
        print(f"  {src_path.name}: {info}")

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_bytes(out_bytes)
    if normals and to == "dds":
        write_normal_png_if_packed(out_bytes, dst_path, quiet)
    return True


# ===========================================================================
# Normal maps -- Cars 3 packed BC3 (X=alpha, Y=green, R=1) -> purple RGB PNG
# ===========================================================================

def _unpack565(c):
    r = ((c >> 11) & 31).astype(np.float32) * (255.0 / 31)
    g = ((c >> 5) & 63).astype(np.float32) * (255.0 / 63)
    b = (c & 31).astype(np.float32) * (255.0 / 31)
    return np.stack([r, g, b], -1)


def decode_bc3(data: bytes, width: int, height: int) -> np.ndarray:
    """Linear (detiled) BC3/DXT5 top mip -> (h, w, 4) uint8 RGBA."""
    bw, bh = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    n = bw * bh
    blk = np.frombuffer(data, np.uint8, count=n * 16).reshape(n, 16)
    shifts16 = np.arange(16, dtype=np.uint64)

    # alpha block
    a0 = blk[:, 0].astype(np.float32)
    a1 = blk[:, 1].astype(np.float32)
    abits = np.zeros(n, np.uint64)
    for i in range(6):
        abits |= blk[:, 2 + i].astype(np.uint64) << np.uint64(8 * i)
    aidx = ((abits[:, None] >> (shifts16 * np.uint64(3))) & np.uint64(7)).astype(np.int64)
    gt = a0 > a1
    pal = np.zeros((n, 8), np.float32)
    pal[:, 0], pal[:, 1] = a0, a1
    for i in range(2, 8):
        p8 = ((8 - i) * a0 + (i - 1) * a1) / 7.0
        if i <= 5:
            p6 = ((6 - i) * a0 + (i - 1) * a1) / 5.0
        else:
            p6 = np.full(n, 0.0 if i == 6 else 255.0, np.float32)
        pal[:, i] = np.where(gt, p8, p6)
    alpha = np.take_along_axis(pal, aidx, 1)

    # colour block (always 4-colour mode in BC3)
    c0 = blk[:, 8].astype(np.uint16) | (blk[:, 9].astype(np.uint16) << 8)
    c1 = blk[:, 10].astype(np.uint16) | (blk[:, 11].astype(np.uint16) << 8)
    p0, p1 = _unpack565(c0), _unpack565(c1)
    cpal = np.stack([p0, p1, (2 * p0 + p1) / 3.0, (p0 + 2 * p1) / 3.0], 1)  # (n,4,3)
    cbits = (blk[:, 12].astype(np.uint64) | (blk[:, 13].astype(np.uint64) << np.uint64(8))
             | (blk[:, 14].astype(np.uint64) << np.uint64(16)) | (blk[:, 15].astype(np.uint64) << np.uint64(24)))
    cidx = ((cbits[:, None] >> (shifts16 * np.uint64(2))) & np.uint64(3)).astype(np.int64)
    rgb = np.take_along_axis(cpal, cidx[:, :, None].repeat(3, 2), 1)  # (n,16,3)

    px = np.concatenate([rgb, alpha[:, :, None]], 2)  # (n,16,4)
    px = px.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
    return np.clip(px[:height, :width] + 0.5, 0, 255).astype(np.uint8)


def write_png_rgb(path: Path, rgb: np.ndarray):
    """Minimal dependency-free 8-bit RGB PNG writer."""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 6))
                     + chunk(b"IEND", b""))


def write_normal_png_if_packed(dds: bytes, dds_path: Path, quiet: bool) -> bool:
    """If `dds` (plain DXT5, as written by this script) is a Cars 3 packed
    normal map (red channel solid 1.0), write <name>_normal.png next to it."""
    if len(dds) < 128 or dds[84:88] != b"DXT5":
        return False
    height, width = struct.unpack_from("<2I", dds, 12)
    rgba = decode_bc3(dds[128:], width, height)
    if rgba[..., 0].min() < 240:  # not the packed-normal layout
        return False
    x = rgba[..., 3].astype(np.float32) / 127.5 - 1.0
    y = rgba[..., 1].astype(np.float32) / 127.5 - 1.0
    z = np.sqrt(np.clip(1.0 - x * x - y * y, 0.0, 1.0))
    rgb = np.clip(np.stack([x, y, z], -1) * 127.5 + 127.5 + 0.5, 0, 255).astype(np.uint8)
    out = dds_path.with_name(dds_path.stem + "_normal.png")
    write_png_rgb(out, rgb)
    if not quiet:
        print(f"    normal map -> {out.name}")
    return True


# ===========================================================================
# Cars 3: Driven to Win -- Xbox One (headerless .tbody, header in the .mtb)
# ===========================================================================

X1_TILE_1D_THIN = 13
X1_FORMATS = {  # dxgiFormat -> (mode, bytes per block/pixel)
    71: ("BC1", 8), 72: ("BC1", 8),
    77: ("BC3", 16), 78: ("BC3", 16),
    87: ("RAW", 4), 91: ("RAW", 4),  # B8G8R8A8 (_SRGB)
}


def x1_read_texb(mtb: bytes) -> dict:
    """.mtb TEXB section -> {hash_hex: 164-byte 'DDS '+DDS_HEADER+DDS_HEADER_XBOX}."""
    i = mtb.find(b"TEXB")
    if i < 0:
        return {}
    count = struct.unpack_from("<I", mtb, i + 12)[0]
    p = i + 24
    entries = []
    for _ in range(count):
        h = mtb[p:p + 8].hex()
        hdr_size = struct.unpack_from("<H", mtb, p + 10)[0]
        entries.append((h, hdr_size))
        p += 16
    out = {}
    for h, hdr_size in entries:
        out[h] = mtb[p:p + hdr_size]
        p += hdr_size
    return out


def x1_collect_headers(paths) -> dict:
    headers = {}
    for root in paths:
        root = Path(root)
        files = [root] if root.is_file() else sorted(root.rglob("*.mtb"))
        for f in files:
            try:
                headers.update(x1_read_texb(f.read_bytes()))
            except (OSError, struct.error):
                pass
    return headers


def x1_default_search_root(inp: Path) -> Path:
    """The bundle's extracted root (_root_ / extracted) above `inp`."""
    start = inp if inp.is_dir() else inp.parent
    for p in [start, *start.parents]:
        if p.name.lower() in ("_root_", "extracted"):
            return p
    return start.parent


def x1_parse_header(hdr: bytes) -> dict:
    if len(hdr) < 164 or hdr[0:4] != DDS_MAGIC or hdr[84:88] != b"XBOX":
        raise ValueError("bad DDS/XBOX header in .mtb")
    height, width = struct.unpack_from("<2I", hdr, 12)
    mips = max(1, struct.unpack_from("<I", hdr, 28)[0])
    dxgi, dim, misc, arr, misc2, tile, align, size, xdk = struct.unpack_from("<9I", hdr, 128)
    if dxgi not in X1_FORMATS:
        raise ValueError(f"unsupported dxgiFormat {dxgi}")
    if tile != X1_TILE_1D_THIN:
        raise ValueError(f"unsupported tileMode {tile} (only 13 / 1D_THIN known)")
    if dim != 3 or arr != 1 or (misc & 0x4):
        raise ValueError("only plain 2D textures supported (no cubemaps/arrays)")
    mode, elem = X1_FORMATS[dxgi]
    return dict(width=width, height=height, mips=mips, dxgi=dxgi,
                mode=mode, elem=elem, size=size)


def _x1_mips(info):
    fn = c3_compute_mips_raw if info["mode"] == "RAW" else c3_compute_mips
    return fn(info["width"], info["height"], info["elem"], info["mips"])


def x1_tbody_to_dds(data: bytes, hdr: bytes):
    info = x1_parse_header(hdr)
    mips, total = _x1_mips(info)
    if total != len(data) or info["size"] != len(data):
        raise ValueError(f"size mismatch (file {len(data)}, .mtb {info['size']}, "
                         f"layout {total})")
    w, h, elem, mode = info["width"], info["height"], info["elem"], info["mode"]
    chunks = [c3_detile_mip(data[o:o + s], bw, bh, elem) for (_, _, bw, bh, o, s) in mips]
    if mode == "RAW":
        head = c3_build_dds_header_uncompressed(w, h, len(mips), w * 4)
    else:
        head = c3_build_dds_header(w, h, len(mips), b"DXT1" if mode == "BC1" else b"DXT5",
                                   mips[0][2] * mips[0][3] * elem)
    names = {"BC1": "BC1", "BC3": "BC3", "RAW": "uncompressed BGRA"}
    return head + b"".join(chunks), f"{w}x{h} {names[mode]} mips={len(mips)}"


def x1_dds_to_tbody(data: bytes, hdr):
    """DXT1/DXT5 DDS -> headerless Xbox One .tbody. If the .mtb header is
    known, the DDS must match it (size/format/mips), since the game reads
    those from the .mtb, not from the .tbody."""
    if data[0:4] != DDS_MAGIC:
        raise ValueError("not a DDS file (bad magic)")
    height, width = struct.unpack_from("<2I", data, 12)
    mipcount = max(1, struct.unpack_from("<I", data, 28)[0])
    fourcc = data[84:88]
    if fourcc == b"DXT1":
        mode, elem = "BC1", 8
    elif fourcc == b"DXT5":
        mode, elem = "BC3", 16
    elif (struct.unpack_from("<I", data, 80)[0] & DDPF_RGB
          and struct.unpack_from("<I", data, 88)[0] == 32
          and struct.unpack_from("<I", data, 92)[0] == 0x00FF0000):
        mode, elem = "RAW", 4  # B8G8R8A8, as written by 2dds
    else:
        raise ValueError(f"only DXT1/DXT5 or 32-bit BGRA DDS supported (fourCC {fourcc!r})")
    if hdr is not None:
        info = x1_parse_header(hdr)
        want = (info["width"], info["height"], info["mips"], info["mode"])
        got = (width, height, mipcount, mode)
        if want != got:
            raise ValueError(f"DDS {width}x{height} {mode} mips={mipcount} does not match the "
                             f".mtb ({info['width']}x{info['height']} {info['mode']} "
                             f"mips={info['mips']}); the .mtb header would need editing too")
    mips, total = _x1_mips(dict(width=width, height=height, elem=elem,
                                mode=mode, mips=mipcount))
    pixel = data[128:]
    out, src = [], 0
    for (w, h, bw, bh, _, _) in mips:
        tight = bw * bh * elem
        chunk = pixel[src:src + tight]
        if len(chunk) < tight:
            raise ValueError(f"DDS pixel data too short at mip {w}x{h}")
        src += tight
        out.append(c3_retile_mip(chunk, bw, bh, elem))
    payload = b"".join(out)
    if hdr is not None and len(payload) != x1_parse_header(hdr)["size"]:
        raise ValueError("tiled size differs from the .mtb dataSize")
    return payload, f"{width}x{height} {mode} mips={mipcount}"


def convert_one_x1(src_path: Path, dst_path: Path, to: str, quiet: bool,
                   headers: dict, normals: bool = False) -> bool:
    data = src_path.read_bytes()
    key = src_path.stem.lower()
    hdr = headers.get(key)
    try:
        if to == "dds":
            if hdr is None:
                raise ValueError("no header for this hash in any .mtb found (use --mtb)")
            out_bytes, info = x1_tbody_to_dds(data, hdr)
        else:
            out_bytes, info = x1_dds_to_tbody(data, hdr)
            if hdr is None:
                info += "  (no .mtb entry found -- not checked against the game header)"
    except ValueError as e:
        print(f"  [skip] {src_path.name} -- {e}")
        return False
    if not quiet:
        print(f"  {src_path.name}: {info}")
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_bytes(out_bytes)
    if normals and to == "dds":
        write_normal_png_if_packed(out_bytes, dst_path, quiet)
    return True


# ===========================================================================
# Shared driver
# ===========================================================================

def find_source_files(root: Path, src_ext: str, recursive: bool):
    pattern = f"**/*{src_ext}" if recursive else f"*{src_ext}"
    return sorted(root.glob(pattern))


def _build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="ditex.py",
        description="Convert between .dds and .tbody (Disney Infinity by default; "
                     "prefix with c3 for Cars 3: Driven to Win).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_dds = sub.add_parser("2dds", help="convert .tbody -> .dds")
    p_dds.add_argument("input", type=Path, help="a .tbody file, or a folder of them")
    p_dds.add_argument("output", type=Path, help="output file path, or output folder")
    p_dds.add_argument("--no-recursive", action="store_true", help="folder mode only: skip subfolders")
    p_dds.add_argument("--force", action="store_true",
                        help="Disney Infinity mode only: copy even if the sanity check fails")
    p_dds.add_argument("--normals", action="store_true",
                        help="c3/x1 only: also write <name>_normal.png (purple RGB normal "
                             "map) for every packed Cars 3 normal map")
    p_dds.add_argument("--mtb", type=Path, action="append",
                        help="x1 only: .mtb file or folder to read headers from (repeatable)")
    p_dds.add_argument("-q", "--quiet", action="store_true")

    p_tbody = sub.add_parser("2tbody", help="convert .dds -> .tbody")
    p_tbody.add_argument("input", type=Path, help="a .dds file, or a folder of them")
    p_tbody.add_argument("output", type=Path, help="output file path, or output folder")
    p_tbody.add_argument("--no-recursive", action="store_true", help="folder mode only: skip subfolders")
    p_tbody.add_argument("--no-srgb-fix", action="store_true",
                          help="Disney Infinity mode only: skip the DX10 SRGB->UNORM patch")
    p_tbody.add_argument("--force", action="store_true",
                          help="Disney Infinity mode only: copy even if the sanity check fails")
    p_tbody.add_argument("--mtb", type=Path, action="append",
                          help="x1 only: .mtb file or folder to check against (repeatable)")
    p_tbody.add_argument("-q", "--quiet", action="store_true")

    return ap


def main():
    argv = sys.argv[1:]
    x1 = bool(argv) and argv[0] == "x1"
    c3 = bool(argv) and argv[0] in ("c3", "x1")
    if c3:
        argv = argv[1:]

    args = _build_arg_parser().parse_args(argv)
    to = "dds" if args.command == "2dds" else "tbody"

    dst_ext = "." + to
    src_ext = ".dds" if to == "tbody" else ".tbody"
    srgb_fix = (not c3) and to == "tbody" and not getattr(args, "no_srgb_fix", False)
    force = getattr(args, "force", False)
    normals = getattr(args, "normals", False)
    if normals and not c3:
        print("note: --normals only applies in c3/x1 mode; ignoring.")

    headers = {}
    if x1:
        roots = args.mtb or [x1_default_search_root(args.input.resolve())]
        headers = x1_collect_headers(roots)
        print(f"x1: {len(headers)} texture header(s) from .mtb files under "
              + ", ".join(str(r) for r in roots))

    def convert(src: Path, dst: Path) -> bool:
        if x1:
            return convert_one_x1(src, dst, to, args.quiet, headers, normals)
        if c3:
            return convert_one_c3(src, dst, to, args.quiet, normals)
        return convert_one_di(src, dst, to, force, args.quiet, srgb_fix)

    if args.input.is_dir():
        files = find_source_files(args.input, src_ext, recursive=not args.no_recursive)
        if not c3:
            # Disney Infinity mode: also pick up files already using the target
            # extension, so a mixed folder can be re-validated/copied in one pass.
            files += find_source_files(args.input, dst_ext, recursive=not args.no_recursive)
        files = sorted(set(files))

        if not files:
            print(f"No {src_ext} files found in {args.input}.")
            sys.exit(0)

        print(f"Found {len(files)} file(s).")
        ok_count = 0
        for f in files:
            rel = f.relative_to(args.input)
            dst = args.output / rel.with_suffix(dst_ext)
            if convert(f, dst):
                ok_count += 1
        print(f"\nDone. {ok_count}/{len(files)} converted.")
        if ok_count < len(files):
            sys.exit(1)

    else:
        dst = args.output
        if dst.suffix == "":
            dst = dst.with_suffix(dst_ext)
        ok = convert(args.input, dst)
        if not ok:
            sys.exit(1)
        print("\nDone -- file passed validation.")


if __name__ == "__main__":
    main()
