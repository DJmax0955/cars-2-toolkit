"""Cars 2 character geometry: read, verify and rebuild (.oct pools + .vbuf/.ibuf).

Format (verified on every character in the Arcade data):
  VertexBufferPool / IndexBufferPool  one 'Static' buffer each, stored in
      <name>_0.vbuf / <name>_0.ibuf; the rest are empty. Indices are u16.
  VertexStream   a typed slice of the .vbuf: Length vertices of Width bytes
      at VertexBufferOffset, with Elements (Type, Name, Offset); packed
      back to back, no padding.
  IndexStream    Length u16 indices at IndexBufferOffset (TRIANGLES).
  SubGeometryLit one mesh: MaterialReference, VertexStreamReferences (one
      position stream + one normal stream), IndexStreamReference,
      SceneComponentReferences -> a SkinRemap. Several meshes may share
      vertex streams.
  SkinRemap      the mesh's bone palette: vertex BoneIndices are palette
      slots, RemapData[slot] = BoneID. Palettes belong to the MESH (meshes
      sharing a pool can use different palettes). Largest shipped: 42.
  Bone           scene-tree node with BoneID, BoneCount, BoneToModelMatrix
      (row-vector / DirectX layout: translation in [12:15]); its
      BoundingBox (bone-local) and NumAssignedVerts are authoring metadata.
  Weights are 4 normalised bytes summing to exactly 255.

Element types: 2 float3, 1 float2, 3 float3, 24 u8x4, 25 u8x4 (normalised),
44 half2, 45 half4.
"""
import copy
import math
import os
import struct
import uuid

from c2oct import IntList, FloatList

PALETTE_LIMIT = 42
MAX_VERTS = 65535
CODEC = {1: ('<2f', 8), 2: ('<3f', 12), 3: ('<3f', 12), 24: ('<4B', 4), 25: ('<4B', 4),
         44: ('<2e', 4), 45: ('<4e', 8)}
SUBGEOM = ('SubGeometryLit', 'SubGeometry')


def plist(pool):
    return list(pool.values()) if pool else []


# ------------------------------------------------------------------ decode
def buffer_files(oct_path, tree=None):
    """(vbuf path, ibuf path): the files the .oct's buffer pools name
    (FileName), next to it; <stem>_0.vbuf/.ibuf when it names none. Ports
    built from another car keep that car's name (boost -> kabuto_0.vbuf,
    mater -> mater_firerescue_0.vbuf) and the game loads the named file.
    The pool's Size picks between the two when both exist (boost's pool says
    924768 = kabuto_0.vbuf; its stale boost_0.vbuf is 307632); a missing
    named file falls back to <stem>_0."""
    folder = os.path.dirname(oct_path)
    stem = os.path.splitext(oct_path)[0]
    out = []
    for pool, ext in (('VertexBufferPool', '_0.vbuf'), ('IndexBufferPool', '_0.ibuf')):
        entry = next((b for b in plist((tree or {}).get(pool)) if b.get('FileName')), None)
        own = stem + ext
        if entry is None:
            out.append(own)
            continue
        named = os.path.join(folder, os.path.basename(entry['FileName']))
        size = entry.get('Size')
        def fits(p):
            return os.path.isfile(p) and (size is None or os.path.getsize(p) == size)
        if fits(named) or not os.path.isfile(own):
            out.append(named)
        elif fits(own) or not os.path.isfile(named):
            out.append(own)
        else:
            out.append(named)
    return out[0], out[1]


def buffer_sets(oct_path, tree):
    """({pool index: vbuf path}, {pool index: ibuf path}) when a pool names more than one file (streams pick theirs
    by VertexBufferReference / IndexBufferReference - mcqueenfabulous has _0 and _1), else None."""
    folder = os.path.dirname(oct_path)
    out = []
    for pool in ('VertexBufferPool', 'IndexBufferPool'):
        out.append({i: os.path.join(folder, os.path.basename(b['FileName']))
                    for i, b in enumerate(plist((tree or {}).get(pool))) if b.get('FileName')})
    return tuple(out) if len(out[0]) > 1 or len(out[1]) > 1 else None


def _read_set(paths):
    data = {}
    for i, p in paths.items():
        with open(p, 'rb') as f:
            data[i] = f.read()
    return data


