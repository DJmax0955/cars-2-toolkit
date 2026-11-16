"""Cars 2: Arcade character materials (no bpy): effects, map lighting, DDS, material patches.

The character pixel shader (Standard_PS of every a_*_skin.tfx; disassembled from effects.zip with d3dcompiler_47,
whose D3DX9 debug info names every variable - a_1_dtmmsmqdrqcuceismm_skin, a_1_dmmnsmqdrqcuceimm_skin,
a_1_dmmqdrqcuceicmlm_skin, a_1_dmmnqdrqcuceimm_ak_skin, a_1_dmmscqdrqcuceimmlm_skin, a_1_dmmqdrqcucm_skin,
glass_scqdruceic_ab_skin):

  N = normalize(normal)            normal maps: t = tex*2-1, t.y = -t.y, t.xy *= normalHeightA1,
                                                N = normalize(t.x T + t.y B + t.z N)
  ambient  = SH(N)  (objectSH: light probes where the object is; the map's PrimarySceneRig outside them)
  dotSun   = saturate(dot(N, L) (1 - soft) + soft) * shadow
  sunColor = lerp(g_sunLightPrimary, g_sunLightTertiary, saturate((1 - dotSun) g_primaryTertiaryBlend))
             * g_sunLightIntensity
  diffuseLight = max(sunColor dotSun + ambient, 0) (+ samAmbientBuffer: runtime dynamic lights)
  colour   = diffuseMapA1 * diffuseColorA1;  with diffuseColorTertiary ('t' effects):
             colour = lerp(colour * diffuseColorTertiary, colour, |dot(V, N)|)
  specular (effects with specularShineA1): H = normalize(L - V)
             s = pow(saturate(dot(N, H) (1 - soft) + soft), specularShineA1)
             F = specularFresnelA1 (1 - dot(L, H))^5 + (1 - specularFresnelA1)
             spec = max(sunColor s F dotSun shadow, 0) (specularShineA1 + 2) / 8
                    * specularMapA1 (or * specularColorA1)
  combined = colour * diffuseLight + spec  (+ emissiveMapA1 [* its alpha] * emissiveMultiplier, glow effects)
  reflection (effects with environmentFresnelA1): R = reflect(V, N), env = RGBM(texCUBE(-R.x, R.y, R.z))
             = (rgb a 6)^2 * g_EnvMapExposure;  E = environmentFresnelA1 (1 - sat(-dot(V, N)))^5 + 1 - f
             amount = E * environmentReflectivityMapAmountA1 * (1 - environmentReflectivityMapA1)
                   or E * (1 - environmentReflectivityColorA1)
             OUT = lerp(combined, env, amount)
  glass_*:   OUT = env * amount * diffuseLight + spec * specularColorA1, alpha g_FinalAlpha (blended)
  then fog (lerp to texCUBE(fogMap, V) by (dist - g_FogStart) / (g_FogEnd - g_FogStart)); alpha = g_FinalAlpha.
  Post: x = max(c * g_HDR_Exposure / avgLum - 0.004, 0); x(6.2x + .5) / (x(6.2x + 1.7) + .06) (gamma included).

Axes: game -> Blender = AX (Blender = (gx, -gz, gy)), as the Toolkit's import and the World Importer.
"""
import base64, ctypes, difflib, json, os, re, struct, zipfile, zlib
import numpy as np

AX = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=np.float64)       # game -> Blender

# texture usages a character material can carry, in panel order; True = colour map (dwCaps3 0x1, sRGB)
MAP_USAGES = (('diffuseMapA1', True), ('normalMapA1', False), ('specularMapA1', True),
              ('environmentReflectivityMapA1', True), ('emissiveMapA1', True))
# the Wii-style matcap shaders (a_1_*xtc*: env_chromebw_wii / sphericalmap_01; only mcqueenfabulous ships them)
WII_USAGES = (('diffuseMapA1', True), ('environmentMapA1', True), ('specularHighlightMaterialMap', True),
              ('glowMapA1', True))
ALL_MAP_USAGES = tuple(dict(MAP_USAGES + WII_USAGES).items())
ENV_USAGES = ('envMapInstance', 'environmentMap')       # the reflection cube (instance = the map's probe at runtime)
GLOBAL_SAMPLERS = {'fogMap', 'ambientBuffer', 'shadowMask', 'shadowTexture', 'environmentScrollMap'}

# defaults for parameters an effect switch adds (the most common stock values, survey of the 179 game models)
PARAM_DEFAULTS = {
    'MaxAnisotropy': ('Int', 10, 1), 'CullBackFaces': ('Bool', 10, 1), 'g_AlphaModifier': ('Float', 0, 1.0),
    'diffuseColorA1': ('Float', 2, [1.0, 1.0, 1.0]), 'diffuseColorTertiary': ('Float', 2, [0.3, 0.3, 0.3]),
    'scrollU1': ('Float', 0, 0.0), 'scrollV1': ('Float', 0, 0.0),
    'specularShineA1': ('Float', 0, 65.0), 'specularFresnelA1': ('Float', 0, 0.88),
    'specularColorA1': ('Float', 2, [0.5, 0.5, 0.5]),
    'environmentFresnelA1': ('Float', 0, 0.88), 'environmentReflectivityColorA1': ('Float', 2, [0.0, 0.0, 0.0]),
    'environmentReflectivityMapAmountA1': ('Float', 0, 0.4), 'emissiveMultiplier': ('Float', 0, 1.0),
    'normalHeightA1': ('Float', 0, 1.5), 'AlphaRef': ('Float', 0, 0.5),
}
# effect parameters that are not material properties (globals, per-instance)
_NOT_MATERIAL = re.compile(r'^(g_(?!AlphaModifier)|objectSH|environmentScrollRotation|ShadowMap|samp?[A-Z])')


