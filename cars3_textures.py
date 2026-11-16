"""Textures for a Cars 3 character import.

The Cars 3 material decoder (cars3_location: .mtb records -> texture hashes
-> .tbody -> DDS via ditex, AG normal maps converted to RGB, gloss ->
roughness, emission ...) is the Cars 2 World Importer's; since v2.0.17 the
toolkit carries its own copy (c3look/, with ditex.py), so the World Importer
add-on is no longer needed. The .tbody textures come from a textures/ folder
next to an extracted bundle (<bundle>/textures/<hash>.tbody, the .oct in
<bundle>/characters/<name>/), else from the actor bundle's _root_.zip in the
extracted game folder. Every material of the .oct is built and put on the
imported meshes - keeping each material's NAME, which the Cars 3 -> Cars 2
export pairs parts by.

Images are packed into the .blend; nothing is written to disk.
"""
import importlib
import re
import os
import sys
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import bpy

UV_LAYER = 'UVMap'          # the Cars 3 import's UV layer (the decoder names it map1)
# 'no Cars 3 materials' build: the Cars 3 game shading (c3exact) is not part of this version -
# Cars 3 imports get plain textured materials (+ the eye preview)
DEFAULT_GAME_ROOT = ''   # no built-in path: the game folder is found above the .oct


def _world_importer():
    """-> (the toolkit's own Cars 3 decoder module, the World Importer's
    add-on preferences or None - only used for its Cars 3 game folder)."""
    from .c3look import cars3_location as mod
    prefs = None
    try:
        for name, addon in bpy.context.preferences.addons.items():
            if name.split('.')[-1] == 'cars2_world_importer':
                prefs = addon.preferences
                break
    except Exception:
        pass
    return mod, prefs


def tbody_dirs(filepath):
    """textures/ folders holding .tbody files near the .oct: an extracted
    bundle keeps them at <bundle>/textures, the .oct at
    <bundle>/characters/<name>/<name>.oct (also a textures/ right next to it)."""
    out = []
    p = Path(os.path.abspath(filepath)).parent
    for d in [p] + list(p.parents)[:4]:
        t = d / 'textures'
        try:
            if t.is_dir() and any(f.suffix.lower() == '.tbody' for f in t.iterdir()):
                out.append(str(t))
        except OSError:
            pass
    return out


def game_root(filepath, prefs=None):
    """The extracted game folder (holding assets/ and bundles/) above the .oct,
    else the world importer's Cars 3 game folder, else the default."""
    p = Path(os.path.abspath(filepath)).parent
    for d in [p] + list(p.parents):
        if (d / 'assets').is_dir() and (d / 'bundles').is_dir():
            return d
    for cand in (getattr(prefs, 'cars3_game_root', '') if prefs else '', DEFAULT_GAME_ROOT):
        if cand and (Path(bpy.path.abspath(cand)) / 'assets').is_dir():
            return Path(bpy.path.abspath(cand))
    return None


def find_source(root, filepath, name):
    """-> (bundle zip, ref) for the character: ref is (path, member) of the
    .oct whose .mtb sibling the decoder reads. Prefers the copy inside the
    actor bundle (its textures sit in the same zip)."""
    member = 'characters/%s/%s.oct' % (name, name)
    bundles = root / 'bundles'
    first = bundles / ('actor-' + name) / '_root_.zip'
    order = ([first] if first.is_file() else []) + \
        [p for p in sorted(bundles.glob('*/_root_.zip')) if p != first]
    for z in order:
        try:
            with ZipFile(z) as f:
                names = {n.lower(): n for n in f.namelist()}
        except Exception:
            continue
        if member in names and member[:-4] + '.mtb' in names:
            return z, (str(z), names[member])
    # a loose .oct with its .mtb next to it (an unpacked bundle): no zip needed
    # (v2.0.17) - its textures come from the textures/ folder next to it
    loose = Path(filepath)
    if loose.with_suffix('.mtb').is_file():
        return (first if first.is_file() else None), (str(loose), None)
    return None, None