def pick(buf, ref):
    """The buffer a stream reads: buf itself, or buf[ref] for a multi-buffer set (dict)."""
    return buf.get(int(ref or 0), b'') if isinstance(buf, dict) else buf


def read_buffers(oct_path, tree=None):
    """(vb, ib): bytes, or {pool index: bytes} for a model with several buffer files."""
    sets = buffer_sets(oct_path, tree) if tree is not None else None
    if sets:
        return _read_set(sets[0]), _read_set(sets[1])
    vp, ip = buffer_files(oct_path, tree)
    with open(vp, 'rb') as f:
        vb = f.read()
    with open(ip, 'rb') as f:
        ib = f.read()
    return vb, ib


def node_streams_valid(n, vs, isp, comps=None):
    """False for a mesh node a hand-edited port switched off by pointing it at
    streams / a scene component the file doesn't have (claudescruggs' Glass
    -> index stream 30 of 4, elmcqueen's Headlights -> 500000, fernando's
    Headlights -> component 7 of 7). The game draws nothing for it."""
    refs = n.get('VertexStreamReferences') or []
    ir = n.get('IndexStreamReference')
    ok = (all(isinstance(r, int) and 0 <= r < len(vs) for r in refs)
          and isinstance(ir, int) and 0 <= ir < len(isp))
    if ok and comps is not None:
        ok = all(isinstance(c, int) and 0 <= c < len(comps) for c in (n.get('SceneComponentReferences') or []))
    return ok


def decode_stream(stream, vb):
    vb = pick(vb, stream.get('VertexBufferReference', 0))
    """-> list of {element name: tuple} per vertex."""
    els = plist(stream['Elements'])
    w, base = stream['Width'] + stream.get('ExtraStride', 0), stream['VertexBufferOffset']
    out = []
    for v in range(stream['Length']):
        o = base + v * w
        out.append({e['Name']: struct.unpack_from(CODEC[e['Type']][0], vb, o + e['Offset']) for e in els})
    return out


def encode_stream(layout, width, verts):
    """layout: [(Type, Name, Offset)], verts: list of {Name: tuple} -> bytes."""
    buf = bytearray(width * len(verts))
    arity = {t: len(struct.unpack(CODEC[t][0], bytes(CODEC[t][1]))) for t, _n, _o in layout}
    for v, vals in enumerate(verts):
        o = v * width
        for t, name, off in layout:
            x = tuple(vals[name])
            if len(x) != arity[t]:                     # e.g. xyz normal into a half4 element
                x = (x + (0.0,) * arity[t])[:arity[t]]
            struct.pack_into(CODEC[t][0], buf, o + off, *x)
    return bytes(buf)


def decode_indices(stream, ib):
    ib = pick(ib, stream.get('IndexBufferReference', 0))
    return list(struct.unpack_from('<%dH' % stream['Length'], ib, stream['IndexBufferOffset']))


def layout_of(stream):
    return tuple((e['Type'], e['Name'], e['Offset']) for e in plist(stream['Elements']))


def roundtrip_check(tree, vb, ib):
    """Decode every stream and re-encode it; True if identical bytes."""
    for s in plist(tree['VertexStreamPool']):
        w = s['Width'] + s.get('ExtraStride', 0)
        b_ = pick(vb, s.get('VertexBufferReference', 0))
        a = b_[s['VertexBufferOffset']:s['VertexBufferOffset'] + w * s['Length']]
        if encode_stream(layout_of(s), w, decode_stream(s, vb)) != a:
            return False
    for s in plist(tree['IndexStreamPool']):
        c_ = pick(ib, s.get('IndexBufferReference', 0))
        a = c_[s['IndexBufferOffset']:s['IndexBufferOffset'] + 2 * s['Length']]
        if struct.pack('<%dH' % s['Length'], *decode_indices(s, ib)) != a:
            return False
    return True


# ------------------------------------------------------- mesh-level model
def bones_by_id(tree):
    return {n['BoneID']: n for n in plist(tree['SceneTreeNodePool']) if n['Type'] == 'Bone'}