def norm_sampler(usage):
    return 'environmentMap' if usage in ENV_USAGES else usage


# ------------------------------------------------------------------ effects
def find_effects_zip(model_path, override=''):
    """assets/effects/effects.zip above the model (game folder or a package next to it), else override."""
    if override and os.path.isfile(override):
        return override
    p = os.path.abspath(model_path)
    for _ in range(8):
        p = os.path.dirname(p)
        for c in (os.path.join(p, 'effects', 'effects.zip'), os.path.join(p, 'assets', 'effects', 'effects.zip')):
            if os.path.isfile(c):
                return c
        if os.path.dirname(p) == p:
            break
    return ''


def _strings(raw):
    return {x.decode('latin1') for x in raw.split(b'\x00') if 0 < len(x) < 64}


class Effects:
    """effects.zip: which effects exist, the textures each samples and its material parameters."""

    def __init__(self, path):
        self.path = path if path and os.path.isfile(path) else ''
        self.z = zipfile.ZipFile(self.path) if self.path else None
        self.names = {n.lower()[:-4].split('/')[-1]: n for n in self.z.namelist()
                      if n.lower().endswith('.tfx')} if self.z else {}
        self._info = {}

    def exists(self, e):
        return bool(e) and e.lower() in self.names

    def raw(self, e):
        return self.z.read(self.names[e.lower()])

    def info(self, e):
        """-> {'samplers': set of usages (envMapInstance/environmentMap as 'environmentMap'), 'params': set}."""
        k = e.lower()
        if k not in self._info:
            st = _strings(self.raw(e))
            tex = {x[3].lower() + x[4:] for x in st if x.startswith('sam') and len(x) > 4 and x[3].isupper()}
            tex -= GLOBAL_SAMPLERS
            params = {x for x in st if x in PARAM_DEFAULTS or x in KNOWN_PARAMS}
            self._info[k] = {'samplers': {norm_sampler(t) for t in tex}, 'params': params}
        return self._info[k]

    def features(self, e):
        f = features_of(self.info(e)['samplers'], self.info(e)['params'], e)
        f['additive'] = self.dest_blend(e) == 'ONE'
        return f

    def dest_blend(self, e):
        """The effect's DestBlend render state ('ONE' = additive: glass_scqdruceic_ab_skin, 'INVSRCALPHA' = alpha
        blend), read in string-table order (the value follows its name), or None."""
        st = [x.decode('latin1') for x in self.raw(e).split(b'\x00') if 0 < len(x) < 30]
        for i, x in enumerate(st):
            if x == 'DestBlend':
                return next((y for y in st[i + 1:i + 4] if y in ('ONE', 'ZERO', 'INVSRCALPHA', 'SRCALPHA',
                                                               'INVSRCCOLOR', 'SRCCOLOR', 'DESTALPHA')), None)
        return None

    def pick(self, current, want_samplers, want_params=None):
        """The stock effect for a material drawn with `current` that samples exactly want_samplers (usages, normalised)
        - same family (a_1 / glass / ...), same suffixes (_ak, _ab, _skin ...); ties: the most of want_params' features
        (tertiary, specular colour vs map, ...), then the most similar shader token. -> name or None."""
        want = {norm_sampler(s) for s in want_samplers}
        cur = current.lower()
        if self.exists(cur) and self.info(cur)['samplers'] == want and \
                (want_params is None or _feature_key(self.info(cur)['params']) == _feature_key(want_params)):
            return self.names[cur][:-4].split('/')[-1]
        t = cur.split('_')
        if len(t) < 3:
            return None
        fam, suf = t[:2], t[3:]
        best = None
        for c in self.names:
            ct = c.split('_')
            if len(ct) < 3 or ct[:2] != fam or ct[3:] != suf:
                continue
            inf = self.info(c)
            if inf['samplers'] != want:
                continue
            fk = _feature_key(inf['params'])
            score = sum(a == b for a, b in zip(fk, _feature_key(want_params or set())))
            key = (score, difflib.SequenceMatcher(None, t[2], ct[2]).ratio(), -len(c))
            if best is None or key > best[0]:
                best = (key, c)
        return self.names[best[1]][:-4].split('/')[-1] if best else None


KNOWN_PARAMS = set(PARAM_DEFAULTS) | {'emissiveColor', 'slideU1', 'slideV1', 'ZWriteEnable', 'environmentReflectivityA1',
                                      'glowAdditiveOpacity', 'g_AlphaFresnel'}


def _feature_key(params):
    p = set(params or ())
    return ('diffuseColorTertiary' in p, 'specularShineA1' in p, 'specularColorA1' in p,
            'environmentFresnelA1' in p, 'environmentReflectivityMapAmountA1' in p, 'emissiveMultiplier' in p)


def is_wii(usages, params, effect=''):
    """A Wii-style matcap material: environmentMapA1 / specularHighlightMaterialMap / glowMapA1 textures,
    environmentReflectivityA1, or 'xtc' in its shader token (a_1_dmmsmcxtcduemm_skin)."""
    toks = (effect or '').lower().split('_')
    return bool(set(usages) & {'environmentMapA1', 'specularHighlightMaterialMap', 'glowMapA1'}) or         'environmentReflectivityA1' in set(params) or (len(toks) > 2 and 'xtc' in toks[2])