def _lenient_packet(original):
    """texture_packet that also accepts a 0 sampler byte for a slot with no
    texture. Cars 3 eye shaders (5e3b3bf8 AvaShader): 5 bindings, then 6
    sampler bytes, the last one 0 - the stock check rejects the 0."""
    import struct
    ok = (2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 28, 32, 34, 35, 64, 66)

    def packet(record, count, total, preferred=None):
        got = original(record, count, total, preferred)
        if got or not count:
            return got
        found = []
        for nb in range(1, count + 1):
            for off in range(0, len(record) - nb * 8 - count + 1, 4):
                ids = [struct.unpack_from('<I', record, off + i * 8)[0] for i in range(nb)]
                flags = [struct.unpack_from('<I', record, off + i * 8 + 4)[0] for i in range(nb)]
                sam = list(record[off + nb * 8:off + nb * 8 + count])
                bound = ids + [0xffffffff] * (count - nb)
                if any(i < total for i in ids) and all(i < total or i == 0xffffffff for i in ids)                         and all(f in (0, 0xffffffff) for f in flags)                         and all(v in ok or (v == 0 and b == 0xffffffff) for v, b in zip(sam, bound))                         and any(v in ok for v in sam):
                    found.append((off, ids, sam))
        # shorter runs inside the real packet also pass (one binding + its
        # neighbours' bytes read as samplers): the real packet is the longest
        most = max(len(ids) for _o, ids, _s in found)
        best = [c for c in found if len(c[1]) == most]
        return best[0] if len(best) == 1 else None
    return packet