def extract_meshes(tree, vb, ib):
    """Every SubGeometry as an independent mesh: its used vertices only,
    bone weights resolved to bone NAMES through its own palette. This is the
    representation rebuild() consumes (and what Blender exports produce)."""
    st = plist(tree['SceneTreeNodePool'])
    vs = plist(tree['VertexStreamPool'])
    isp = plist(tree['IndexStreamPool'])
    comps = plist(tree.get('SceneComponentPool'))
    name_of = {bid: n['NodeName'] for bid, n in bones_by_id(tree).items()}
    meshes = []
    for ni, n in enumerate(st):
        if n['Type'] not in SUBGEOM:
            continue
        if not node_streams_valid(n, vs, isp, comps):      # switched off: dropped on rebuild
            continue
        streams = [vs[k] for k in n['VertexStreamReferences']]
        decoded = [decode_stream(s, vb) for s in streams]
        idx = decode_indices(isp[n['IndexStreamReference']], ib)
        used = sorted(set(idx))
        remap_of = {v: k for k, v in enumerate(used)}
        verts = []
        palette = None
        refs = n.get('SceneComponentReferences') or []
        for c in refs:
            if comps[c]['Type'] == 'SkinRemap':
                palette = comps[c]['RemapData']
        for v in used:
            vals = {}
            for d in decoded:
                vals.update(d[v])
            if palette is not None and 'BoneIndices' in vals:
                bw = []
                for slot, w in zip(vals['BoneIndices'], vals['BlendWeights']):
                    if w:
                        bw.append((name_of[palette[slot]], w))
                vals['_bones'] = bw
            verts.append(vals)
        meshes.append({'node': ni, 'name': n['NodeName'], 'material': n['MaterialReference'],
                       'template': ni, 'verts': verts, 'indices': [remap_of[i] for i in idx]})
    return meshes


# ------------------------------------------------------------- helpers
def quantize_weights(pairs):
    """[(bone, float weight)] -> top 4 as bytes summing to exactly 255."""
    pairs = sorted(((b, w) for b, w in pairs if w > 0), key=lambda p: -p[1])[:4]
    if not pairs:
        raise ValueError('vertex has no bone weights')
    tot = sum(w for _, w in pairs)
    raw = [w / tot * 255.0 for _, w in pairs]
    q = [int(math.floor(x)) for x in raw]
    for k in sorted(range(len(q)), key=lambda k: -(raw[k] - q[k]))[:255 - sum(q)]:
        q[k] += 1
    return [(b, x) for (b, _), x in zip(pairs, q) if x > 0]


def mat_rows(m16):
    return [m16[0:4], m16[4:8], m16[8:12], m16[12:16]]


def to_bone_local(m16, p):
    """Model-space point -> bone-local, row-vector convention (rotation rows
    m[0:3], m[4:7], m[8:11], translation m[12:15])."""
    d = [p[k] - m16[12 + k] for k in range(3)]
    return [sum(d[r] * m16[4 * c + r] for r in range(3)) for c in range(3)]


def aabb(points):
    return [min(p[k] for p in points) for k in range(3)] + [max(p[k] for p in points) for k in range(3)]