def features_of(samplers, params, effect=''):
    """What the shader does, from the textures it samples and its parameters (or a material's own entries)."""
    s = {norm_sampler(x) for x in samplers}
    p = set(params)
    e = (effect or '').lower()
    toks = e.split('_')
    return {'tertiary': 'diffuseColorTertiary' in p,
            'normal': 'normalMapA1' in s,
            'specular': 'specularShineA1' in p,
            'spec_map': 'specularMapA1' in s,
            'env': 'environmentFresnelA1' in p and 'environmentMap' in s,
            'refl_map': 'environmentReflectivityMapA1' in s,
            'emissive': 'emissiveMapA1' in s,
            'alpha_test': 'ak' in toks[3:] or 'ak' in toks,
            'blend': 'ab' in toks[3:] or e.startswith('glass'),
            'glass': e.startswith('glass'),
            # the stock glass effects without a texture add their reflection (SrcBlend SRCALPHA, DestBlend ONE)
            'additive': e.startswith('glass') and 'diffuseMapA1' not in s}


# emissive x its alpha: stock glow effects differ (a_1_dmmqdrqcuceicmlm_skin multiplies, a_1_dmmscqdrqcuceimmlm_skin
# does not). Read from the shader code when d3dcompiler_47 (Windows) is there, else from the effect name.
def emissive_uses_alpha(fx, effect):
    try:
        ps = sorted((code for kind, code in shaders(fx.raw(effect)) if kind == 'ps'), key=len, reverse=True)
        for code in ps[:1]:                              # Standard_PS = the biggest pixel shader
            txt = disassemble(code)
            if 'samEmissiveMapA1' not in txt:
                continue
            reg = re.search(r'samEmissiveMapA1\s+s(\d+)', txt)
            if not reg:
                continue
            s = reg.group(1)
            lines = [l.split('//')[0].strip() for l in txt.splitlines() if l.strip() and not l.strip().startswith('//')]
            for i, l in enumerate(lines):
                m = re.match(r'texld\S*\s+(r\d+),.*,\s*s%s$' % s, l)
                if m:
                    r = m.group(1)
                    nxt = ' '.join(lines[i + 1:i + 3])
                    return bool(re.search(r'mul\S*\s+%s\.xyz,\s*%s\.w,\s*%s' % (r, r, r), nxt))
    except Exception:
        pass
    return True                 # every stock glow effect checked multiplies by the alpha


def shaders(raw):
    """SM3 shaders in a .tfx: version token, comments (0xFFFE) and instructions stepped over whole to the end token."""
    out = []
    for i in range(0, len(raw) - 8):
        v = raw[i:i + 4]
        if v not in (b'\x00\x03\xff\xff', b'\x00\x03\xfe\xff'):
            continue
        if struct.unpack_from('<I', raw, i + 4)[0] & 0xFFFF != 0xFFFE:
            continue
        j, ok = i + 4, False
        while j + 4 <= len(raw):
            t = struct.unpack_from('<I', raw, j)[0]
            if t == 0x0000FFFF:
                ok, j = True, j + 4
                break
            j += 4 + 4 * (((t >> 16) & 0x7FFF) if t & 0xFFFF == 0xFFFE else ((t >> 24) & 0xF))
        if ok:
            out.append(('ps' if v[2] == 0xFF else 'vs', raw[i:j]))
    return out


_D3D = []