def _fit_record_sizes(L, imp, src, ref):
    """The decoder slices the .mtb material records by a size worked out from
    each shader (or its learned size table). When the records then do not add
    up to the MATP section and exactly ONE shader has no learned size, that
    shader's records take the difference (Cars 3 McQueen's eye shader
    5e3b3bf8: formula 464, real 368 - like the eye shader 1e70dba2 in the table)."""
    import struct
    try:
        mtb = src.sibling(ref, '.mtb')
        data = src.read(mtb)
        sec = L.mtb_sections(data)
        mp = sec[b'MATP']
        p = mp[0] + 16

        def strings(p, align):
            n = struct.unpack_from('<I', data, p)[0]; p += 4; out = []
            for _ in range(n):
                q = data.index(0, p); out.append(data[p:q].decode()); p = q + 1
            return out, (p + align - 1) & ~(align - 1)
        shaders, p = strings(p, 4)
        _uuids, p = strings(p, 2)
        ids = struct.unpack_from('<' + 'H' * mp[3], data, p)
        p += 2 * mp[3]; p = (p + 3) & ~3; p += 4 * mp[3]; p = (p + 15) & ~15
        total = 0; unknown = {}
        for sid in ids:
            key = shaders[sid]
            shd = imp.shaderzip.read(key + '.shd')
            counts = list(shd[24:28])
            extra = 432 if struct.unpack_from('<I', shd, 76)[0] & 0x1f00000 == 0x1f00000 else 0
            size = counts[0] * 16 + (((counts[3] + 1) * 8 + 15) & ~15) + extra
            if key in imp._sizes:
                size = imp._sizes[key]['sizes'][0]
            else:
                unknown[key] = unknown.get(key, 0) + 1
            size += imp._overrides.get(key, 0)
            total += size
        diff = mp[0] + mp[2] - p - total
        if 0 <= diff < 16 or len(unknown) != 1:
            return
        key, n = next(iter(unknown.items()))
        if diff % n == 0 and (diff // n) % 16 == 0 and abs(diff) < 4096:
            imp._overrides = dict(imp._overrides)
            imp._overrides[key] = imp._overrides.get(key, 0) + diff // n
            print('Cars 3 textures: shader %s records resized by %d bytes' % (key, diff // n))
    except Exception as e:
        print('Cars 3 textures: record sizes not checked: %s' % e)


def _eye_inputs(imp, mat, record):
    """Cars 3 car eyes draw from a runtime render target
    (g_carEyes_renderTarget_texture), composed in game from the material's own
    maps: slot 0 = iris, slot 1 = eyelid colour (flat body paint). The decoder
    leaves both unconnected: show the iris as Base Color, keep the lid colour
    as a labelled node."""
    if not record or not mat.node_tree:
        return
    if 'g_carEyes_renderTarget_texture' not in imp.semantics(record['shader']).get('textures', {}):
        return
    tex = record.get('textures') or []
    nodes = mat.node_tree.nodes
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    for n in [n for n in nodes if n.type == 'TEX_IMAGE']:
        if n.label.endswith('[slot 0]') and len(tex) > 0 and tex[0]:
            im = imp.image(tex[0], False)             # colour, not data
            if im:
                n.image = im
            n.label = 'Iris (eye render target source) [slot 0]'
            if bsdf:
                mat.node_tree.links.new(n.outputs['Color'], bsdf.inputs['Base Color'])
        elif n.label.endswith('[slot 1]') and len(tex) > 1 and tex[1]:
            im = imp.image(tex[1], False)
            if im:
                n.image = im
            n.label = 'Eyelid colour [slot 1]'
    mat['c3_eye_shader'] = 'eyes drawn in game from a render target; iris shown as Base Color'


def _reflections(mat):
    """The decoder plugs the reflectance map (g_specular0_reflectanceTexture,
    grey) into Specular Tint as well as Specular level. A dark tint colours a
    dielectric's reflections black, so most of the car showed none, and
    Roughness stayed at a flat 0.65. Cars 3 draws car reflections from the
    level's cube maps + screen-space reflections, scaled by that map. Here:
    tint white, level = 2 x reflectance, a clear coat where the map is bright
    (paint is authored up to 1.0; rubber / rust stay below ~0.35, so they get
    none) and roughness 0.6 -> 0.25 with reflectance."""
    nt = mat.node_tree
    if not nt:
        return
    nodes, links = nt.nodes, nt.links
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    spec = next((n for n in nodes if n.type == 'TEX_IMAGE' and n.label.startswith('Specular reflectance')), None)
    if bsdf is None or spec is None:
        return
    tint = bsdf.inputs.get('Specular Tint')
    if tint is not None:
        for l in list(tint.links):
            links.remove(l)
        tint.default_value = (1.0, 1.0, 1.0, 1.0)
    src = spec.outputs['Color']
    x, y = spec.location.x + 300, spec.location.y

    def node(kind, label, dy, **kw):
        n = nodes.new(kind); n.label = label; n.location = (x, y + dy)
        for k, v in kw.items():
            setattr(n, k, v)
        return n

    def rng(label, dy, a, b, c, d, interp='LINEAR'):
        n = node('ShaderNodeMapRange', label, dy, clamp=True, interpolation_type=interp)
        n.inputs['From Min'].default_value = a; n.inputs['From Max'].default_value = b
        n.inputs['To Min'].default_value = c; n.inputs['To Max'].default_value = d
        links.new(src, n.inputs['Value'])
        return n.outputs['Result']

    level = bsdf.inputs.get('Specular IOR Level')
    if level is not None:
        links.new(rng('Specular level = 2 x reflectance', 0, 0.0, 0.5, 0.0, 1.0), level)
    links.new(rng('Roughness from reflectance', -180, 0.0, 1.0, 0.6, 0.25), bsdf.inputs['Roughness'])
    coat = bsdf.inputs.get('Coat Weight')
    if coat is not None:
        links.new(rng('Clear coat where reflectance is bright', -360, 0.15, 0.6, 0.0, 1.0, 'SMOOTHSTEP'), coat)
        cr = bsdf.inputs.get('Coat Roughness')
        if cr is not None:
            cr.default_value = 0.04
    mat['c3_reflections'] = 'reflectance map -> specular level, roughness and clear coat (approximates cube map + SSR)'


def apply(context, filepath, name, meshes):
    """Build the Cars 3 materials of the character and put them on its meshes.
    -> (number of materials textured, number of images, message or '')."""
    if filepath.lower().endswith('.json'):
        return 0, 0, 'textures need the .oct (not .oct.json)'
    L, prefs = _world_importer()
    root = game_root(filepath, prefs)
    if root is None:
        return 0, 0, ('Cars 3 game folder not found (the extracted folder with assets/ and bundles/ - its shaders '
                      'tell which texture is which) - no textures')
    bundle, ref = find_source(root, filepath, name)
    if ref is None:
        return 0, 0, 'no %s.mtb next to the .oct and no actor bundle with it under %s - no textures' % (
            name, root / 'bundles')
    ditex = L.DITEX_SCRIPT
    if not os.path.isfile(ditex):
        return 0, 0, 'ditex.py missing from the toolkit (c3look/ditex.py) - no textures'
    L.TBODY_DIRS[:] = tbody_dirs(filepath)

    keys = ('GAME_ROOT', 'DITEX_SCRIPT', 'IMPORT_TEXTURES', 'IMPORT_EXTRA_MAPS', 'CONNECT_DIFFUSE_ALPHA',
            'EXISTING_DDS_DIR', 'LOCATION')
    saved = {k: getattr(L, k) for k in keys}
    saved['texture_packet'] = L.texture_packet
    imp = None
    texts_before = set(bpy.data.texts.keys())
    try:
        L.GAME_ROOT = str(root); L.DITEX_SCRIPT = ditex; L.IMPORT_TEXTURES = True
        L.IMPORT_EXTRA_MAPS = True; L.CONNECT_DIFFUSE_ALPHA = True; L.EXISTING_DDS_DIR = ''; L.LOCATION = name
        report = L.new_report(name, bundle)
        src = L.Source(str(root), str(bundle) if bundle else None, Path(bpy.app.tempdir or '.'), report)
        imp = L.Importer(src, name, Path(bpy.app.tempdir or '.'), report, create_scene=False)
        scene = src.decode(ref)
        L.texture_packet = _lenient_packet(saved['texture_packet'])
        _fit_record_sizes(L, imp, src, ref)
        records = imp.material_records(scene, ref)
        try:                            # v2.0.36: eye materials' texture slots read at their real place
            from . import c3eyes
            c3eyes.fix_texture_slots(L, imp, ref, records)
        except Exception as e:
            print('Cars 3 eyes: texture slots not checked: %s' % e)
        pool = scene.get('MaterialPool', {})
        built = {}                      # Cars 3 material name -> new material
        built_rec = {}                  # v2.0.37: name -> (material, record) for c3exact
        for key, row in pool.items():
            try:
                mid = int(str(key).split('#')[-1])
            except ValueError:
                continue
            mname = row.get('Name')
            if not mname or mname in built:
                continue
            mat = imp.material(scene, mid, records.get(mid), ref)
            _eye_inputs(imp, mat, records.get(mid))
            try:                        # v2.0.36: the eye values, for c3eyes (built once the rig exists)
                from . import c3eyes
                c3eyes.read_material(imp, mat, records.get(mid))
            except Exception as e:
                print('Cars 3 eyes: %s values not read: %s' % (mname, e))
            _reflections(mat)
            for node in mat.node_tree.nodes:   # the decoder samples 'map1'
                if hasattr(node, 'uv_map'):
                    node.uv_map = UV_LAYER
            mat['c3_source_oct'] = '%s|%s' % ref if ref[1] else ref[0]
            built[mname] = mat
            built_rec[mname] = (mat, records.get(mid))
        done = set()
        for obj in meshes:
            for slot in obj.material_slots:
                old = slot.material
                if old is None:
                    continue
                base = old.name if old.name in built else re.sub(r'\.\d{3}$', '', old.name)
                new = built.get(base)
                if new is None or old is new:
                    continue
                slot.material = new
                done.add(base)
        for base in done:                # keep the exact Cars 3 name (the export pairs by it)
            new = built[base]
            old = bpy.data.materials.get(base)
            if old is not None and old is not new and old.users == 0:
                bpy.data.materials.remove(old)
            new.name = base
        for base, mat in built.items():  # built but not on a mesh (other LODs/variants)
            if base not in done and mat.users == 0:
                bpy.data.materials.remove(mat)
        nimg = len({i for i in imp.images.values() if i} | {i for i in imp.scalar_images.values() if i})
        warn = [w for w in report.get('warnings', []) if w.get('kind') in
                ('missing_texture', 'texture_conversion', 'blender_texture_load', 'material_without_bindings',
                 'unresolved_material_packet', 'material_bundle')]
        msg = ('%d texture warnings (see the System Console)' % len(warn)) if warn else ''
        for w in warn:
            print('Cars 3 textures:', w)
        return len(done), nimg, msg
    finally:
        for k, v in saved.items():
            setattr(L, k, v)
        if imp is not None:
            if imp.shaderzip:
                imp.shaderzip.close()
            t = imp.report_text
            if t and t.name not in texts_before:
                bpy.data.texts.remove(t)
        try:
            src.close()
        except Exception:
            pass