# --------------------------------------------------------------- rebuild
def rebuild(tree, meshes, new_bones=(), keep_bone_metadata=True):
    """Replace a character's geometry.

    meshes: list of dicts
        name, material (MaterialPool index), template (index of an existing
        SubGeometry node whose stream layout / RenderCaps / flags to use),
        verts: [{'Position': (x,y,z), 'Normal': (x,y,z[,w]), 'Uv1': (u,v),
                 'Tangent'/'Binormal' (if the layout has them),
                 '_bones': [(bone name, weight)...]}],  weights any scale
        indices: triangle list into verts.
      Meshes with a 'node' key reuse that scene-tree slot; others get new slots.
    new_bones: [{'name', 'parent' (bone name), 'matrix' (16 floats, row-vector)}]
        skin bones to create (their network node must exist: rig edit add_node).
    Returns (new_tree, vbuf_bytes, ibuf_bytes, log).
    """
    t = copy.deepcopy(tree)
    st = plist(t['SceneTreeNodePool'])
    # Shipping files name their pool entries Node#N, VertexStream#N, ... -
    # keep whatever prefix the source uses (the scene tree is 'Node').
    def _prefix(pool, default):
        for k in (t.get(pool) or {}):
            return k.split('#', 1)[0]
        return default
    prefixes = {pool: _prefix(pool, d) for pool, d in (
        ('SceneTreeNodePool', 'Node'), ('VertexStreamPool', 'VertexStream'),
        ('IndexStreamPool', 'IndexStream'), ('SceneComponentPool', 'SceneComponent'))}
    # Only the skeleton root carries BoneCount in shipping files.
    had_bone_count = set(id(n) for n in st if n.get('Type') == 'Bone' and 'BoneCount' in n)
    log = []
    tmpl_nodes = {i: n for i, n in enumerate(st) if n['Type'] in SUBGEOM}
    old_vs = plist(t['VertexStreamPool'])

    # ---- bones
    bones = {n['NodeName']: (i, n) for i, n in enumerate(st) if n['Type'] == 'Bone'}
    if new_bones and not bones:
        raise ValueError('this file has no skeleton to add bones to')
    for nb in new_bones:
        if nb['name'] in bones:
            raise ValueError('bone %s already exists' % nb['name'])
        if nb['parent'] not in bones:
            raise ValueError('new bone %s: parent %s is not a skin bone' % (nb['name'], nb['parent']))
        pi, pn = bones[nb['parent']]
        b = copy.deepcopy(pn)
        b.update({'NodeName': nb['name'], 'Uuid': uuid.uuid4().bytes, 'BoneID': len(bones),
                  'BoneToModelMatrix': FloatList(float(x) for x in nb['matrix']),
                  'PivotPoint': FloatList(float(nb['matrix'][12 + k]) for k in range(3)),
                  'BoundingBox': FloatList([0.0] * 6), 'NumAssignedVerts': 0,
                  'ParentNodeReferences': IntList([pi])})
        if 'BoneEndPoint' in b:
            y = [nb['matrix'][4 + k] for k in range(3)]              # bone's local +Y, 0.1 long
            b['BoneEndPoint'] = FloatList(nb['matrix'][12 + k] + 0.1 * y[k] for k in range(3))
        st.append(b)
        bones[nb['name']] = (len(st) - 1, b)
        log.append('new skin bone %s (BoneID %d) under %s' % (nb['name'], b['BoneID'], nb['parent']))
    roots = [n for _, (_, n) in bones.items() if id(n) in had_bone_count]
    if not roots:                                   # none in the source: the root bone
        roots = [n for _, (pi, n) in bones.items()
                 if not any(p == bi for p in (n.get('ParentNodeReferences') or [])
                            for bi, _x in bones.values())][:1]
    for n in roots:
        n['BoneCount'] = len(bones)
    bone_id = {name: n['BoneID'] for name, (_, n) in bones.items()}

    # ---- mesh nodes must sit at identity (true for every shipping character):
    # vertices are written in model space, as the importer shows them
    ident = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    for m in meshes:
        j, seen = m['template'], set()
        while j is not None and j not in seen:
            seen.add(j)
            ltp = st[j].get('LocalToParentMatrix')
            if ltp and any(abs(a - b) > 1e-6 for a, b in zip(ltp, ident)):
                raise ValueError('mesh node %s (or a parent) is transformed; meshes under transformed '
                                 'nodes are not supported' % tmpl_nodes[m['template']].get('NodeName'))
            p = st[j].get('ParentNodeReferences')
            j = p[0] if p and p[0] != j else None

    # ---- group meshes into vertex pools by layout, <= 65535 verts per pool.
    # Follow the base's own layout: shipping files come both ways - McQueen
    # and Mater share one stream pair per vertex format between mesh nodes,
    # Sheriff and Victor Hugo give every mesh node its own pair (and index
    # stream) in node order. A base that shares keeps sharing; one that
    # doesn't gets one pool per mesh, in scene-node order.
    refs = [tuple(n.get('VertexStreamReferences') or ()) for n in tmpl_nodes.values()]
    base_shares = len(set(refs)) < len(refs)
    order = list(meshes) if base_shares else sorted(
        meshes, key=lambda m: (m['node'] if m.get('node') is not None else 1 << 30, meshes.index(m)))
    pools = []                                     # [{'streams': [(layout,width)], 'verts': [...], 'meshes': [...]}]
    for m in order:
        tn = tmpl_nodes[m.get('layout', m['template'])]        # 'layout': another node's stream layout
        layouts = tuple((layout_of(old_vs[k]), old_vs[k]['Width']) for k in tn['VertexStreamReferences'])
        names = {name for lay, _ in layouts for _, name, _ in lay}
        missing = names - set(m['verts'][0].keys()) - {'BoneIndices', 'BlendWeights'} if m['verts'] else set()
        if missing:
            raise ValueError('mesh %s lacks %s required by its layout' % (m['name'], sorted(missing)))
        if len(m['verts']) > MAX_VERTS:
            raise ValueError('mesh %s has %d vertices (max %d)' % (m['name'], len(m['verts']), MAX_VERTS))
        pool = next((p for p in pools if p['streams'] == layouts
                     and len(p['verts']) + len(m['verts']) <= MAX_VERTS), None) if base_shares else None
        if pool is None:
            pool = {'streams': layouts, 'verts': [], 'meshes': []}
            pools.append(pool)
        m['_base'] = len(pool['verts'])
        pool['verts'].extend(m['verts'])
        pool['meshes'].append(m)

    # ---- palettes per mesh, encoded vertex bytes per pool
    comps = plist(t.get('SceneComponentPool'))
    # Every component keeps its original slot: CarEyes (the eye renderer)
    # must stay exactly where it was (Mater #8, McQueen #1, Guido #2 ...) -
    # tested in-game, moving it breaks the eyes even with the ComponentLink
    # renumbered. The rebuilt bone palettes reuse the original SkinRemap
    # slots in order and only append past the end when they run out; unused
    # SkinRemap slots keep their original contents, so nothing shifts.
    new_comps = list(comps)
    comp_index = {id(c): i for i, c in enumerate(comps) if c['Type'] != 'SkinRemap'}
    free_skin_slots = [i for i, c in enumerate(comps) if c['Type'] == 'SkinRemap']
    palettes = {}
    stats = {name: [] for name in bones}
    vbuf, ibuf = bytearray(), bytearray()
    new_vs, new_is = [], []
    for pool in pools:
        # palette per mesh; shared vertices must agree, so encode per mesh
        # into the pool order (each vertex belongs to exactly one mesh here)
        enc_verts = []
        for m in pool['meshes']:
            skinned = any(name == 'BoneIndices' for lay, _ in pool['streams'] for _, name, _ in lay)
            names = {b for v in m['verts'] for b, _w in v.get('_bones', [])}
            unknown = sorted(b for b in names if b not in bone_id)
            if unknown:
                raise ValueError('mesh %s is weighted to bones that are not skin bones: %s' % (m['name'], unknown))
            names = sorted(names, key=lambda b: bone_id[b])
            if len(names) > PALETTE_LIMIT:
                raise ValueError('mesh %s uses %d bones; the game limit is %d per mesh - split it by material'
                                 % (m['name'], len(names), PALETTE_LIMIT))
            pal = [bone_id[b] for b in names]
            key = tuple(pal)
            if key not in palettes:
                comp = {'Type': 'SkinRemap', 'RemapData': IntList(pal)}
                if free_skin_slots:
                    k = free_skin_slots.pop(0)
                    new_comps[k] = comp
                else:
                    new_comps.append(comp)
                    k = len(new_comps) - 1
                palettes[key] = k
            m['_palette'] = palettes[key]
            slot = {b: k for k, b in enumerate(names)}
            for v in m['verts']:
                vals = dict(v)
                if skinned:
                    q = quantize_weights(v.get('_bones', []))
                    bi = [slot[b] for b, _ in q] + [0] * (4 - len(q))
                    bw = [w for _, w in q] + [0] * (4 - len(q))
                    vals['BoneIndices'], vals['BlendWeights'] = tuple(bi), tuple(bw)
                    stats[q[0][0]].append(v['Position'])
                enc_verts.append(vals)
        pool['_stream_ids'] = []
        for lay, width in pool['streams']:
            data = encode_stream(lay, width, enc_verts)
            s = copy.deepcopy(next(x for x in old_vs if layout_of(x) == lay and x['Width'] == width))
            s['VertexBufferReference'] = 0              # everything goes into the one rebuilt buffer
            s['Length'], s['VertexBufferOffset'] = len(enc_verts), len(vbuf)
            vbuf += data
            new_vs.append(s)
            pool['_stream_ids'].append(len(new_vs) - 1)
        for m in pool['meshes']:
            idx = [i + m['_base'] for i in m['indices']]
            s = copy.deepcopy(plist(t['IndexStreamPool'])[0])
            s['IndexBufferReference'] = 0
            s['Length'], s['IndexBufferOffset'] = len(idx), len(ibuf)
            ibuf += struct.pack('<%dH' % len(idx), *idx)
            new_is.append(s)
            m['_istream'] = len(new_is) - 1

    # ---- scene tree: reuse slots, append new meshes, drop unused slots last
    used_slots = set()
    for m in meshes:
        tn = copy.deepcopy(tmpl_nodes[m['template']])
        tn['NodeName'] = m['name']
        tn['MaterialReference'] = m['material']
        pool = next(p for p in pools if m in p['meshes'])
        tn['VertexStreamReferences'] = IntList(pool['_stream_ids'])
        tn['IndexStreamReference'] = m['_istream']
        other = [c for c in (tn.get('SceneComponentReferences') or [])
                 if comps[c]['Type'] != 'SkinRemap']
        tn['SceneComponentReferences'] = IntList([comp_index[id(comps[c])] for c in other] + [m['_palette']])
        pts = [v['Position'] for v in m['verts']]
        bb = aabb(pts)
        tn['BoundingCenter'] = FloatList((bb[k] + bb[k + 3]) / 2 for k in range(3))
        if m.get('node') is not None and m['node'] not in used_slots:
            st[m['node']] = tn
            used_slots.add(m['node'])
        else:
            st.append(tn)
            used_slots.add(len(st) - 1)
    dropped = [i for i in tmpl_nodes if i not in used_slots]
    if dropped:
        st = _remove_nodes(t, st, dropped)
        log.append('removed %d mesh node(s) no longer present' % len(dropped))

    # ---- bounds and bone metadata
    allpts = [v['Position'] for m in meshes for v in m['verts']]
    for n in st:
        if n['Type'] == 'Geometry' and allpts:
            bb = aabb(allpts)
            # Shipped bounds are generous (they cover the body moving with
            # the rig, e.g. Sheriff's box reaches 0.38 below the ground and
            # above the roof); the game culls with them, so grow, never shrink.
            old = list(n.get('BoundingBox') or [])
            if keep_bone_metadata and len(old) == 6 and any(old):
                bb = [min(a, b) for a, b in zip(bb[:3], old[:3])] + \
                     [max(a, b) for a, b in zip(bb[3:], old[3:])]
                if bb == old:
                    continue                    # unchanged: keep every shipped value
                allpts = allpts + [bb[:3], bb[3:]]
            c = [(bb[k] + bb[k + 3]) / 2 for k in range(3)]
            n['BoundingBox'] = FloatList(bb)
            n['BoundingOBBCenter'] = FloatList(c)
            n['BoundingOBBExtents'] = FloatList((bb[k + 3] - bb[k]) / 2 for k in range(3))
            n['BoundingOBBOrientation'] = FloatList([1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0])
            n['BoundingSphereCenter'] = FloatList(c)
            n['BoundingSphereRadius'] = max(math.dist(c, p) for p in allpts)
    for name, (_, n) in bones.items():
        pts = stats.get(name, [])
        if not pts:
            continue
        local = aabb([to_bone_local(n['BoneToModelMatrix'], p) for p in pts])
        old = list(n['BoundingBox'])
        if keep_bone_metadata and any(old):             # grow, never shrink, shipped metadata
            local = [min(a, b) for a, b in zip(local[:3], old[:3])] + [max(a, b) for a, b in zip(local[3:], old[3:])]
            n['NumAssignedVerts'] = max(n['NumAssignedVerts'], len(pts))
        else:
            n['NumAssignedVerts'] = len(pts)
        n['BoundingBox'] = FloatList(local)

    # ---- pools and buffers
    t['SceneTreeNodePool'] = {'%s#%d' % (prefixes['SceneTreeNodePool'], i): n for i, n in enumerate(st)}
    t['VertexStreamPool'] = {'%s#%d' % (prefixes['VertexStreamPool'], i): s for i, s in enumerate(new_vs)}
    t['IndexStreamPool'] = {'%s#%d' % (prefixes['IndexStreamPool'], i): s for i, s in enumerate(new_is)}
    t['SceneComponentPool'] = {'%s#%d' % (prefixes['SceneComponentPool'], i): c
                               for i, c in enumerate(new_comps)}
    # Associations point at components by number too - above all the
    # ComponentLink that attaches the CarEyes component (the eye renderer):
    # left pointing at the old number, the eyes render black. Renumber them;
    # a reference to a component that no longer exists is an error.
    new_index = {id(c): i for i, c in enumerate(new_comps)}
    for a in plist(t.get('AssociationPool')):
        if 'ComponentRef' in a:
            old = a['ComponentRef']
            if not (0 <= old < len(comps)) or id(comps[old]) not in new_index:
                raise ValueError('association %s points at scene component %d, which the '
                                 'rebuilt pool no longer has' % (a.get('NodeName'), old))
            a['ComponentRef'] = new_index[id(comps[old])]
    for pool in ('VertexBufferPool', 'IndexBufferPool'):      # one buffer file each from here on
        named = [b for b in plist(t[pool]) if b.get('FileName')]
        for b in named[1:]:
            del b['FileName']; b['Size'] = 0
    for b in plist(t['VertexBufferPool']):
        if b.get('FileName'):
            b['Size'] = len(vbuf)
    for b in plist(t['IndexBufferPool']):
        if b.get('FileName'):
            b['Size'] = len(ibuf)

    # ---- associations for new bones (by name: 'Anchor|X' <-> Anchor/X/Transform)
    assoc = t.get('AssociationPool')
    if new_bones and assoc is not None:
        tmpl = next(a for a in assoc.values() if a.get('Type') == 'Bone')
        for nb in new_bones:
            a = {k: v for k, v in tmpl.items() if k != 'LazyDataNodeName'}
            root = tmpl['NodeName'].split('|', 1)[0]
            a['NodeName'] = '%s|%s' % (root, nb['name'])
            a['SceneTreeNodeRef'] = next(i for i, n in enumerate(st) if n.get('NodeName') == nb['name'] and n['Type'] == 'Bone')
            assoc['Association#%d' % len(assoc)] = a
    log.append('%d mesh(es) in %d vertex pool(s), %d palette(s); %d vertices, %d triangles'
               % (len(meshes), len(pools), len(palettes), sum(len(m['verts']) for m in meshes),
                  sum(len(m['indices']) for m in meshes) // 3))
    return t, bytes(vbuf), bytes(ibuf), log


def _remove_nodes(t, st, dropped):
    """Remove scene-tree nodes and renumber every reference to the rest."""
    keep = [i for i in range(len(st)) if i not in set(dropped)]
    new_of = {old: new for new, old in enumerate(keep)}
    for i in keep:
        n = st[i]
        for k in ('ParentNodeReferences',):
            if k in n:
                n[k] = type(n[k])(new_of[x] for x in n[k] if x in new_of)
        if 'ParentGeometryReference' in n and n['ParentGeometryReference'] in new_of:
            n['ParentGeometryReference'] = new_of[n['ParentGeometryReference']]
    for a in plist(t.get('AssociationPool')):
        if 'SceneTreeNodeRef' in a and a['SceneTreeNodeRef'] in new_of:
            a['SceneTreeNodeRef'] = new_of[a['SceneTreeNodeRef']]
    return [st[i] for i in keep]


def write(tree, vbuf, ibuf, oct_path):
    """Save .oct + <stem>_0.vbuf/.ibuf (the file names the buffer pools name)."""
    import c2oct
    stem = os.path.splitext(oct_path)[0]
    os.makedirs(os.path.dirname(oct_path) or '.', exist_ok=True)
    # The pools must name the files written here: a port built from another
    # car still names that car's buffers (boost -> kabuto_0.vbuf), and the
    # game would load those old buffers with the new mesh layout.
    for pool, ext in (('VertexBufferPool', '_0.vbuf'), ('IndexBufferPool', '_0.ibuf')):
        for b in plist(tree.get(pool)):
            if b.get('FileName'):
                b['FileName'] = os.path.basename(stem) + ext
    c2oct.save(tree, oct_path)
    with open(stem + '_0.vbuf', 'wb') as f:
        f.write(vbuf)
    with open(stem + '_0.ibuf', 'wb') as f:
        f.write(ibuf)