def disassemble(code):
    if not _D3D:
        d = ctypes.WinDLL('d3dcompiler_47.dll')
        d.D3DDisassemble.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_char_p,
                                     ctypes.POINTER(ctypes.c_void_p)]
        _D3D.append(d)
    p = ctypes.c_void_p()
    if _D3D[0].D3DDisassemble(code, len(code), 0, None, ctypes.byref(p)) != 0:
        return ''
    vt = ctypes.cast(ctypes.cast(p, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    ptr = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(vt[3])(p)
    size = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(vt[4])(p)
    text = ctypes.string_at(ptr, size).decode('latin1')
    ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[2])(p)          # Release
    return text


# ------------------------------------------------------------------ DDS
def dds_bytes(v):
    if isinstance(v, str) and v.startswith('base64:'):
        v = v[7:]
        return base64.b64decode(v + '=' * (-len(v) % 4))
    return bytes(v) if isinstance(v, (bytes, bytearray)) else b''


def dds_srgb(dds):
    return len(dds) >= 0x78 and bool(struct.unpack_from('<I', dds, 0x74)[0] & 1)


def _dxt5(data, off, w, h):
    nb = max(1, h // 4) * max(1, w // 4)
    b = np.frombuffer(data, np.uint8, nb * 16, off).reshape(-1, 16)
    a0 = b[:, 0].astype(np.float32); a1 = b[:, 1].astype(np.float32); big = a0 > a1
    ab = np.zeros((nb, 8), np.uint8); ab[:, :6] = b[:, 2:8]
    ai = ((ab.view('<u8').ravel()[:, None] >> (3 * np.arange(16, dtype=np.uint64))) & 7).astype(np.int64)
    pal = np.zeros((nb, 8), np.float32); pal[:, 0] = a0; pal[:, 1] = a1
    for k in range(2, 8):
        pal[:, k] = ((8 - k) * a0 + (k - 1) * a1) / 7
    for k in range(2, 6):
        pal[:, k] = np.where(big, pal[:, k], ((6 - k) * a0 + (k - 1) * a1) / 5)
    pal[:, 6] = np.where(big, pal[:, 6], 0); pal[:, 7] = np.where(big, pal[:, 7], 255)
    A = np.take_along_axis(pal, ai, 1) / 255.0
    C = _dxt_colors(b[:, 8:16], True)
    bw, bh = max(1, w // 4), max(1, h // 4)
    return np.concatenate([C, A[:, :, None]], 2).reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)[:h, :w]


def _dxt_colors(b, four_always):
    c0 = b[:, 0:2].copy().view('<u2').ravel().astype(np.int64)
    c1 = b[:, 2:4].copy().view('<u2').ravel().astype(np.int64)
    rgb = lambda c: np.stack([((c >> 11) & 31) * 255 / 31, ((c >> 5) & 63) * 255 / 63, (c & 31) * 255 / 31], 1)
    p0, p1 = rgb(c0), rgb(c1)
    four = np.ones(len(c0), bool)[:, None] if four_always else (c0 > c1)[:, None]
    p2 = np.where(four, (2 * p0 + p1) / 3, (p0 + p1) / 2); p3 = np.where(four, (p0 + 2 * p1) / 3, 0)
    pal = np.stack([p0, p1, p2, p3], 1) / 255.0
    ci = ((b[:, 4:8].copy().view('<u4').ravel()[:, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3).astype(np.int64)
    return np.take_along_axis(pal, ci[:, :, None].repeat(3, 2), 1)


def _dxt1(data, off, w, h):
    nb = max(1, h // 4) * max(1, w // 4)
    b = np.frombuffer(data, np.uint8, nb * 8, off).reshape(-1, 8)
    c0 = b[:, 0:2].copy().view('<u2').ravel(); c1 = b[:, 2:4].copy().view('<u2').ravel()
    C = _dxt_colors(b, False)
    ci = ((b[:, 4:8].copy().view('<u4').ravel()[:, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3)
    A = np.where((c0 <= c1)[:, None] & (ci == 3), 0.0, 1.0)
    bw, bh = max(1, w // 4), max(1, h // 4)
    return np.concatenate([C, A[:, :, None]], 2).reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)[:h, :w]


def _level_bytes(w, h, fourcc, bpp):
    if fourcc in (b'DXT1', b'DXT5', b'DXT3'):
        return max(1, w // 4) * max(1, h // 4) * (8 if fourcc == b'DXT1' else 16)
    return w * h * bpp // 8


def dds_decode(dds, face=0):
    """Top mip of a DXT1/DXT5/A8R8G8B8 .dds (face of a cube) -> (h, w, 4) float 0..1, rows top first."""
    h, w = struct.unpack_from('<II', dds, 12)
    mips = max(1, struct.unpack_from('<I', dds, 28)[0])
    fourcc = dds[84:88]
    bpp = struct.unpack_from('<I', dds, 88)[0]
    face_bytes = sum(_level_bytes(max(1, w >> i), max(1, h >> i), fourcc, bpp) for i in range(mips))
    off = 128 + face * face_bytes
    if fourcc == b'DXT1':
        return _dxt1(dds, off, w, h)
    if fourcc in (b'DXT5', b'DXT3'):
        return _dxt5(dds, off, w, h)
    masks = struct.unpack_from('<4I', dds, 92)
    px = np.frombuffer(dds, '<u4', w * h, off).reshape(h, w).astype(np.int64)
    out = []
    for m in masks:
        if not m:
            out.append(np.ones((h, w)))
            continue
        sh = (m & -m).bit_length() - 1
        out.append(((px & m) >> sh) / float(m >> sh))
    return np.stack(out, -1)


def is_cube(dds):
    return len(dds) >= 128 and bool(struct.unpack_from('<I', dds, 112)[0] & 0x200)


def _equirect_dirs(width):
    """Blender directions of a width x width/2 equirect image, rows BOTTOM first (Cycles' Environment Texture)."""
    H = width // 2
    u = (np.arange(width) + 0.5) / width; v = (np.arange(H) + 0.5) / H
    psi = (0.5 - u) * 2 * np.pi; th = (v - 0.5) * np.pi
    P, T = np.meshgrid(psi, th)
    return np.stack([np.cos(T) * np.cos(P), np.cos(T) * np.sin(P), np.sin(T)], -1)


def cube_lookup(faces, d):
    """D3D texCUBE: d (N,3) directions -> (N,4)."""
    d = np.asarray(d, np.float64); ax = np.abs(d); n = faces[0].shape[0]
    major = ax.argmax(1); out = np.zeros((len(d), 4))
    x, y, z = d[:, 0], d[:, 1], d[:, 2]
    table = {0: (x > 0, -z, -y, ax[:, 0], 0, 1), 1: (y > 0, x, z, ax[:, 1], 2, 3), 2: (z > 0, x, -y, ax[:, 2], 4, 5)}
    for m, (pos, sc_p, tc_p, ma, fp, fn) in table.items():
        sel = major == m
        for face, sign in ((fp, True), (fn, False)):
            s = sel & (pos if sign else ~pos)
            if not s.any():
                continue
            sc = sc_p[s] if sign else {0: z, 1: x, 2: -x}[m][s]
            tc = tc_p[s] if sign else {0: -y, 1: -z, 2: -y}[m][s]
            u = (sc / ma[s] + 1) / 2; v = (tc / ma[s] + 1) / 2
            out[s] = faces[face][(v * n).astype(int).clip(0, n - 1), (u * n).astype(int).clip(0, n - 1)]
    return out


def reflection_equirect(cube_dds, width=512, exposure=1.0):
    """A reflection cube as the character shader reads it - texCUBE(-R.x, R.y, R.z) of the game reflection vector,
    RGBM-decoded (rgb a 6)^2 x g_EnvMapExposure - laid out as an equirect float image (rows bottom first) to be looked
    up with Blender's reflection vector."""
    faces = [dds_decode(cube_dds, f) for f in range(6)]
    b = _equirect_dirs(width).reshape(-1, 3)
    g = b @ AX                                          # game = AX^T blender
    g[:, 0] = -g[:, 0]                                  # the shader samples (-R.x, R.y, R.z)
    c = cube_lookup(faces, g)
    rgb = (c[:, :3] * c[:, 3:4] * 6.0) ** 2 * float(exposure)
    out = np.concatenate([rgb, np.ones((len(rgb), 1))], 1)
    return out.reshape(width // 2, width, 4).astype(np.float32)


def _dds_header(w, h, mips):
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000
    return struct.pack('<4sIIIIIII44xIIIIIIIIIIIII', b'DDS ', 124, flags, h, w, max(1, w // 4) * max(1, h // 4) * 16,
                       1, mips, 32, 0x4, 0x35545844, 0, 0, 0, 0, 0, 0x401008, 0, 0, 0, 0)


def _to_blocks(img):
    h, w = img.shape[:2]
    return img.reshape(h // 4, 4, w // 4, 4, -1).transpose(0, 2, 1, 3, 4).reshape(-1, 16, img.shape[2])


def _alpha_encode(a):
    lo = a.min(1); hi = a.max(1)
    a0 = np.round(hi).astype(np.uint8); a1 = np.round(lo).astype(np.uint8)
    pal = np.zeros((len(a), 8), np.float32)
    pal[:, 0] = a0; pal[:, 1] = a1
    for k in range(2, 8):
        pal[:, k] = ((8 - k) * pal[:, 0] + (k - 1) * pal[:, 1]) / 7
    idx = np.abs(a[:, :, None] - pal[:, None, :]).argmin(2).astype(np.uint64)
    bits = (idx << (3 * np.arange(16, dtype=np.uint64))).sum(1)
    out = np.zeros((len(a), 8), np.uint8)
    out[:, 0] = a0; out[:, 1] = a1
    out[:, 2:8] = bits[:, None].view(np.uint8).reshape(-1, 8)[:, :6]
    return out


def _565(c):
    c = np.clip(np.round(c), 0, 255).astype(np.int64)
    return ((c[..., 0] >> 3) << 11) | ((c[..., 1] >> 2) << 5) | (c[..., 2] >> 3)


def _from565(v):
    return np.stack([((v >> 11) & 31) * 255 / 31, ((v >> 5) & 63) * 255 / 63, (v & 31) * 255 / 31], -1)


def _color_encode(c):
    """(n,16,3) -> (n,8) DXT colour blocks (endpoints = bounding box along the main axis, 4-colour mode)."""
    mean = c.mean(1, keepdims=True)
    cov = np.einsum('nki,nkj->nij', c - mean, c - mean)
    axis = np.ones((len(c), 3))
    for _ in range(4):
        axis = np.einsum('nij,nj->ni', cov, axis)
        axis /= np.maximum(np.linalg.norm(axis, axis=1, keepdims=True), 1e-9)
    t = np.einsum('nki,ni->nk', c - mean, axis)
    e0 = mean[:, 0] + axis * t.max(1, keepdims=True); e1 = mean[:, 0] + axis * t.min(1, keepdims=True)
    v0, v1 = _565(e0), _565(e1)
    swap = v0 < v1
    v0, v1 = np.where(swap, v1, v0), np.where(swap, v0, v1)
    p0, p1 = _from565(v0), _from565(v1)
    pal = np.stack([p0, p1, (2 * p0 + p1) / 3, (p0 + 2 * p1) / 3], 1)
    idx = ((c[:, :, None, :] - pal[:, None, :, :]) ** 2).sum(3).argmin(2).astype(np.uint32)
    idx = np.where((v0 == v1)[:, None], 0, idx)
    bits = (idx << (2 * np.arange(16, dtype=np.uint32))).sum(1).astype(np.uint32)
    out = np.zeros((len(c), 8), np.uint8)
    out[:, 0:2] = v0.astype('<u2')[:, None].view(np.uint8)
    out[:, 2:4] = v1.astype('<u2')[:, None].view(np.uint8)
    out[:, 4:8] = bits.astype('<u4')[:, None].view(np.uint8)
    return out


def _pow2(n, cap=2048):
    return int(min(cap, max(4, 2 ** round(np.log2(max(n, 1))))))


def _fit_pow2(a, cap=2048):
    h, w = a.shape[:2]; H, Wd = _pow2(h, cap), _pow2(w, cap)
    if (H, Wd) != (h, w):
        ys = ((np.arange(H) + 0.5) * h / H).astype(np.int64); xs = ((np.arange(Wd) + 0.5) * w / Wd).astype(np.int64)
        a = a[ys][:, xs]
    return a


def _mips(a):
    out = [a]
    while out[-1].shape[0] > 1 or out[-1].shape[1] > 1:
        l = out[-1]; hh, ww = l.shape[:2]
        out.append(l.reshape(max(hh // 2, 1), 2 if hh > 1 else 1, max(ww // 2, 1), 2 if ww > 1 else 1, 4).mean((1, 3)))
    return out


def encode_color_dds(rgba, fmt=None, srgb=True, cap=2048):
    """rgba (h,w,4) uint8 top row first -> DDS like the stock ones (pow2, full mips, depth 1); DXT1 when opaque
    (or fmt), else DXT5; srgb = dwCaps3 0x1 (colour maps)."""
    a = np.asarray(rgba)
    if a.shape[2] == 3:
        a = np.concatenate([a, np.full(a.shape[:2] + (1,), 255, np.uint8)], 2)
    a = _fit_pow2(a, cap); H, Wd = a.shape[:2]
    alpha = bool((a[..., 3] < 255).any()) if fmt is None else fmt != 'DXT1'
    levels = _mips(a.astype(np.float32))
    hdr = bytearray(_dds_header(Wd, H, len(levels)))
    if not alpha:
        hdr[84:88] = b'DXT1'; struct.pack_into('<I', hdr, 20, max(1, Wd // 4) * max(1, H // 4) * 8)
    struct.pack_into('<I', hdr, 0x74, 1 if srgb else 0)
    out = [bytes(hdr)]
    for L in levels:
        hh, ww = L.shape[:2]
        if hh < 4 or ww < 4:
            L = np.pad(L, ((0, (-hh) % 4), (0, (-ww) % 4), (0, 0)), mode='edge')
        b = _to_blocks(L)
        cb = _color_encode(b[:, :, :3])
        out.append((np.concatenate([_alpha_encode(b[:, :, 3]), cb], 1) if alpha else cb).tobytes())
    return b''.join(out)


def encode_argb_dds(rgba, srgb=False, cap=2048):
    """Uncompressed A8R8G8B8 with full mips (the stock Cars 2 normal-map layout), depth 1."""
    a = np.asarray(rgba)
    if a.shape[2] == 3:
        a = np.concatenate([a, np.full(a.shape[:2] + (1,), 255, np.uint8)], 2)
    a = _fit_pow2(a, cap); H, Wd = a.shape[:2]
    levels = _mips(a.astype(np.float32))
    hdr = bytearray(struct.pack('<4s7I44x8I5I', b'DDS ', 124, 0x2100F, H, Wd, Wd * 4, 1, len(levels),
                                32, 0x41, 0, 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000, 0x401008, 0, 0, 0, 0))
    struct.pack_into('<I', hdr, 0x74, 1 if srgb else 0)
    return bytes(hdr) + b''.join(np.clip(np.round(l), 0, 255).astype(np.uint8)[..., [2, 1, 0, 3]].tobytes() for l in levels)


def encode_like(rgba, like=None, usage='diffuseMapA1'):
    """An edited texture in its original's format (DXT1/DXT5/uncompressed, sRGB flag, size cap >= its size); a new one:
    normal maps uncompressed linear, colour maps DXT sRGB (dwCaps3 0x1, as dwCaps3_Patch.py sets it)."""
    normal = usage == 'normalMapA1'
    if like is None or len(like) < 128 or like[:4] != b'DDS ':
        return encode_argb_dds(rgba) if normal else encode_color_dds(rgba, srgb=True)
    h, w = struct.unpack_from('<II', like, 12); cap = max(2048, h, w)
    srgb = dds_srgb(like); pf = struct.unpack_from('<I', like, 80)[0]
    if pf & 4:
        has_a = np.asarray(rgba).shape[2] == 4 and (np.asarray(rgba)[..., 3] < 255).any()
        return encode_color_dds(rgba, 'DXT1' if like[84:88] == b'DXT1' and not has_a else 'DXT5', srgb, cap)
    return encode_argb_dds(rgba, srgb, cap)


def crc(dds):
    return int(np.int32(np.uint32(zlib.crc32(dds))))


# ------------------------------------------------------------------ map lighting
def list_worlds(model_path):
    """{name: zip path} of the game's worlds (assets/worlds/*.zip above the model; '_' backups left out)."""
    p = os.path.abspath(model_path)
    for _ in range(8):
        p = os.path.dirname(p)
        d = os.path.join(p, 'worlds') if os.path.basename(p).lower() == 'assets' else os.path.join(p, 'assets', 'worlds')
        if os.path.isdir(d):
            return {f[:-4]: os.path.join(d, f) for f in sorted(os.listdir(d))
                    if f.lower().endswith('.zip') and not f.startswith('_')}
        if os.path.dirname(p) == p:
            break
    return {}


def _world_matrix(nodes, i):
    M = np.identity(4)
    while i is not None and 0 <= i < len(nodes):
        n = nodes[i]
        L = np.array(n.get('LocalToParentMatrix') or np.identity(4).ravel(), np.float64).reshape(4, 4)
        M = M @ L
        ps = n.get('ParentNodeReferences') or []
        i = ps[0] if ps else None
    return M


def world_lighting(world_zip):
    """The runtime light a character gets on this map (its scene_settings, sunLight node, PrimarySceneRig SH and the
    scene reflection probe cube). -> dict (vectors in game axes; 'cube' = DDS bytes or None)."""
    import c2oct
    import tempfile
    name = os.path.splitext(os.path.basename(world_zip))[0]
    with zipfile.ZipFile(world_zip) as z:
        member = next((n for n in z.namelist() if n.lower().split('/')[-1] == name.lower() + '.oct'), None)
        if member is None:
            member = next(n for n in z.namelist() if n.lower().endswith('.oct') and '/' not in n.strip('/'))
        data = z.read(member)
    fd, tmp = tempfile.mkstemp(suffix='.oct')
    try:
        os.write(fd, data); os.close(fd)
        t = c2oct.load(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    plist = lambda pool: list(pool.values()) if isinstance(pool, dict) else list(pool or [])
    props = {}
    for m in plist(t.get('MaterialPool')):
        if m.get('Effect') == 'scene_settings':
            props = {p.get('Name'): p.get('Value') for p in plist(m.get('PropertyEntries'))}
            break
    nodes = plist(t.get('SceneTreeNodePool'))
    sun = None
    for i, n in enumerate(nodes):
        if n.get('Type') == 'Light' and n.get('LightType') == 'Directional':
            if n.get('NodeName') == 'sunLight' or sun is None:
                sun = i
            if n.get('NodeName') == 'sunLight':
                break
    to_sun = np.array([0.3, 0.8, 0.5])
    if sun is not None:
        z_ = _world_matrix(nodes, sun)[2, :3]
        if np.linalg.norm(z_) > 1e-9:
            to_sun = z_ / np.linalg.norm(z_)
    rig = next(iter(plist(t.get('LightRigPool'))), {}).get('LightRigConsts') or []
    rig = [float(x) for x in rig[:27]] if len(rig) >= 27 else [1.0] + [0.0] * 8 + [1.0] + [0.0] * 8 + [1.0] + [0.0] * 8
    texs = plist(t.get('TexturePool'))
    cube = None
    for q in plist(t.get('LightProbePool')):
        if q.get('IsSceneProbe') and q.get('TextureRef') is not None and 0 <= int(q['TextureRef']) < len(texs):
            cube = dds_bytes(texs[int(q['TextureRef'])].get('Data')) or None
            break
    f3 = lambda k, d: [float(x) for x in (props.get(k) or d)[:3]]
    fl = lambda k, d: float(props.get(k, d))
    return {'world': name, 'to_sun': [float(x) for x in to_sun],
            'sun_primary': f3('g_sunLightPrimary', [1, 1, 1]), 'sun_tertiary': f3('g_sunLightTertiary', [1, 1, 1]),
            'sun_blend': fl('g_primaryTertiaryBlend', 0.0), 'sun_intensity': fl('g_sunLightIntensity', 1.0),
            'sun_softness': fl('g_sunLightSoftness', 0.0), 'env_exposure': fl('g_EnvMapExposure', 1.0),
            'hdr_exposure': fl('g_HDR_Exposure', 0.18), 'hdr_min': fl('g_HDR_Min_Luminance', 0.1),
            'hdr_max': fl('g_HDR_Max_Luminance', 0.5), 'rig': rig, 'cube': cube}


# SH: D3DX real SH x convolution / pi, as the World Importer's objectSH (probes.py, objlight.TERMS)
SH_SIGN = np.array([1, -1, 1, -1, 1, -1, 1, -1, 1.0])
SH_Y = np.array([0.282095, 0.488603, 0.488603, 0.488603, 1.092548, 1.092548, 0.315392, 1.092548, 0.546274])
SH_API = np.array([1, 2 / 3, 2 / 3, 2 / 3, .25, .25, .25, .25, .25])
SH_TERMS = SH_SIGN * SH_Y * SH_API


def sh_coefficients(rig):
    """27 LightRigConsts (rgb-major, 9 each) -> 9 RGB coefficients (already x terms) for the polynomials
    [1, y, z, x, xy, yz, 3z^2-1, xz, x^2-y^2] of the normal in game axes."""
    r = np.asarray(rig[:27], np.float64).reshape(3, 9)
    return [[float(r[ch, k] * SH_TERMS[k]) for ch in range(3)] for k in range(9)]


# ------------------------------------------------------------------ writing material changes
def _prop_entry(name, value, like=None):
    if like is not None:
        e = dict(like)
        e['Value'] = value
        return e
    typ, dt, dflt = PARAM_DEFAULTS.get(name, ('Float', 2 if isinstance(value, (list, tuple)) else 0, value))
    return {'Name': name, 'Type': typ, 'DataType': dt, 'Value': value}


def apply_materials(tree, patches, fx=None, car='car'):
    """Write material edits into a character tree (in place).

    patches: [{'index': MaterialPool index, 'props': {name: value}, 'textures': {usage: spec or None},
               'env': True/False/None (None = keep), 'effect': forced effect or None}]
      texture spec: ('keep', texture index) | ('replace', texture index, dds bytes) | ('new', name, dds bytes)
    -> (log lines, errors, {effect name: tfx bytes} the export must write to effects/)."""
    plist = lambda pool: list(pool.values()) if isinstance(pool, dict) else list(pool or [])
    mats = tree['MaterialPool']; texs = tree['TexturePool']
    mkeys = list(mats.keys()); tkeys = list(texs.keys())
    tprefix = tkeys[0].split('#')[0] if tkeys else 'Texture'
    log, errs, tfx = [], [], {}
    added = {}                              # dds crc -> new texture index (one image used by several materials)
    for p in patches:
        mi = p['index']
        if not 0 <= mi < len(mkeys):
            errs.append('material %d not in the file' % mi)
            continue
        m = mats[mkeys[mi]]
        mname = m.get('Name', 'material %d' % mi)
        ents = plist(m.get('TextureEntries'))
        have = {e.get('Usage'): e for e in ents}
        # ---- textures
        want = {}
        for usage, spec in (p.get('textures') or {}).items():
            if spec is None:
                continue
            if spec[0] == 'keep':
                want[usage] = int(spec[1])
            elif spec[0] == 'replace':
                ti = int(spec[1]); t = texs[tkeys[ti]]
                t['Data'] = spec[2]; t['DataCRC'] = crc(spec[2])
                want[usage] = ti
                log.append('%s.%s: texture %s replaced' % (mname, usage, t.get('Name')))
            elif spec[0] == 'new':
                c = crc(spec[2])
                if c in added:
                    want[usage] = added[c]
                    continue
                base = re.sub(r'[^A-Za-z0-9_]+', '_', spec[1]).strip('_').lower() or 'texture'
                used = {t.get('Name') for t in texs.values()}
                nm, k = base, 1
                while nm in used:
                    k += 1; nm = '%s_%d' % (base, k)
                ti = len(texs)
                texs['%s#%d' % (tprefix, ti)] = {'Name': nm, 'DataCRC': c,
                                                 'SourceFilePath': 'characters\\%s\\maps\\%s.png' % (car, nm),
                                                 'Type': '2d', 'Data': spec[2]}
                tkeys.append('%s#%d' % (tprefix, ti))
                added[c] = ti; want[usage] = ti
                log.append('%s.%s: new texture %s' % (mname, usage, nm))
        env_entry = next((have[u] for u in ENV_USAGES if u in have), None)
        env_on = p.get('env')
        if env_on is None:
            env_on = env_entry is not None
        if env_on and env_entry is None:
            # the reflection cube another material of this file uses (stock: envMapInstance -> the errorsky cube,
            # replaced at runtime by the map's probe)
            for mm in mats.values():
                e = next((e for e in plist(mm.get('TextureEntries')) if e.get('Usage') == 'envMapInstance'), None)
                if e is not None:
                    env_entry = {'TextureReference': e['TextureReference'], 'Usage': 'envMapInstance'}
                    break
            if env_entry is None:
                errs.append('%s: reflections need a reflection cube (envMapInstance) - no material of this file '
                            'has one' % mname)
                continue
        # entries edited in place (stock order kept): managed usages updated / dropped, new ones appended
        new_ents = []
        for e in ents:
            u = e.get('Usage')
            if u in dict(ALL_MAP_USAGES):
                if u in want:
                    e2 = dict(e); e2['TextureReference'] = want[u]; new_ents.append(e2)
            elif u in ENV_USAGES:
                if env_on:
                    new_ents.append(dict(e))
            else:                                    # usages the panel doesn't manage (eyeBallTexture, shellMapL1...)
                new_ents.append(dict(e))
        for usage, _c in ALL_MAP_USAGES:
            if usage in want and usage not in have:
                new_ents.append({'TextureReference': want[usage], 'Usage': usage})
        if env_on and not any(e.get('Usage') in ENV_USAGES for e in new_ents):
            new_ents.append(dict(env_entry))
        samplers = {norm_sampler(e['Usage']) for e in new_ents}
        # ---- effect
        old_eff = m.get('Effect', '')
        props_now = {e.get('Name'): e for e in plist(m.get('PropertyEntries'))}
        eff = p.get('effect') or old_eff
        if p.get('effect') and p['effect'] != old_eff and fx is not None and fx.z and not fx.exists(eff):
            errs.append('%s: effect %s is not in effects.zip (the game would not find it)' % (mname, eff))
            continue
        old_samplers = {norm_sampler(e.get('Usage')) for e in ents}
        if not p.get('effect') and samplers != old_samplers:
            if fx is None or not fx.z:
                errs.append('%s: its textures changed (%s -> %s) but effects.zip was not found to pick the effect '
                            'that samples them' % (mname, ', '.join(sorted(old_samplers)), ', '.join(sorted(samplers))))
                continue
            eff = fx.pick(old_eff, samplers, set(props_now))
            if eff is None:
                errs.append('%s: no stock effect like %s samples exactly %s' % (mname, old_eff, ', '.join(sorted(samplers))))
                continue
        if eff != old_eff:
            m['Effect'] = eff
            log.append('%s: effect %s -> %s' % (mname, old_eff, eff))
            if fx is not None and fx.exists(eff):
                tfx[eff] = fx.raw(eff)
        # ---- properties: values updated in place; on an effect switch the settings of the features it gained /
        # lost are added / dropped (a .tfx's strings also hold render-state names like AlphaRef, so its string
        # table is no parameter list)
        gained, lost = set(), set()
        if eff != old_eff and fx is not None and fx.exists(eff):
            new_fp = fx.info(eff)['params'] & FEATURE_PARAMS
            old_fp = (fx.info(old_eff)['params'] if fx.exists(old_eff) else set(props_now)) & FEATURE_PARAMS
            gained, lost = new_fp - set(props_now), old_fp - new_fp
        vals = p.get('props') or {}
        new_props = []
        for name, e in props_now.items():
            if name in lost:
                log.append('%s.%s dropped (%s has no such setting)' % (mname, name, eff))
                continue
            v = vals.get(name, e.get('Value'))
            if not _same(v, e.get('Value')):
                log.append('%s.%s: %s -> %s' % (mname, name, _short(e.get('Value')), _short(v)))
                e = dict(e); e['Value'] = _typed(v, e.get('Value'))
            new_props.append(e)
        for name in sorted(gained):
            v = vals.get(name, PARAM_DEFAULTS[name][2])
            new_props.append(_prop_entry(name, _typed(v, None)))
            log.append('%s.%s added (%s)' % (mname, name, _short(v)))
        if new_props != plist(m.get('PropertyEntries')):
            pk = next(iter((m.get('PropertyEntries') or {}).keys()), 'PropertyEntry#0').split('#')[0]
            m['PropertyEntries'] = {'%s#%d' % (pk, i): e for i, e in enumerate(new_props)}
        if new_ents != ents:
            tk = next(iter((m.get('TextureEntries') or {}).keys()), 'TextureEntry#0').split('#')[0]
            m['TextureEntries'] = {'%s#%d' % (tk, i): e for i, e in enumerate(new_ents)}
    return log, errs, tfx


FEATURE_PARAMS = {'diffuseColorTertiary', 'normalHeightA1', 'specularShineA1', 'specularFresnelA1', 'specularColorA1',
                  'environmentFresnelA1', 'environmentReflectivityMapAmountA1', 'environmentReflectivityColorA1',
                  'emissiveMultiplier'}


def _same(a, b):
    try:
        if isinstance(b, (list, tuple)):
            return len(a) == len(b) and all(abs(float(x) - float(y)) < 1e-6 for x, y in zip(a, b))
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return a == b


def _typed(v, like):
    """The value as the .oct writer needs it: lists as FloatList (or the stored value's own list type)."""
    import c2oct
    if isinstance(v, (list, tuple)):
        cls = type(like) if isinstance(like, list) and type(like) is not list else c2oct.FloatList
        return cls(float(x) for x in v)
    if isinstance(like, int) and not isinstance(like, bool):
        return int(round(float(v)))
    return v


def _short(v):
    if isinstance(v, (list, tuple)):
        return '(' + ', '.join('%.3g' % x for x in v) + ')'
    return '%.4g' % v if isinstance(v, float) else str(v)
