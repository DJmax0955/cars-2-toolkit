"""Cars 3 -> Cars 2: put a Cars 3 model's meshes on a Cars 2 character.

The Cars 2 character (its .oct and .bent) stays the base: its rig, skin
skeleton, materials, vertex layouts and every number the game
cross-references are kept. Only the geometry is replaced, through
c2geom.rebuild - the same path the Blender mesh export uses.

What makes this work:
  * Cars 3 and Cars 2 name their skin bones alike (Body, FrontEnd, Jaw, Lip*,
    Cheek*, Brow*, Tire*_SpinJoint ...), so weights carry over by name.
  * Their skeletons sit within about a centimetre of each other, so the
    Cars 3 mesh is moved by the average offset of the wheel hubs - the wheels
    then spin about their own centres.
  * Cars 3 materials are matched to the Cars 2 character's materials by kind
    (body, tires, eyes, headlights, brake lights, glass).

What does not carry over: textures. Cars 3 UVs are used as they are, and the
Cars 2 textures were painted for the Cars 2 UVs.

Everything here is plain Python (no Blender): `convert()` runs from a script,
`build_geometry()` is what the Blender export feeds with the scene's meshes.
"""
import base64
import json
import math
import os
import copy
import re
import shutil
import struct
import uuid

import c2geom
import c2oct
import c2rigedit

# Which Cars 2 material a Cars 3 material becomes: the first keyword found in
# the Cars 3 material's name (lower case) picks the kind; the Cars 2 material
# whose name contains the same word is the target. None = leave it out.
KINDS = (('shadow', None), ('tire', 'tire'), ('eye', 'eye'), ('brake', 'brake'),
         ('head', 'head'), ('glass', 'glass'), ('body', 'body'))

# A Cars 3 material with no Cars 2 counterpart is added to the Cars 2 file as
# a new material named after it - a copy of the closest base material, to edit
# in the .oct.json afterwards - with its own mesh node. False: stop instead.
ADD_UNMATCHED_MATERIALS = True

# Which base material a new one starts as (first keyword found wins);
# anything else starts as the body material.
NEW_MATERIAL_TEMPLATE = (('brake', 'brake'), ('tail', 'brake'), ('light', 'head'),
                         ('lamp', 'head'), ('glass', 'glass'), ('window', 'glass'),
                         ('tire', 'tire'), ('wheel', 'tire'))

# RenderCaps bits that tell a Cars 3 primitive's LOD (same as cars3.py).
LOD_BITS = ((0x20000, 0), (0x40000, 1), (0x180000, 2))


# ------------------------------------------------------------------ materials
def material_kind(c3_name):
    """-> 'tire' | 'eye' | ... | None (leave out) | False (unknown)."""
    low = str(c3_name).lower()
    for key, kind in KINDS:
        if key in low:
            return kind
    return False


def pick_material(c3_name, c2_names):
    """Index of the Cars 2 material for a Cars 3 material name.
    -> (index or None, skipped): skipped is True for kinds that are left out
    on purpose (shadow casters); (None, False) means no match."""
    kind = material_kind(c3_name)
    if kind is None:
        return None, True
    if kind is False:
        return None, False
    for i, nm in enumerate(c2_names):
        if kind in str(nm).lower():
            return i, False
    return None, False


def _stem(name):
    """Blender's .001 and the importer's _LOD0 taken off a name."""
    return re.sub(r'_LOD\d+$', '', re.sub(r'\.\d{3}$', '', str(name or '')))


def name_match(part, c2_names):
    """Index of the Cars 2 material whose name the part carries: its material
    or object name is that name or contains it (Cars3_McQueen_Eyeshader ->
    eyeshader). Material name first, then object name; the longest Cars 2
    name wins (McQueen_Body2 over McQueen_Body, BrakeLights over Lights).
    -> index or None."""
    for src in (part.get('material'), part.get('name')):
        low = _stem(src).lower()
        if not low:
            continue
        best = None
        for i, nm in enumerate(c2_names):
            c2 = str(nm).lower()
            if len(c2) < 4 or c2 == 'error':
                continue
            if c2 == low:
                return i
            if c2 in low and (best is None or len(c2) > len(str(c2_names[best]))):
                best = i
        if best is not None:
            return best
    return None


def template_material(c3_name, c2_names, usable):
    """Index of the base material a new material starts as (one with a mesh)."""
    low = str(c3_name).lower()
    for key, kind in NEW_MATERIAL_TEMPLATE:
        if key in low:
            for i in usable:
                if kind in str(c2_names[i]).lower():
                    return i
    for i in usable:
        if 'body' in str(c2_names[i]).lower():
            return i
    return min(usable) if usable else None


def add_materials(tree, new_materials):
    """Append the planned new materials to the MaterialPool (existing indices
    stay as they are): copies of their template material, renamed, with a
    fresh Uuid. Returns the tree (changed in place)."""
    if not new_materials:
        return tree
    pool = tree['MaterialPool']
    keys = list(pool)
    prefix = keys[0].split('#', 1)[0] if keys else 'Material'
    mats = c2geom.plist(pool)
    for nm in new_materials:
        m = copy.deepcopy(mats[nm['template']])
        m['Name'] = nm['name']
        if 'Uuid' in m:
            m['Uuid'] = uuid.uuid4().bytes
        pool['%s#%d' % (prefix, len(pool))] = m
    return tree


# ------------------------------------------------------------------ Cars 3 data
def prim_lod(prim):
    caps = prim.get('RenderCaps')
    if not isinstance(caps, int):
        return None
    for bit, lod in LOD_BITS:
        if caps & bit == bit:
            return lod
    return None


def _pool(p):
    if not p:
        return []

    def num(k):
        tail = k.rsplit('#', 1)[-1]
        return int(tail) if tail.isdigit() else 0
    return [v for _k, v in sorted(p.items(), key=lambda kv: num(kv[0]))]


def _strip(path):
    for suffix in ('.oct.json', '_oct.json', '.json', '.oct'):
        if path.lower().endswith(suffix):
            return path[:-len(suffix)]
    return path


def read_cars3(json_path):
    """-> (data, vbuf, ibuf) from a Cars 3 .oct.json and the <n>_0.vbuf/.ibuf next to it."""
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    base = _strip(json_path)
    bufs = []
    for ext in ('_0.vbuf', '_0.ibuf'):
        p = base + ext
        if not os.path.isfile(p):
            raise ValueError('%s not found next to the model' % os.path.basename(p))
        with open(p, 'rb') as f:
            bufs.append(f.read())
    return data, bufs[0], bufs[1]


def _mesh_nodes(data):
    return [n for n in data['SceneTreeNodePool'].values()
            if n.get('Type') == 'Geometry' and n.get('Primitives')]


def _decode(vb, ib, data, prim):
    """(positions, uvs, [(influence slot, weight)...], triangles) of a primitive."""
    ib_ref, offset, _pad, count = prim['Idata']
    width = data['IndexBufferPool']['IndexBuffer#%d' % ib_ref]['Width']
    idx = struct.unpack_from('<%d%s' % (count, {1: 'B', 2: 'H', 4: 'I'}[width]), ib, offset)
    _n, vcount, _p0, off_a, stride_a, _p1, off_b, stride_b = prim['Vdata']
    ub, us = prim['UnitBase'], prim['UnitScale']
    pos, uvs, skin = [], [], []
    for i in range(vcount):
        o = off_a + i * stride_a
        x, y, z, _w = struct.unpack_from('<4h', vb, o)
        pos.append((ub[0] + x / 32767.0 * us[0], ub[1] + y / 32767.0 * us[1], ub[2] + z / 32767.0 * us[2]))
        u, v = struct.unpack_from('<2e', vb, o + 8)
        uvs.append((u, v))                                  # the game's own convention
        ob = off_b + i * stride_b
        bi = struct.unpack_from('<4B', vb, ob)
        bw = struct.unpack_from('<4B', vb, ob + 4)
        pairs = [(bi[k], bw[k]) for k in range(4) if bw[k] > 0] or [(bi[0], 1)]
        skin.append(pairs)
    return pos, uvs, skin, list(idx)


def cars3_parts(data, vb, ib, lod=0):
    """The Cars 3 primitives of one LOD (None = all) as parts for build_geometry,
    in raw Cars 3 space."""
    mats = _pool(data.get('MaterialPool'))
    parts = []
    for node in _mesh_nodes(data):
        infl = node.get('Influences') or {}
        local = [infl['Influence#%d' % i].get('Name') for i in range(len(infl))]
        for key, prim in node['Primitives'].items():
            if lod is not None and prim_lod(prim) != lod:
                continue
            try:
                mat = mats[int(prim.get('MaterialReference'))]['Name']
            except (TypeError, ValueError, IndexError, KeyError):
                mat = prim.get('MaterialName', 'material')
            pos, uvs, skin, tris = _decode(vb, ib, data, prim)
            verts = []
            for p, uv, pairs in zip(pos, uvs, skin):
                bones = [(local[b], w) for b, w in pairs if 0 <= b < len(local) and local[b]]
                verts.append({'Position': p, 'Uv1': uv, '_bones': bones})
            parts.append({'material': mat, 'name': '%s %s' % (mat, key), 'verts': verts, 'indices': tris})
    return parts


def _inv4(m):
    """Inverse of a 4x4 (list of rows) by Gauss-Jordan."""
    n = 4
    a = [list(map(float, m[i])) + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            raise ValueError('singular matrix')
        a[c], a[p] = a[p], a[c]
        d = a[c][c]
        a[c] = [x / d for x in a[c]]
        for r in range(n):
            if r != c:
                f = a[r][c]
                a[r] = [x - f * y for x, y in zip(a[r], a[c])]
    return [row[n:] for row in a]


def cars3_bone_positions(data):
    """{skin bone: (x, y, z)} of the Cars 3 skeleton in raw Cars 3 space."""
    out = {}
    for node in _mesh_nodes(data):
        infl = node.get('Influences') or {}
        for i in range(len(infl)):
            inf = infl.get('Influence#%d' % i) or {}
            nm = inf.get('Name')
            if nm and nm not in out:
                f = inf['BindPoseSkinLocalToWorldMatrixInverseData']
                inv = _inv4([f[0:4], f[4:8], f[8:12], f[12:16]])
                out[nm] = tuple(inv[3][:3])                 # row-vector layout: translation in the last row
    return out


# ------------------------------------------------------------------ Cars 2 base
def template_info(tree):
    """-> dict: materials (names), nodes {material index: first mesh node}, skin {bone: pivot}."""
    st = c2geom.plist(tree['SceneTreeNodePool'])
    mats = [m.get('Name', '') for m in c2geom.plist(tree['MaterialPool'])]
    nodes = {}
    for i, n in enumerate(st):
        if n['Type'] in c2geom.SUBGEOM:
            nodes.setdefault(n['MaterialReference'], i)
    skin = {n['NodeName']: tuple(n['PivotPoint']) for n in st if n['Type'] == 'Bone'}
    return {'st': st, 'materials': mats, 'nodes': nodes, 'skin': skin}


def alignment_offset(tree, c3_positions):
    """How far to move the Cars 3 meshes (in the space `c3_positions` is in) so
    its wheel hubs sit on the Cars 2 ones. -> ((dx, dy, dz), worst bone gap after, bone)."""
    skin = template_info(tree)['skin']
    common = [n for n in c3_positions if n in skin]
    if not common:
        raise ValueError('no skin bone of the model has the name of a Cars 2 skin bone')
    hubs = [n for n in common if n.startswith('Tire') and n.endswith('SpinJoint')] or common
    d = [sum(skin[n][k] - c3_positions[n][k] for n in hubs) / len(hubs) for k in range(3)]
    worst, who = 0.0, None
    for n in common:
        g = math.dist([c3_positions[n][k] + d[k] for k in range(3)], skin[n])
        if g > worst:
            worst, who = g, n
    return tuple(d), worst, who


# ------------------------------------------------------------------ bones the Cars 2 base doesn't have
# A Cars 3 skin bone the Cars 2 character lacks (Mack's middle-axle wheels,
# say) becomes a new bone, the way Add Selected Bones to Rig makes one: a rig
# node under its parent's node, and a game bone with the same name, parent and
# place, linked to it. A new ..._SpinJoint also gets the driven key the
# character's own wheels spin by (RotateFront for front wheels, RotateRear
# for the rest) - the same curve and axis, byte for byte.

TWO_PI = 2.0 * math.pi


def _mat_cols(rv):
    """Row-vector flat 16 (translation in the last row) -> column-vector rows."""
    return [[rv[0], rv[4], rv[8], rv[12]], [rv[1], rv[5], rv[9], rv[13]],
            [rv[2], rv[6], rv[10], rv[14]], [0.0, 0.0, 0.0, 1.0]]


def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _quat(m):
    """(x, y, z, w) of a column-vector rotation matrix."""
    tr = m[0][0] + m[1][1] + m[2][2]
    if tr > 0:
        S = math.sqrt(tr + 1.0) * 2
        w, x, y, z = 0.25 * S, (m[2][1] - m[1][2]) / S, (m[0][2] - m[2][0]) / S, (m[1][0] - m[0][1]) / S
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        S = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2
        w, x, y, z = (m[2][1] - m[1][2]) / S, 0.25 * S, (m[0][1] + m[1][0]) / S, (m[0][2] + m[2][0]) / S
    elif m[1][1] > m[2][2]:
        S = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2
        w, x, y, z = (m[0][2] - m[2][0]) / S, (m[0][1] + m[1][0]) / S, 0.25 * S, (m[1][2] + m[2][1]) / S
    else:
        S = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2
        w, x, y, z = (m[1][0] - m[0][1]) / S, (m[0][2] + m[2][0]) / S, (m[1][2] + m[2][1]) / S, 0.25 * S
    return x, y, z, w


def _rot_cols(r):
    """Column-vector 4x4 of a stored rotor (stored conjugated: q = (w, -x, -y, -z))."""
    x, y, z, w = -r[0], -r[1], -r[2], r[3]
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0.0],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0.0],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0.0],
            [0.0, 0.0, 0.0, 1.0]]


def _node_local(n):
    d = n.get('Data') if isinstance(n.get('Data'), dict) else {}
    t = list(d.get('Translation') or (0.0, 0.0, 0.0))
    m = [[1.0, 0.0, 0.0, t[0]], [0.0, 1.0, 0.0, t[1]], [0.0, 0.0, 1.0, t[2]], [0.0, 0.0, 0.0, 1.0]]
    if 'Orientation#Rotor' in d:
        m = _mul(m, _rot_cols(d['Orientation#Rotor']))
    px = d.get('Orientation#OrientProxy')
    if isinstance(px, dict):
        m = _mul(m, _mul(_rot_cols(px.get('ParentProxyToParent#Rotor', (0, 0, 0, 1))),
                         _rot_cols(px.get('LocalToLocalProxy#Rotor', (0, 0, 0, 1)))))
    sc = d.get('Scale')
    if sc:
        m = _mul(m, [[sc[0], 0, 0, 0], [0, sc[1], 0, 0], [0, 0, sc[2], 0], [0, 0, 0, 1]])
    return m


def _rig_world(net, i, depth=0):
    n = net.data[i]
    b = n.get('BasisDataNodeRef')
    loc = _node_local(n)
    return _mul(_rig_world(net, b, depth + 1), loc) if b is not None and depth < 256 else loc


def rig_offset(tree, net):
    """How far the base's game bones sit from their rig nodes. The rig keeps
    the skeleton below Main lower than the game bones (McQueen: 0.44, his
    Body height); Main itself agrees. Median over the joints below Main."""
    diffs = []
    for n in c2geom.plist(tree['SceneTreeNodePool']):
        if n.get('Type') != 'Bone' or n['NodeName'] == 'Main':
            continue
        path = 'Anchor/%s/Transform' % n['NodeName']
        if net.has(path):
            w = _rig_world(net, net.index(path))
            diffs.append(tuple(n['PivotPoint'][k] - w[k][3] for k in range(3)))
    if not diffs:
        return (0.0, 0.0, 0.0)
    return tuple(sorted(d[k] for d in diffs)[len(diffs) // 2] for k in range(3))


def spin_driver(name):
    return 'Anchor/Main_Ctrl/RotateFront' if name.startswith('TireF') else 'Anchor/Main_Ctrl/RotateRear'


def plan_new_bones(tree, wanted, c3_skeleton, parent_override=None, net=None, off=None):
    """wanted: names of Cars 3 bones the base lacks that vertices use.
    c3_skeleton: {bone: (row-vector flat 16 matrix in Cars 2 space, Cars 3 parent)}.
    parent_override: {bone: parent} - e.g. a dynamic chain's bones in a line.
    -> (new_bones for c2geom.rebuild, rig edits, notes, problems)"""
    parent_override = parent_override or {}
    st = c2geom.plist(tree['SceneTreeNodePool'])
    skin = {n['NodeName']: list(n['BoneToModelMatrix']) for n in st if n.get('Type') == 'Bone'}
    if net is None:
        sn = tree['SubNetworkPool']['SubNetwork#0']
        from c2scene import SceneNetwork
        net = SceneNetwork.from_subnetwork(sn)
    notes, problems = [], []
    # bones to add: the wanted ones plus any Cars 3 ancestors the base lacks
    todo = []

    def need(b, depth=0):
        if b in skin or b in todo or depth > 64:
            return
        if b not in c3_skeleton:
            problems.append('bone %s: not on the Cars 2 skeleton and its place is unknown' % b)
            return
        p = parent_override.get(b, c3_skeleton[b][1])
        while p and p not in skin and p not in c3_skeleton:
            p = None
        if p and p not in skin:
            need(p, depth + 1)
        todo.append(b)
    for b in sorted(wanted):
        need(b)
    new_bones, edits, planned = [], [], {}
    if off is None:
        off = rig_offset(tree, net)        # game bone - rig node, below Main

    def rig_of(rv):
        """A game-space bone matrix (row-vector flat) -> its rig node's world (columns)."""
        c = _mat_cols(rv)
        for k in range(3):
            c[k][3] -= off[k]
        return c
    for b in todo:
        m, p = c3_skeleton[b]
        p = parent_override.get(b, p)
        if not p or (p not in skin and p not in planned):
            p = 'Main' if 'Main' in skin else ('Body' if 'Body' in skin else None)
        if p is None:
            problems.append('bone %s: nothing on the Cars 2 skeleton to attach it to' % b)
            continue
        ppath = 'Anchor/%s/Transform' % p
        if p in skin and not net.has(ppath):
            # v1.97.2: the base has the parent as a skin bone but no rig node for it
            # (Mater's tow-cable bones under BackEnd, built on Pacer): follow the
            # nearest ancestor that has one
            q, guard = p, 0
            while q and not net.has('Anchor/%s/Transform' % q) and guard < 64:
                q = c3_skeleton[q][1] if q in c3_skeleton else None
                guard += 1
            if not q:
                q = next((x for x in ('Main', 'Body') if x in skin and net.has('Anchor/%s/Transform' % x)), None)
            if not q:
                problems.append('bone %s: its parent %s has no rig node %s' % (b, p, ppath))
                continue
            notes.append('bone %s: the base has no rig node for its parent %s - it follows %s' % (b, p, q))
            p, ppath = q, 'Anchor/%s/Transform' % q
        # the rig node's place relative to its parent's actual rig node
        parent_w = rig_of(planned[p]) if p in planned else _rig_world(net, net.index(ppath))
        local = _mul(_inv4(parent_w), rig_of(m))
        qx, qy, qz, qw = _quat([row[:3] + [0.0] for row in local[:3]] + [[0, 0, 0, 1]])
        path = 'Anchor/%s/Transform' % b
        edits.append({'op': 'add_node', 'path': path, 'parent': ppath,
                      'translation': [local[0][3], local[1][3], local[2][3]],
                      'rotor': [-qx, -qy, -qz, qw]})          # stored conjugated
        new_bones.append({'name': b, 'parent': p, 'matrix': [float(x) for x in m]})
        planned[b] = m
        msg = 'new bone %s under %s' % (b, p)
        if '_SpinJoint' in b:          # also extra spin joints (Mack's TireMidLeft_SpinJoint_OL)
            drv = spin_driver(b)
            if net.has(drv):
                edits.append({'op': 'driven_key', 'driver': drv, 'outputs': [[path, 'rot_x']],
                              'points': [[-360.0, -TWO_PI], [360.0, TWO_PI]], 'linear': True})
                msg += ', spinning with %s' % drv.split('/')[-1]
            else:
                notes.append('%s: the base has no %s, so it does not spin' % (b, drv))
        notes.append(msg)
    return new_bones, edits, notes, problems


# ------------------------------------------------------------------ retarget: Cars 3 positions
# The Cars 2 character's skeleton, controls and FX markers moved to where the
# Cars 3 model has them. Everything moves by a difference (new - old place),
# so each stored field keeps its own convention:
#   game bones   BoneToModel / BoneToStandard / PivotPoint / BoneEndPoint by
#                their move, LocalToParent by their move relative to the parent
#   skeleton rig nodes  by their bone's move (mostly constraint-driven in game,
#                but their rest should agree)
#   controls, proxies, FX markers  to the Cars 3 place minus the rig offset
#                (the rule the Cars 3 import placed them by); position only -
#                animations are stored relative to each control's rest
#                rotation; clip-driven ones' rest-table copies move too
# Left alone: nodes the Cars 3 model doesn't have (they ride with their
# parent) and nodes the rig computes (slider-driven proxies, spin joints).

def _rest_find(pool, seq):
    for o in range(len(pool) - len(seq) + 1):
        if all(struct.pack('<f', pool[o + k]) == struct.pack('<f', seq[k]) for k in range(len(seq))):
            return o
    pool.extend(seq)
    return len(pool) - len(seq)


def _set_rotation(net, i, rotor):
    """Orientation#Rotor (stored conjugated) - and the rest-table rotation of a
    clip-driven node, appended (pool values are shared between nodes)."""
    n = net.data[i]
    d = n.get('Data') if isinstance(n.get('Data'), dict) else {}
    d['Orientation#Rotor'] = c2oct.FloatList(float(x) for x in rotor)
    order = ['Translation', 'Orientation#Rotor', 'Orientation#OrientProxy', 'Scale']
    n['Data'] = {k: d[k] for k in sorted(d, key=lambda k: order.index(k) if k in order else len(order))}
    if i in net.rest:
        a, _b = net.rest[i]
        net.rest[i] = (a, _rest_find(net.rest_pool, [0.0 if abs(x) < 1e-6 else float(x) for x in rotor]))


def _set_translation(net, i, t):
    n = net.data[i]
    d = n.get('Data') if isinstance(n.get('Data'), dict) else {}
    d['Translation'] = c2oct.FloatList(float(x) for x in t)
    order = ['Translation', 'Orientation#Rotor', 'Orientation#OrientProxy', 'Scale']
    n['Data'] = {k: d[k] for k in sorted(d, key=lambda k: order.index(k) if k in order else len(order))}
    if i in net.rest:                                # clip-driven: its rest-table copy too
        a, b = net.rest[i]
        seq = [0.0 if abs(x) < 1e-6 else float(x) for x in t]
        pool = net.rest_pool
        found = None
        for o in range(len(pool) - 2):
            if all(struct.pack('<f', pool[o + k]) == struct.pack('<f', seq[k]) for k in range(3)):
                found = o
                break
        if found is None:
            pool.extend(seq)
            found = len(pool) - 3
        net.rest[i] = (found, b)


WHEELS = ('FLeft', 'FRight', 'BLeft', 'BRight')


# The control rig's ground chain: these sit on the ground (y 0 in model
# space = -off in rig space); everything hanging off them keeps its place.
GROUND_NODES = ('MainOrient_Ctrl', 'Main_Ctrl', 'Main_Baxle_Ctrl')


def place_wheel_rig(net, skin, off):
    """The wheel rig exactly as vanilla Cars 2 builds it (McQueen, H = his
    hub height 0.44), at the model's hubs (TireX_centerJoint):
      Main_CtrlGrp             (0, -H, 0)      the ground at model y 0
      Front/BackTires_Ctrl     (0, H, ...)     the axle, at its hubs' height
      X_bttm_Ctrl_Grp          (x, -H, z)      straight down to the ground
      X_Center_Ctrl            (0, H, 0)       straight up to the hub
      TireX_centerJoint        on the hub      (its SpinJoint rides on it)
    Moving these by differences kept a base's old misplacement - the hub off
    the steer / suspension axis makes the tires hop when driving. A vanilla
    base at its own hubs is left exactly as it is. `net` changed in place.
    -> (moves [(path, local translation, None)], notes)"""
    moves, fixed, notes = [], [], []
    ground = -off[1]                                        # model y 0, rig space

    def world(path):
        return _rig_world(net, net.index(path))

    def place(path, tgt, keep=None):
        """Put the node at rig-space position tgt (keep: axes left as they are)."""
        if not net.has(path):
            return False
        i = net.index(path)
        n = net.data[i]
        d = n.get('Data') if isinstance(n.get('Data'), dict) else {}
        if 'Translation' not in d:
            return False                                    # sits on its parent: carried there
        cur = _rig_world(net, i)
        tgt = [cur[k][3] if keep and k in keep else tgt[k] for k in range(3)]
        pb = n.get('BasisDataNodeRef')
        pw = _rig_world(net, pb) if pb is not None else [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]
        inv = _inv4(pw)
        t = [sum(inv[r][c] * (tgt + [1.0])[c] for c in range(4)) for r in range(3)]
        if max(abs(a - b) for a, b in zip(t, d['Translation'])) < 1e-6:
            return True
        _set_translation(net, i, t)
        moves.append((path, t, None))
        fixed.append(path.split('/')[1])
        return True

    hubs = {}
    for w in WHEELS:
        hub = skin.get('Tire%s_centerJoint' % w) or skin.get('Tire%s_SpinJoint' % w)
        if hub is not None:
            hubs[w] = [hub[k] - off[k] for k in range(3)]   # rig space
    if not hubs:
        return moves, notes

    # 1. the ground at model y 0; what hangs off the ground chain stays put
    gp = 'Anchor/Main_CtrlGrp/Transform'
    if net.has('Anchor/Main_Ctrl/Transform') and net.has(gp) and \
            abs(world('Anchor/Main_Ctrl/Transform')[1][3] - ground) > 1e-6:
        ground_idx = {net.index('Anchor/%s/Transform' % g) for g in GROUND_NODES
                      if net.has('Anchor/%s/Transform' % g)}
        hang = [k for k, nd in enumerate(net.data)
                if nd['Type'] in ('Transform', 'Dilation') and nd.get('BasisDataNodeRef') in ground_idx
                and k not in ground_idx]
        before = {k: _rig_world(net, k) for k in hang}
        dy = ground - world('Anchor/Main_Ctrl/Transform')[1][3]
        cur = world(gp)
        if place(gp, [cur[0][3], cur[1][3] + dy, cur[2][3]]):
            stuck = []
            for k in hang:
                if not place(net.path_of(k), [before[k][r][3] for r in range(3)]):
                    stuck.append(net.path_of(k).split('/')[1])
            if stuck:
                notes.append('moved with the ground (no position field): %s' % ', '.join(stuck))

    # 2. each axle control at its hubs' height, 3. each wheel on its hub
    for axle, side in (('FrontTires_Ctrl', 'F'), ('BackTires_Ctrl', 'B')):
        hs = [h for w, h in hubs.items() if w.startswith(side)]
        if hs:
            place('Anchor/%s/Transform' % axle, [0.0, sum(h[1] for h in hs) / len(hs), 0.0], keep=(0, 2))
    for w, h in hubs.items():
        place('Anchor/%s_bttm_Ctrl_Grp/Transform' % w, [h[0], ground, h[2]])
        place('Anchor/%s_Center_Ctrl/Transform' % w, h)
        place('Anchor/Tire%s_centerJoint/Transform' % w, h)
    if fixed:
        notes.insert(0, 'wheel rig placed as vanilla Cars 2 has it (ground at 0, bttm under each hub, '
                        'Center and centerJoint on it): %s' % ', '.join(dict.fromkeys(fixed)))
    return moves, notes


def plan_retarget(tree, net, c3_pose, node_of_bone, no_turn=()):
    """Move the base's rig in `net` (changed in place) to the Cars 3 places.
    c3_pose: {bone: row-vector flat 16 matrix, game space} - every bone of the
    Cars 3 import (skin, controls, FX markers). node_of_bone: {bone name: rig
    node name} for the Cars 2 names that differ (FX_Marker_X_Ctrl ->
    FX_Marker_X). -> (moves [(path, local translation)], skin {bone: new
    pivot}, notes)"""
    st = c2geom.plist(tree['SceneTreeNodePool'])
    bones = {n['NodeName']: n for n in st if n.get('Type') == 'Bone'}
    off = rig_offset(tree, net)
    want = {}                                        # node index -> rig-space world position
    want_rot = {}                                    # FX markers: node index -> world rotation (3x3)
    skin = {}
    for b, m in c3_pose.items():
        pos = m[12:15]
        if b in bones:                               # a skin bone: move by its own difference
            skin[b] = list(pos)
            path = 'Anchor/%s/Transform' % b
            if net.has(path):
                i = net.index(path)
                w = _rig_world(net, i)
                old = bones[b]['PivotPoint']
                want[i] = [w[k][3] + pos[k] - old[k] for k in range(3)]
            continue
        raw = node_of_bone.get(b, b)
        path = 'Anchor/%s/Transform' % raw
        if not net.has(path):
            continue
        i = net.index(path)
        if net.data[i]['Type'] not in ('Transform', 'Dilation'):
            continue
        w = net.writer_of(i)
        if w is not None and net.procs[w]['Type'] in ('RelationshipProcessor', 'ClipProcessor', 'DynamicChain'):
            continue                                 # the rig computes it
        want[i] = [pos[k] - off[k] for k in range(3)]
        if raw.startswith('FX_') and b not in no_turn:   # a marker's rotation aims its effect
            want_rot[i] = [row[:3] for row in _mat_cols(m)[:3]]

    def depth(i):
        k = 0
        while net.data[i].get('BasisDataNodeRef') is not None and k < 256:
            i = net.data[i]['BasisDataNodeRef']
            k += 1
        return k
    linked = set(d for ls in net.links for d in ls['nodes'] if d >= 0)
    kids = {}
    for k, nd in enumerate(net.data):
        if nd['Type'] in ('Transform', 'Dilation') and nd.get('BasisDataNodeRef') is not None:
            kids.setdefault(nd['BasisDataNodeRef'], []).append(k)

    def has_pos(k):
        dd = net.data[k].get('Data')
        return isinstance(dd, dict) and 'Translation' in dd

    def at_zero(k):
        dd = net.data[k].get('Data')
        return not isinstance(dd, dict) or not any(key in dd for key in
                                                   ('Translation', 'Orientation#Rotor', 'Orientation#OrientProxy'))

    def carrier(k):
        """A node without a position field: the nearest parent that has one
        and carries only it (not animated, not computed, not moved itself) -
        the game may ignore a field added to a node that lacks it. On the way
        it may pass a slider proxy that sits at zero on its parent (Jaw_Ctrl
        -> Jaw_Proxy -> Jaw_CtrlGrp): the sliders pose the proxy relative to
        its parent, so moving the group carries proxy and control along."""
        j = k
        while True:
            pj = net.data[j].get('BasisDataNodeRef')
            if pj is None or kids.get(pj) != [j] or pj in linked or pj in want:
                return None
            w = net.writer_of(pj)
            if w is not None:
                if net.procs[w]['Type'] == 'RelationshipProcessor' and at_zero(pj):
                    j = pj                            # pass the slider proxy
                    continue
                return None
            if has_pos(pj):
                return pj
            j = pj
    added, unplaced = [], []
    for i in list(want):
        if not has_pos(i):
            j = carrier(i)
            if j is not None:                         # move the carrier by the same amount
                cur = _rig_world(net, i)
                cj = _rig_world(net, j)
                target = want.pop(i)
                want[j] = [cj[k][3] + target[k] - cur[k][3] for k in range(3)]
            elif net.writer_of(i) is not None:
                want.pop(i)                           # the rig computes it every frame: leave it
            elif i in linked and i not in net.rest:
                # a clip channel with no rest-table entry: the game animates
                # it from zero, so a position added to it is lost in every
                # clip (a face control off its group) - leave it, and say so
                target = want.pop(i)
                cur = _rig_world(net, i)
                mv = [target[k] - cur[k][3] for k in range(3)]
                a = net.data[i].get('BasisDataNodeRef')
                while a is not None and a not in want:      # carried there by a moved parent?
                    a = net.data[a].get('BasisDataNodeRef')
                if a is not None:
                    ca = _rig_world(net, a)
                    mv = [mv[k] - (want[a][k] - ca[k][3]) for k in range(3)]
                if max(abs(x) for x in mv) > 1e-5:
                    unplaced.append(i)
            else:
                added.append(i)
    moves, biggest, rot_added = [], 0.0, []
    for i in sorted(want, key=depth):                # parents first
        n = net.data[i]
        pb = n.get('BasisDataNodeRef')
        pw = _rig_world(net, pb) if pb is not None else [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]
        inv = _inv4(pw)
        tgt = want[i] + [1.0]
        t = [sum(inv[r][c] * tgt[c] for c in range(4)) for r in range(3)]
        d = n.get('Data') if isinstance(n.get('Data'), dict) else {}
        old = list(d.get('Translation') or (0.0, 0.0, 0.0))
        rotor = None
        if i in want_rot:
            R = want_rot[i]
            local = [[sum(inv[r][k] * R[k][c] for k in range(3)) for c in range(3)] for r in range(3)]
            qx, qy, qz, qw = _quat([row + [0.0] for row in local] + [[0, 0, 0, 1]])
            new = [-qx, -qy, -qz, qw]                # stored conjugated
            cur = list(d.get('Orientation#Rotor') or (0.0, 0.0, 0.0, 1.0))
            if sum(a * b for a, b in zip(new, cur)) < 0:
                new = [-x for x in new]              # keep the sign the file has
            has_rot = 'Orientation#Rotor' in d
            if max(abs(a - b) for a, b in zip(new, cur)) > 1e-5 and \
                    (has_rot or abs(abs(new[3]) - 1.0) > 1e-5):
                rotor = new
                if not has_rot:
                    rot_added.append(i)
        if max(abs(a - b) for a, b in zip(t, old)) < 1e-6 and rotor is None:
            continue
        biggest = max(biggest, math.sqrt(sum((a - b) ** 2 for a, b in zip(t, old))))
        _set_translation(net, i, t)
        if rotor is not None:
            _set_rotation(net, i, rotor)
        moves.append((net.path_of(i), t, rotor))
    wheel_moves, wheel_notes = place_wheel_rig(net, skin, off)
    moves += wheel_moves
    notes = ['moved %d rig nodes and %d game bones to the model\'s places (largest local move %.3f)'
             % (len(moves), sum(1 for b in skin if max(abs(skin[b][k] - bones[b]['PivotPoint'][k])
                                                        for k in range(3)) > 1e-6), biggest)]
    moved_paths = set(m[0] for m in moves)
    turned = sum(1 for m in moves if m[2] is not None)
    if turned:
        notes.append('%d FX marker(s) turned to the model\'s rotation' % turned)
    notes += wheel_notes
    if rot_added:
        notes.append('rotation added to %d FX marker(s) that had none: %s' % (
            len(rot_added), ', '.join(net.path_of(i).split('/')[1] for i in rot_added)))
    added = [net.path_of(i) for i in added if net.path_of(i) in moved_paths]
    if added:
        notes.append('position field added to %d node(s) that had none: %s' % (
            len(added), ', '.join(p.split('/')[1] for p in added[:6])))
    if unplaced:
        notes.append('not moved (animated from zero, nothing to move them by): %s'
                     % ', '.join(net.path_of(i).split('/')[1] for i in unplaced[:8]))
    return moves, skin, notes


def derive_rig_pose(tree, pose):
    """A rigging template has only the skin bones (and maybe FX markers):
    place the rest of the base's control rig from how the skin bones moved,
    so plan_retarget can move it like a Cars 3 import's full rig.

    pose: {bone: row-vector flat 16, game space} - the template's bones.
    -> (pose with an entry added for every rig node it doesn't name, the
        names added, notes) - pass the names as plan_retarget's no_turn

    Each rig node moves by the move (new pivot - old pivot) of a skin bone:
    - a control that drives a skin bone (the first input of the bone's
      constraint: Jaw_Ctrl -> Jaw, Body_Final_Proxy -> Body, FLeft_Center_Ctrl
      -> TireFLeft_centerJoint): by that bone's move;
    - a back-axle node (`Baxle` in its name): by the back hubs' average;
    - the common parents of every control (Main_Ctrl and above): not moved;
    - any other node with such controls below it: by the nearest of them (by
      place; ties averaged - FrontTires_Ctrl takes both front hubs, a wheel's
      bttm / steer group its own hub);
    - a node with none below it (FX markers, Eyes_Ctrl): by its parent's move;
    - a node on the ground (the wheel bttm controls, Main_Baxle_Ctrl) keeps
      its height: it moves only sideways / along.
    Rotations are kept."""
    from c2scene import SceneNetwork, _inputs
    net = SceneNetwork.from_subnetwork(tree['SubNetworkPool']['SubNetwork#0'])
    st = c2geom.plist(tree['SceneTreeNodePool'])
    bones = {n['NodeName']: n for n in st if n.get('Type') == 'Bone'}
    off = rig_offset(tree, net)
    delta = {b: [pose[b][12 + k] - bones[b]['PivotPoint'][k] for k in range(3)] for b in bones if b in pose}
    out = dict(pose)
    xf = [i for i, n in enumerate(net.data) if n['Type'] in ('Transform', 'Dilation')]
    world = {i: _rig_world(net, i) for i in xf}
    kids = {}
    for i in xf:
        kids.setdefault(net.data[i].get('BasisDataNodeRef'), []).append(i)
    skin_nodes = {net.index('Anchor/%s/Transform' % b): b for b in bones if net.has('Anchor/%s/Transform' % b)}
    driver = {}                                       # control node -> skin bone it drives
    for i, b in skin_nodes.items():
        ci = net.constraint_of(net.path_of(i))
        if ci is not None:
            ins = [x for x in _inputs(net.procs[ci]) if x is not None and x >= 0]
            if ins and ins[0] not in skin_nodes:
                driver[ins[0]] = b
    below = {}

    def collect(i):
        if i in below:
            return below[i]
        s = set([i]) if i in driver else set()
        for k in kids.get(i, ()):
            s |= collect(k)
        below[i] = s
        return s
    for i in xf:
        collect(i)
    all_drivers = set(driver)
    back = [b for b in ('TireBLeft_centerJoint', 'TireBRight_centerJoint') if b in delta]
    ground = None
    if net.has('Anchor/Main_Ctrl/Transform'):
        ground = world[net.index('Anchor/Main_Ctrl/Transform')][1][3]
    named = {node_name for node_name in pose}
    moved = {}

    def avg(vs):
        return [sum(v[k] for v in vs) / len(vs) for k in range(3)] if vs else [0.0, 0.0, 0.0]

    def move_of(i):
        if i in moved:
            return moved[i]
        nm = net.path_of(i).split('/')[1]
        p = net.data[i].get('BasisDataNodeRef')
        if i in skin_nodes:
            d = delta.get(skin_nodes[i], [0.0, 0.0, 0.0])
        elif i in driver:
            d = delta.get(driver[i], [0.0, 0.0, 0.0])
        elif 'Baxle' in nm and back:
            d = avg([delta[b] for b in back])
        elif all_drivers and below[i] >= all_drivers:
            d = [0.0, 0.0, 0.0]                       # Main_Ctrl and above
        elif below[i]:
            here = [world[i][k][3] for k in range(3)]
            dist = {j: math.sqrt(sum((world[j][k][3] - here[k]) ** 2 for k in range(3))) for j in below[i]}
            best = min(dist.values())
            d = avg([delta.get(driver[j], [0.0, 0.0, 0.0]) for j in below[i] if dist[j] - best < 1e-3])
        else:
            d = list(move_of(p)) if p is not None and p in world else [0.0, 0.0, 0.0]
        d = list(d)
        if ground is not None and i not in skin_nodes and abs(world[i][1][3] - ground) < 0.01:
            d[1] = 0.0                                # on the ground: keep the height
        moved[i] = d
        return d
    added = 0
    for i in xf:
        nm = net.path_of(i).split('/')[1]
        if i in skin_nodes or nm in named or nm + '_Ctrl' in named:
            continue
        d = move_of(i)                                # (unmoved ones too: their parents may move)
        w = world[i]
        rv = [w[0][0], w[1][0], w[2][0], 0.0, w[0][1], w[1][1], w[2][1], 0.0,
              w[0][2], w[1][2], w[2][2], 0.0,
              w[0][3] + off[0] + d[0], w[1][3] + off[1] + d[1], w[2][3] + off[2] + d[2], 1.0]
        out[nm] = rv
        added += max(abs(x) for x in d) > 1e-7
    notes = ['rig placed from the skin bones: %d control / rig node(s) moved with the bone they '
             'drive (or the nearest one below them)' % added]
    return out, set(out) - set(pose), notes


MARKER_PARENT = 'Anchor/Body_Final_Proxy/Transform'     # where every Cars 2 FX marker hangs


def plan_new_markers(net, c3_pose, node_of_bone, off):
    """FX markers of the Cars 3 model that the base has no node for (Fritter's
    second exhaust on Sheriff) -> [{'path', 'basis', 'translation', 'rotor'?}]:
    a Transform under Body_Final_Proxy, like every Cars 2 marker, at the
    Cars 3 place (rig space) and, when turned, its rotation."""
    if not net.has(MARKER_PARENT):
        return []
    inv = _inv4(_rig_world(net, net.index(MARKER_PARENT)))
    out = []
    for bone, m in sorted(c3_pose.items()):
        if not bone.startswith('FX_'):
            continue
        raw = node_of_bone.get(bone, bone[:-len('_Ctrl')] if bone.endswith('_Ctrl') else bone)
        path = 'Anchor/%s/Transform' % raw
        if net.has(path):
            continue
        w = _mat_cols(m)
        for k in range(3):
            w[k][3] -= off[k]
        local = _mul(inv, w)
        spec = {'path': path, 'basis': MARKER_PARENT, 'translation': [local[k][3] for k in range(3)]}
        qx, qy, qz, qw = _quat([row[:3] + [0.0] for row in local[:3]] + [[0, 0, 0, 1]])
        if abs(abs(qw) - 1.0) > 1e-6:
            spec['rotor'] = [-qx, -qy, -qz, qw]               # stored conjugated
        out.append(spec)
    return out


def add_markers(net, markers):
    """Add the planned markers and connect each to its slot in the clip
    templates that have one (car_body: Exhaust_Left1/2, Exhaust_Right1/2,
    Battery_*, Bumper, Headlight_*, MachineGun_*, SkateJack, SpareTire,
    WGPCam) - that is how the game finds a marker."""
    log, connected, loose = [], [], []
    for m in markers:
        if net.has(m['path']):
            continue
        data = {'Translation': m['translation']}
        if m.get('rotor'):
            data['Orientation#Rotor'] = m['rotor']
        net.add_data(m['path'], 'Transform', data=data, basis=m['basis'])
        hit = False
        for ct in sorted(set(ls['ct'] for ls in net.links)):
            e = net._ct.get(ct)
            if e and m['path'] in [x and x['channel'] for x in e['slots']]:
                net.link(ct, m['path'], m['path'])
                hit = True
        if hit:
            # shipping markers are connected but have no rest-table entry (the
            # game reads their stored place) - the same for a new one
            import c2scene
            net.rest[net.index(m['path'])] = (c2scene.NONE16, c2scene.NONE16)
        (connected if hit else loose).append(m['path'].split('/')[1])
    if connected:
        log.append('new FX markers, connected to their clip-template slot: %s' % ', '.join(connected))
    if loose:
        log.append('new FX markers with no Cars 2 template slot (in the file, but nothing in Cars 2 '
                   'uses them): %s' % ', '.join(loose))
    return log


def axle_zero_shift(net):
    """Every Cars 2 rig has its zero height at the front axle: the hub joints
    and FrontTires_Ctrl / BackTires_Ctrl sit at rig height 0, the ground
    control one axle-height below (Sheriff 0.379, the six-wheeler 0.704), and
    the game places the rig's zero at the character's axle height above the
    road. A model moved to taller wheels (Mack: 0.573 on Sheriff's 0.379)
    keeps the wheels above the zero and floats by the difference. So shift
    everything under Main (the skeleton) and under the control root down by
    the front hubs' height: same shape, wheels at zero. -> the shift"""
    hubs = [net.index('Anchor/%s/Transform' % n) for n in ('TireFLeft_centerJoint', 'TireFRight_centerJoint')
            if net.has('Anchor/%s/Transform' % n)]
    if not hubs or not net.has('Anchor/Main/Transform'):
        return 0.0
    dy = sum(_rig_world(net, i)[1][3] for i in hubs) / len(hubs)
    if abs(dy) < 1e-4:
        return 0.0
    main = net.index('Anchor/Main/Transform')
    roots = [main]
    for i, n in enumerate(net.data):
        if net.path_of(i).split('/')[1] == 'controls' and n['Type'] in ('Transform', 'Dilation'):
            roots.append(i)
    for i, n in enumerate(net.data):
        if n['Type'] not in ('Transform', 'Dilation') or n.get('BasisDataNodeRef') not in roots:
            continue
        pr = _rig_world(net, n['BasisDataNodeRef'])
        down = [-dy * pr[1][k] for k in range(3)]            # world -Y in the parent's axes
        d = n.get('Data') if isinstance(n.get('Data'), dict) else {}
        t = list(d.get('Translation') or (0.0, 0.0, 0.0))
        _set_translation(net, i, [t[k] + down[k] for k in range(3)])
    return dy


def shift_bones_to_axle_zero(tree, dy):
    """The bones' LocalToParentMatrix follows the rig's convention: children
    of Main move down with the rig."""
    st = c2geom.plist(tree['SceneTreeNodePool'])
    for n in st:
        if n.get('Type') != 'Bone' or 'LocalToParentMatrix' not in n:
            continue
        par = st[(n.get('ParentNodeReferences') or [0])[0]]
        if par.get('NodeName') != 'Main':
            continue
        m = [float(x) for x in n['LocalToParentMatrix']]
        m[13] -= dy
        n['LocalToParentMatrix'] = c2oct.FloatList(m)


def apply_retarget_moves(net, moves):
    for path, t, rotor in moves:
        if net.has(path):
            _set_translation(net, net.index(path), t)
            if rotor is not None:
                _set_rotation(net, net.index(path), rotor)
    return ['retarget: %d rig nodes moved' % len(moves)] if moves else []


def apply_skin_moves(tree, skin):
    """Move existing game bones to their new pivots (by the difference)."""
    st = c2geom.plist(tree['SceneTreeNodePool'])
    by_index = dict(enumerate(st))
    delta = {}
    for n in st:
        if n.get('Type') == 'Bone' and n['NodeName'] in skin:
            delta[n['NodeName']] = [skin[n['NodeName']][k] - n['PivotPoint'][k] for k in range(3)]
    # a bone sitting on its parent (zero local offset, same pivot - every
    # TireX_SpinJoint on its centerJoint) stays on it, whatever the model says
    for n in st:
        par = by_index.get((n.get('ParentNodeReferences') or [None])[0])
        if n.get('Type') != 'Bone' or not par or par.get('Type') != 'Bone' or par['NodeName'] not in delta:
            continue
        lt = list(n.get('LocalToParentMatrix') or [0.0] * 16)[12:15]
        if max(abs(x) for x in lt) < 1e-6 and \
                max(abs(n['PivotPoint'][k] - par['PivotPoint'][k]) for k in range(3)) < 1e-5:
            delta[n['NodeName']] = list(delta[par['NodeName']])
    for n in st:
        nm = n.get('NodeName')
        if n.get('Type') != 'Bone' or nm not in delta:
            continue
        d = delta[nm]
        pivot = [float(n['PivotPoint'][k]) + d[k] for k in range(3)]
        m = [float(x) for x in n['BoneToModelMatrix']]
        # v2.0.23: skip only when bind AND PivotPoint are already there (King's port: SpinJoint bind on
        # the wheel, PivotPoint still McQueen's - the pivot was never written)
        if max(abs(m[12 + k] - pivot[k]) for k in range(3)) < 1e-7 and                 max(abs(float(n['PivotPoint'][k]) - pivot[k]) for k in range(3)) < 1e-7:
            continue
        # bind at the pivot, as on every shipping character: a base whose bind
        # was off its pivot (an earlier edit) would skin the tires off the
        # point the rig spins them around - they hop
        for k in range(3):
            m[12 + k] = pivot[k]
        n['BoneToModelMatrix'] = c2oct.FloatList(m)
        R = [m[0:3], m[4:7], m[8:11]]                 # rows = axes (row-vector layout)
        p = m[12:15]
        inv = [[R[c][r] for c in range(3)] for r in range(3)]   # R^T
        t = [-sum(p[j] * inv[j][k] for j in range(3)) for k in range(3)]
        n['BoneToStandardMatrix'] = c2oct.FloatList([inv[0][0], inv[0][1], inv[0][2], 0.0,
                                                     inv[1][0], inv[1][1], inv[1][2], 0.0,
                                                     inv[2][0], inv[2][1], inv[2][2], 0.0, t[0], t[1], t[2], 1.0])
        n['PivotPoint'] = c2oct.FloatList(pivot)
        if 'BoneEndPoint' in n:
            n['BoneEndPoint'] = c2oct.FloatList(float(n['BoneEndPoint'][k]) + d[k] for k in range(3))
    for n in st:                                      # LocalToParent: the move relative to the parent
        nm = n.get('NodeName')
        if n.get('Type') != 'Bone' or 'LocalToParentMatrix' not in n:
            continue
        par = by_index.get((n.get('ParentNodeReferences') or [None])[0])
        dp = delta.get(par.get('NodeName'), [0.0, 0.0, 0.0]) if par else [0.0, 0.0, 0.0]
        dc = delta.get(nm, [0.0, 0.0, 0.0])
        rel = [dc[k] - dp[k] for k in range(3)]
        if max(abs(x) for x in rel) < 1e-7:
            continue
        PR = [list(par['BoneToModelMatrix'])[4 * r:4 * r + 3] for r in range(3)] if par and par.get('Type') == 'Bone' \
            else [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        local = [sum(rel[j] * PR[k][j] for j in range(3)) for k in range(3)]   # into the parent's axes
        m = [float(x) for x in n['LocalToParentMatrix']]
        for k in range(3):
            m[12 + k] += local[k]
        n['LocalToParentMatrix'] = c2oct.FloatList(m)
    return tree


# ------------------------------------------------------------------ animated parts
# A Cars 3 part the Cars 2 base doesn't have, moved by its own clip-animated
# control (Fritter's blade: BladeBase_Ctrl, BladeExtension_Ctrl, Blade_Ctrl on
# cars3.ct ports 10, 11, 9) is rebuilt the Cars 3 way: X_Ctrl_Grp (at the
# bone's place and orientation) -> control (at zero, a clip channel) -> the
# bone X (at zero).
# The game only animates the slots its own clip templates define (tested: a
# slot appended past the shipping 110 of car_frontend_frontend is accepted but
# never animated). So each part control BORROWS a slot the template already
# has and the character leaves empty - one its .bent has a channel for - and
# takes that slot's name (the .bent names the node a slot drives): Fritter's
# blade on Sheriff uses Mater's tow-crane slots. The .bent doesn't change. A
# <name>.cars3_ports.json tells the Cars 3 clip import / convert which Cars 3
# port feeds which borrowed slot.

PART_TEMPLATES = ('car_frontend_frontend', 'car_body')
# Free slots tried first (parts a character rarely has: Mater's crane, arms,
# hands, mirror mounts, Luigi's moustache), then any other free transform slot.
BORROW_FIRST = ('TowCraneBase_Ctrl', 'TowCrane_Ctrl', 'TCable_Pulley_Ctrl', 'ControlArmLeft',
                'ControlArmRight', 'ControlHandLeft', 'ControlHandRight', 'SideMirrorMountLeft_Ctrl',
                'SideMirrorMountRight_Ctrl', 'ControlStacheLeft', 'ControlStacheRight')


def free_part_slots(net, bent):
    """{template: {channel path: slot}} - transform slots of the part
    templates that the .bent has a channel for and the scene leaves empty."""
    out = {}
    if bent is None:
        # no .bent: the template catalog lists every slot and its channel name
        # (the .bent names the same channels - a borrowed slot needs no change)
        for tm in PART_TEMPLATES:
            ls = next((x for x in net.links if x['ct'].endswith('/%s.ct' % tm)), None)
            e = net._ct.get(ls['ct']) if ls is not None else None
            if not e:
                continue
            free = {}
            for j, sl in enumerate(e['slots']):
                nm = sl and sl.get('channel')
                if j < len(ls['nodes']) and ls['nodes'][j] < 0 and nm and nm.endswith('/Transform') \
                        and not net.has(nm):
                    free[nm] = j
            out[tm] = free
        return out
    blend_type = {n: bb.get('Type') for n, bb in bent.blenders}
    float_type = blend_type.get(next((n for n in blend_type if n.endswith('/Spin') or '/Eyes_Ctrl/' in n), None))
    for tm in PART_TEMPLATES:
        ls = next((x for x in net.links if x['ct'].endswith('/%s.ct' % tm)), None)
        if ls is None:
            continue
        try:
            names = bent.channels(tm)
        except Exception:
            continue
        free = {}
        for j, nm in enumerate(names):
            if j < len(ls['nodes']) and ls['nodes'][j] < 0 and nm and nm.endswith('/Transform') \
                    and not net.has(nm) and blend_type.get(nm) != float_type:
                free[nm] = j
        out[tm] = free
    return out


def assign_part_slots(parts, free):
    """Give each part a borrowed channel free in every template that has one
    (frontend, and car_body for a game .bent). Sets part['ctrl'] / ['slots']."""
    tms = [tm for tm in PART_TEMPLATES if free.get(tm)]
    if not tms:
        return [p['bone'] for p in parts]
    common = set.intersection(*[set(free[tm]) for tm in tms])
    order = [n for n in ('Anchor/%s/Transform' % b for b in BORROW_FIRST) if n in common] + \
        sorted(n for n in common if n not in ['Anchor/%s/Transform' % b for b in BORROW_FIRST])
    left = []
    for part in parts:
        if not order:
            left.append(part['bone'])
            continue
        ch = order.pop(0)
        part['ctrl'] = ch
        part['slots'] = {tm: free[tm][ch] for tm in tms}
    return left


def _u16_pairs(a):
    """Little-endian u16s from a byte array - as numbers, bytes, or the
    "base64:..." text offsetting / the importer gives for raw bytes."""
    if isinstance(a, str):
        r = a[len('base64:'):] if a.startswith('base64:') else a
        a = base64.b64decode(r + '=' * (-len(r) % 4))
    a = list(a)
    return [a[2 * i] | (a[2 * i + 1] << 8) for i in range(len(a) // 2)]


def cars3_ports(data):
    """{'Anchor/X_Ctrl/Transform': cars3.ct port} for a Cars 3 model (data in
    offsetting's JSON shape, as the Cars 3 import reads it)."""
    sn = next((x for x in _pool(data.get('SubNetworkPool')) if x.get('BaseDataNodeLinksPool')), None)
    if sn is None:
        return {}
    HS, HI, HL = sn['HeaderStrings'], sn['HeaderStringIndices'], sn['HeaderLayout']
    n = len(HI)
    parent, nxt = [None] * (n + 1), 1
    for k in range(0, len(HL) - 1, 2):
        for _c in range(HL[k + 1]):
            if nxt <= n:
                parent[nxt] = HL[k]
            nxt += 1

    def path(h):
        out, k = [], 0
        while h and 0 < h <= n and k < 32:
            out.append(HS[HI[h - 1]])
            h, k = parent[h], k + 1
        return '/'.join(reversed(out))
    dn = _pool(sn['DataNodePool'])
    cm = list(sn.get('ConnectionMapUsedDataNodes') or [])
    out = {}
    for ls in _pool(sn['BaseDataNodeLinksPool']):
        if not str(ls.get('ConnectionTemplate', '')).endswith('cars3.ct'):
            continue
        loc = _u16_pairs(ls['LocalUsedDataNodeIndices'])
        for port, l in enumerate(_u16_pairs(ls['PortToLocalIndices'])):
            if l != 0xFFFF and l < len(loc) and loc[l] < len(cm):
                out[path(dn[cm[loc[l]]].get('Header', 0))] = port
    return out


def plan_animated_parts(c3_ports, new_bone_names):  # noqa: E302
    """The Cars 3 controls that animate a bone the base lacks -> [{'bone',
    'ctrl', 'grp', 'port'}] (X_Ctrl driving the bone X)."""
    parts = []
    for path, port in sorted(c3_ports.items(), key=lambda kv: (kv[1] is None, kv[1] or 0, kv[0])):
        m = re.match(r'^Anchor/(.+)_Ctrl/Transform$', path)
        if m and m.group(1) in new_bone_names:
            b = m.group(1)
            parts.append({'bone': b, 'port': port, 'ctrl': 'Anchor/%s_Ctrl/Transform' % b,
                          'grp': 'Anchor/%s_Ctrl_Grp/Transform' % b})
    return parts


def _tr_matrix(t, rotor):
    m = _rot_cols(rotor or (0.0, 0.0, 0.0, 1.0))
    for k in range(3):
        m[k][3] = float(t[k])
    return m


def plan_extra_wheels(c3_skeleton, skin, used, override):
    """Extra wheels on another wheel's hub (Mack's TireMidLeft_SpinJoint_OL on
    TireBLeft_centerJoint) get a hub of their own, as Cars 2's six-wheeler
    (bg_explodingminion: TireBLeft2_centerJoint under Main) has: a new hub
    joint at the wheel, under the back hub's parent, with the spin joint under
    it. Changes c3_skeleton / used / override. -> [(spin, new hub, back hub)]"""
    out = []
    for b in sorted(used):
        if '_SpinJoint' not in b or b in skin or b not in c3_skeleton:
            continue
        par = c3_skeleton[b][1]
        own = b.split('_SpinJoint')[0]                       # TireMidLeft
        if not par or not par.endswith('_centerJoint') or par == own + '_centerJoint' or par not in skin:
            continue
        hub = own + '_centerJoint'
        if hub in skin:
            continue
        m = list(c3_skeleton[b][0])
        mat = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0] + m[12:15] + [1.0]
        gp = c3_skeleton[par][1] if par in c3_skeleton else 'Main'
        c3_skeleton[hub] = (mat, gp or 'Main')
        used.add(hub)
        override[b] = hub
        out.append((b, hub, par))
    return out


def native_extra_wheels(tree, net, wheels, warnings):
    """The rest of Cars 2's six-wheel recipe (bg_explodingminion's BLeft2):
    a copy of the back wheel's control chain (BLeft_bttm_Ctrl_Grp ->
    BLeft_steer_Ctrl -> BLeft_bttm_Ctrl -> BLeft_Center_Ctrl_Grp ->
    BLeft_Center_Ctrl) under the same parent (BackTires_Ctrl), moved to the
    extra wheel; the copies of clip-driven controls copy the original's
    ROTATION only (orient) - the extra wheel tilts with the back wheel about
    its own centre, never levered - and the new hub copies its Center control
    (orient + point), like every hub. -> (edits, constraint edits)"""
    st = c2geom.plist(tree['SceneTreeNodePool'])
    pivot = {n['NodeName']: n['PivotPoint'] for n in st if n.get('Type') == 'Bone'}
    linked = set(x for ls in net.links for x in ls['nodes'] if x >= 0)
    edits, cons = [], []
    for spin, hub, back in wheels:
        bp = 'Anchor/%s/Transform' % back
        ci = net.constraint_of(bp) if net.has(bp) else None
        if ci is None:
            warnings.append('%s: %s has no control - the extra wheel has no suspension controls' % (spin, back))
            continue
        pr = net.procs[ci]
        ins = pr['Input'] if isinstance(pr['Input'], list) else [pr['Input']]
        c = ins[0]
        cname = net.path_of(c).split('/')[1]                       # BLeft_Center_Ctrl
        old_tag = cname.split('_')[0]                              # BLeft
        new_tag = hub[len('Tire'):-len('_centerJoint')] if hub.startswith('Tire') else hub.split('_')[0]
        chain, i = [], c
        while i is not None and net.path_of(i).split('/')[1].startswith(old_tag + '_'):
            chain.append(i)
            i = net.data[i].get('BasisDataNodeRef')
        if i is None or not chain:
            continue
        chain.reverse()
        top = i
        prev_path = net.path_of(top)              # the chain hangs where the back wheel's does
        # ... but rides on the back wheel's suspension travel: a follower on the
        # back wheel's bttm control that keeps the axle's (top's) orientation -
        # the game's physics moves the four wheels it knows to the road, and an
        # extra wheel left on the axle would hang above it (in game: higher than
        # the front and back tires); the follower carries it up and down with the
        # back wheel, not its tilt
        bttm = next((x for x in chain if net.path_of(x).split('/')[1].endswith('_bttm_Ctrl')), None)
        follow_world = None
        if bttm is not None:
            fw = _rig_world(net, bttm)
            tw = _rig_world(net, top)
            follow_world = [[tw[r][k] for k in range(3)] + [fw[r][3]] for r in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
            fl = _mul(_inv4(fw), follow_world)
            qx, qy, qz, qw = _quat([row[:3] + [0.0] for row in fl[:3]] + [[0, 0, 0, 1]])
            follow = 'Anchor/%s_Follow/Transform' % (new_tag + net.path_of(bttm).split('/')[1][len(old_tag):])
            edits.append({'op': 'add_node', 'path': follow, 'parent': net.path_of(bttm),
                          'translation': [fl[0][3], fl[1][3], fl[2][3]], 'rotor': [-qx, -qy, -qz, qw]})
            cons.append({'op': 'constraint', 'owner': follow, 'targets': [net.path_of(top)],
                         'ops': [{'op': 'orient', 'input': 0}]})
            prev_path = follow
        for k, ni in enumerate(chain):
            nm = net.path_of(ni).split('/')[1]
            new = 'Anchor/%s/Transform' % (new_tag + nm[len(old_tag):])
            pw = _rig_world(net, net.data[ni]['BasisDataNodeRef'])
            local = _mul(_inv4(pw), _rig_world(net, ni))
            qx, qy, qz, qw = _quat([row[:3] + [0.0] for row in local[:3]] + [[0, 0, 0, 1]])
            t = [local[0][3], local[1][3], local[2][3]]
            if k == 0:                             # moved by the extra wheel's offset from the back one
                d = WHEEL_DELTA.get(spin, [0.0, 0.0, 0.0])
                if follow_world is not None:       # placed relative to the follower
                    want = _rig_world(net, ni)
                    want = [[want[r2][c2] for c2 in range(3)] + [want[r2][3] + d[r2]] for r2 in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
                    loc2 = _mul(_inv4(follow_world), want)
                    qx, qy, qz, qw = _quat([row[:3] + [0.0] for row in loc2[:3]] + [[0, 0, 0, 1]])
                    t = [loc2[0][3], loc2[1][3], loc2[2][3]]
                else:
                    ld = [sum(pw[r2][c2] * d[r2] for r2 in range(3)) for c2 in range(3)]   # into the parent's axes
                    t = [t[j] + ld[j] for j in range(3)]
            edits.append({'op': 'add_node', 'path': new, 'parent': prev_path, 'translation': t,
                          'rotor': [-qx, -qy, -qz, qw]})
            w = net.writer_of(ni)
            if ni in linked or (w is not None and net.procs[w]['Type'] == 'UberConstraintProcessor'):
                cons.append({'op': 'constraint', 'owner': new, 'targets': [net.path_of(ni)],
                             'ops': [{'op': 'orient', 'input': 0}]})
            prev_path = new
        cons.append({'op': 'constraint', 'owner': 'Anchor/%s/Transform' % hub, 'targets': [prev_path],
                     'ops': [{'op': 'orient', 'input': 0}, {'op': 'point', 'input': 0}]})
    return edits, cons


WHEEL_DELTA = {}            # spin joint -> its offset from the back wheel (set by build_geometry)


def level_extra_spins(edits, net):
    """Extra wheels hung on another wheel's hub (Mack's middle tires on the
    back hubs, 1.24 in front): Cars 2 clips pitch the back hubs (a launch's
    B*_bttm squat - up to 9 deg in shared_quickstart_go2), and anything 1.24
    away swings like a lever - the middle tires float. Each gets a helper on
    the hub that keeps the hub's parent's orientation (Main - the chassis),
    so the extra wheel follows the hub's position (suspension, squat) but not
    its tilt, and still spins by its own driven key.
    -> (edits, constraint edits for after the rest)"""
    out, cons = list(edits), []
    for e in list(edits):
        if e.get('op') != 'add_node':
            continue
        bone = e['path'].split('/')[1]
        hub = e['parent'].split('/')[1] if e.get('parent') else ''
        if '_SpinJoint' not in bone or not hub.endswith('_centerJoint') or not net.has(e['parent']):
            continue
        if bone.split('_SpinJoint')[0] == hub[:-len('_centerJoint')]:
            continue                                  # the hub's own spin joint
        hi = net.index(e['parent'])
        pi = net.data[hi].get('BasisDataNodeRef')
        if pi is None:
            continue
        Wh = _rig_world(net, hi)
        Wp = _rig_world(net, pi)
        rh = [[sum(Wh[k][r] * Wp[k][c] for k in range(3)) for c in range(3)] for r in range(3)]   # Wh^T Wp
        H = [rh[0] + [0.0], rh[1] + [0.0], rh[2] + [0.0], [0.0, 0.0, 0.0, 1.0]]
        want = _mul(Wh, _tr_matrix(e['translation'], e.get('rotor')))
        local = _mul(_inv4(_mul(Wh, H)), want)
        qx, qy, qz, qw = _quat([row[:3] + [0.0] for row in rh] + [[0, 0, 0, 1]])
        lx, ly, lz, lw = _quat([row[:3] + [0.0] for row in local[:3]] + [[0, 0, 0, 1]])
        helper = 'Anchor/%s_Level/Transform' % bone
        k = out.index(e)
        out[k:k + 1] = [{'op': 'add_node', 'path': helper, 'parent': e['parent'],
                         'translation': [0.0, 0.0, 0.0], 'rotor': [-qx, -qy, -qz, qw]},
                        dict(e, parent=helper, translation=[local[0][3], local[1][3], local[2][3]],
                             rotor=[-lx, -ly, -lz, lw])]
        cons.append({'op': 'constraint', 'owner': helper, 'targets': [net.path_of(pi)],
                     'ops': [{'op': 'orient', 'input': 0}]})
    return out, cons


def control_part_edits(edits, parts, net):
    """Build each part the way Cars 2 builds its face: the bone stays in the
    skeleton (its rig node under its skeleton parent, as planned), and a
    separate X_Ctrl_Grp -> control chain follows what moves that parent; an
    orient + point constraint makes the bone copy its control (Jaw <- Jaw_Ctrl).
    (The game places a bone from its skeleton parent and its own rig node, so a
    bone hung under a clip-driven control never sees the animation.)
    -> (edits, constraint edits for after the chains)"""
    by_path = {e['path']: e for e in edits if e.get('op') == 'add_node'}
    ctrl_of = {'Anchor/%s/Transform' % p['bone']: p['ctrl'] for p in parts}
    out, cons = list(edits), []
    for part in parts:
        bp = 'Anchor/%s/Transform' % part['bone']
        e = by_path.get(bp)
        if e is None:
            continue
        P = e['parent']
        local = _tr_matrix(e['translation'], e.get('rotor'))
        if P in ctrl_of:                      # under another part: its control sits on that bone
            cp, gl = ctrl_of[P], local
        else:
            cp = P
            if net.has(P):                    # follow what drives the parent (FrontEnd <- FrontEnd_Ctrl)
                ci = net.constraint_of(P)
                if ci is not None:
                    pr = net.procs[ci]
                    ins = pr['Input'] if isinstance(pr['Input'], list) else [pr['Input']]
                    if ins and ins[0] is not None and ins[0] >= 0:
                        cp = net.path_of(ins[0])
            if cp != P and net.has(P) and net.has(cp):
                want = _mul(_rig_world(net, net.index(P)), local)
                gl = _mul(_inv4(_rig_world(net, net.index(cp))), want)
            else:
                gl = local
        qx, qy, qz, qw = _quat([row[:3] + [0.0] for row in gl[:3]] + [[0, 0, 0, 1]])
        grp = {'op': 'add_node', 'path': part['grp'], 'parent': cp,
               'translation': [gl[0][3], gl[1][3], gl[2][3]], 'rotor': [-qx, -qy, -qz, qw]}
        ctrl = {'op': 'add_node', 'path': part['ctrl'], 'parent': part['grp'],
                'translation': [0.0, 0.0, 0.0], 'rotor': [0.0, 0.0, 0.0, 1.0]}
        k = out.index(e)
        out[k:k] = [grp, ctrl]                # before the bone; the bone stays as planned
        cons.append({'op': 'constraint', 'owner': bp, 'targets': [part['ctrl']],
                     'ops': [{'op': 'orient', 'input': 0}, {'op': 'point', 'input': 0}]})
    return out, cons


def add_part_channels(net, bent, parts):
    """Connect each part control to its borrowed slot(s). The .bent already
    has those channels, so it isn't changed. -> ({port: {template: [slot,
    name]}}, log)"""
    ports, log = {}, []
    for part in parts:
        if not part.get('slots') or not net.has(part['ctrl']):
            continue
        for tm, slot in part['slots'].items():
            ls = next(x for x in net.links if x['ct'].endswith('/%s.ct' % tm))
            net.link(ls['ct'], part['ctrl'], part['ctrl'])
        if part.get('port') is not None:          # (a Cars 3 part: for the Cars 3 clip converter)
            ports[str(part['port'])] = {tm: [slot, part['bone']] for tm, slot in part['slots'].items()}
        log.append('animated part %s: %s%s slot (%s)'
                   % (part['bone'], '' if part.get('port') is None else 'Cars 3 port %d -> ' % part['port'],
                      part['ctrl'].split('/')[1],
                      ', '.join('%s %d' % (t, sl) for t, sl in part['slots'].items())))
    return ports, log


# ------------------------------------------------------------------ dynamic chains
# What makes Mater's tow hook swing: a DynamicChain processor that simulates
# a line of rig nodes (parent to child, root first) every frame from how the
# character moves - so it reacts to every animation. Its settings sit in a
# NetworkBuffer block, decoded from Mater's tow cable and Fritter's Cars 3
# chains (which store the same settings by name):
#   header : GlobalDamping f32, IterationRate f32, ContainerExtent 3 x f32, count i32
#   per link: ConfigFlags i32, InverseMass, NaturalFrequency, DampingFactor,
#             DragCoefficient, 1.0, 0.0 (f32; the last two as on Mater)
# ConfigFlags: low two bits the constraint (1 rigidly attached - the root,
# 2 relative target: the link springs back towards its rest place relative
# to its parent with NaturalFrequency / DampingFactor), +4 gravity.
# Each chain also has a Bool "lazy" group node (1) that the chain bones'
# scene links name, as on Mater.

MATER_CHAIN = {'global_damping': 1.0, 'iteration_rate': 300.0, 'frequency': 4.0,
               'damping': 0.5, 'drag': 0.0, 'inverse_mass': None}    # None: 0.4 -> 0.1 along the chain


MATER_INVERSE_MASS = (0.4, 0.3, 0.25, 0.2, 0.1)        # root .. hook


def _mater_mass(f):
    """Mater's inverse masses along his cable, at fraction f (0 root, 1 tip)."""
    m = MATER_INVERSE_MASS
    x = f * (len(m) - 1)
    i = min(int(x), len(m) - 2)
    return m[i] + (m[i + 1] - m[i]) * (x - i)


def chain_block(chain):
    n = len(chain['bones'])
    out = struct.pack('<5fi', float(chain.get('global_damping', 1.0)), float(chain.get('iteration_rate', 300.0)),
                      0.0, 0.0, 0.0, n)
    for k in range(n):
        im = chain.get('inverse_mass')
        if im is None:                                   # Mater's curve, stretched over n links
            im = _mater_mass(k / (n - 1)) if n > 1 else MATER_INVERSE_MASS[0]
        out += struct.pack('<i6f', 5 if k == 0 else 6, float(im), float(chain.get('frequency', 4.0)),
                           float(chain.get('damping', 0.5)), float(chain.get('drag', 0.0)), 1.0, 0.0)
    return out


def chain_paths(chain):
    tag = re.sub(r'[^A-Za-z0-9_]', '_', chain['name'])
    return ('Anchor/AvaDynamicChainConstraintNode_%s/DynamicChain' % tag,
            'Anchor/Lazy_%sChainGroup' % tag)


def add_dynamic_chain(net, chain):
    """The DynamicChain over chain['bones'] (root first, already a parent-to-
    child line of rig nodes), its lazy group node and its NetworkBuffer block."""
    nodes = ['Anchor/%s/Transform' % b for b in chain['bones']]
    missing = [p for p in nodes if not net.has(p)]
    if missing:
        raise ValueError('chain %s: no rig node %s' % (chain['name'], missing[0]))
    # the links stored as Mater's are: a translation down the parent's Z, an
    # identity OrientProxy, InheritScale 0 - the chain writes the links'
    # rotations itself, so any turn has to sit in the mount above (chain_mounts)
    path, lazy = chain_paths(chain)
    if not net.has(lazy):
        net.add_data(lazy, 'Bool', data=1)
    i = net.add_processor(path, 'DynamicChain', LazyDataNode=lazy, ChainNode=nodes)
    block = chain_block(chain)
    net._grow_buffer(len(block), net.procs[i], block)
    return '%s: dynamic chain of %d links (frequency %.3g, damping %.3g)' % (
        chain['name'], len(nodes), chain.get('frequency', 4.0), chain.get('damping', 0.5))


def chain_mounts(edits, chains):
    """Mater's tow cable hangs from a mount (TCable_Base) that carries the
    turn (its Z points down), and every link under it has identity rotation
    - the chain writes the links' rotations, so a turn stored in the root
    link itself is lost (Fritter's pendulum then pointed forward, and the
    plates chain copying it turned 90 deg). Put a mount above each chain's
    root with the root's place and turn; the root sits on it unrotated."""
    out = list(edits)
    for chain in chains:
        root = 'Anchor/%s/Transform' % chain['bones'][0]
        e = next((x for x in out if x.get('op') == 'add_node' and x['path'] == root), None)
        if e is None:
            continue
        mount = 'Anchor/%s_Base/Transform' % chain['bones'][0]
        k = out.index(e)
        out[k:k + 1] = [dict(e, path=mount),
                        dict(e, parent=mount, translation=[0.0, 0.0, 0.0], rotor=[0.0, 0.0, 0.0, 1.0])]
    return out


def aim_chain(c3_skeleton, chain):
    """Face every chain bone the way the game's chain expects - as on Mater,
    whose cable links each have the next link exactly on their local +Z: +Z
    at the next link (the tip keeps the last direction), X kept as close to
    the original as possible so the link doesn't spin about the chain.
    Positions don't change, and the rig node and the game bone get the same
    frame, so the mesh stays exactly where it is. Changes c3_skeleton."""
    names = chain['bones']
    pos = [c3_skeleton[b][0][12:15] for b in names]
    z = None
    for k, b in enumerate(names):
        m, parent = c3_skeleton[b]
        if k + 1 < len(names):
            d = [pos[k + 1][i] - pos[k][i] for i in range(3)]
            if math.sqrt(_dot(d, d)) > 1e-6:
                z = _unit(d)
        if z is None:
            continue
        ref = m[0:3]                                   # the original X axis (row-vector layout)
        x = [ref[i] - z[i] * _dot(ref, z) for i in range(3)]
        if _dot(x, x) < 1e-8:
            ref = m[4:7]
            x = [ref[i] - z[i] * _dot(ref, z) for i in range(3)]
        x = _unit(x)
        y = _cross(z, x)
        c3_skeleton[b] = (list(x) + [0.0] + list(y) + [0.0] + list(z) + [0.0] + list(m[12:15]) + [1.0], parent)


def link_chains_lazy(tree, chains):
    """Name each chain's lazy group in its bones' scene links, as on Mater."""
    for chain in chains:
        lazy = chain_paths(chain)[1].replace('/', '|')
        want = {'Anchor|%s' % b for b in chain['bones']}
        for a in c2geom.plist(tree.get('AssociationPool')):
            if a.get('Type') == 'Bone' and a.get('NodeName') in want:
                a['LazyDataNodeName'] = lazy


def _num_suffix(name):
    m = re.search(r'(\d+)$', name)
    return int(m.group(1)) if m else -1


def order_chain(names):
    """Root to tip: by the number at the end of the name (Proxy01 ... Proxy010)."""
    return sorted(names, key=lambda n: (_num_suffix(n), n))


def find_cars3_chains(data, skin_names):
    """Fritter-style Cars 3 chains -> [{'name', 'bones', 'anchor', settings}].
    A Cars 3 chain simulates two points: a fixed anchor (its AnchorToWorld
    frame, e.g. chainLeft_Start) and one swinging point; only the proxy bones
    fed from the swinging point move with it (Fritter: chainLeft_Proxy01), the
    others are fixed to the body (Proxy02..010). So the Cars 2 chain is the
    anchor -> the swinging proxies, and the rest just ride along.
    'anchor': the anchor frame's name - a bone of that name is the chain's
    root (the Cars 3 import makes one). Settings from the chain's
    DynamicChainData and swinging ChainParticle."""
    sn = next((x for x in _pool(data.get('SubNetworkPool')) if x.get('DataNodePool')), None)
    if sn is None:
        return []
    HS, HI, HL = sn['HeaderStrings'], sn['HeaderStringIndices'], sn['HeaderLayout']
    dn = _pool(sn['DataNodePool'])
    owners, cum = [], 2
    for i in range(0, len(HL) - 1, 2):
        h, c = HL[i], HL[i + 1]
        owners.append((cum, c, HS[HI[h]] if 0 <= h < len(HI) and 0 <= HI[h] < len(HS) else None))
        cum += c

    def owner(h):
        for st_, c, nm in owners:
            if st_ <= h < st_ + c:
                return nm
        return None
    bases = _pool(sn.get('BasisPool'))
    frame_names = [b.get('Name') for b in bases]
    def outputs(p, refs):
        """Which refs a processor writes: 'Output'; a ...Compose writes its
        'Aggregate' from the components, a ...Decompose the components."""
        t = str(p.get('Type'))
        if t.endswith('Decompose'):
            return {a for a, _r in refs if a != 'Aggregate'}
        if t.endswith('Compose'):
            return {'Aggregate'}
        return {'Output'}
    writer = {}
    for p in _pool(sn.get('ProcessorNodePool')):
        refs = list(zip(p.get('DataNodeAttributes') or [], p.get('DataNodeRefs') or []))
        outs = outputs(p, refs)
        for a, r in refs:
            if a in outs:
                writer[r] = [rr for aa, rr in refs if aa not in outs]

    procs_ = _pool(sn.get('ProcessorNodePool'))
    out_by = {}
    for p in procs_:
        refs = list(zip(p.get('DataNodeAttributes') or [], p.get('DataNodeRefs') or []))
        outs = outputs(p, refs)
        for a, r in refs:
            if a in outs:
                out_by[r] = p.get('Type')

    def turns(r, depth=0, seen=None):
        """Is this node's orientation taken from an aim (the chain's swing)?"""
        seen = seen if seen is not None else set()
        if r in seen or depth > 8 or not 0 <= r < len(dn):
            return False
        seen.add(r)
        if out_by.get(r) == 'AimConstraintProcessorNode':
            return True
        return any(turns(x, depth + 1, seen) for x in writer.get(r, ()))

    def swings(r, depth=0, seen=None):
        """Is this node fed from a dynamic chain particle's frame?"""
        seen = seen if seen is not None else set()
        if r in seen or depth > 6 or not 0 <= r < len(dn):
            return False
        seen.add(r)
        b = dn[r].get('BasisRef')
        if b is not None and 0 <= b < len(frame_names) and 'DynamicChainConstraintNode' in str(frame_names[b]):
            return True
        return any(swings(x, depth + 1, seen) for x in writer.get(r, ()))
    node_of = {}
    for i, n in enumerate(dn):
        node_of.setdefault(owner(n.get('Header', -1)), []).append(i)
    chains = []
    for p in _pool(sn.get('ProcessorNodePool')):
        if p.get('Type') != 'DynamicChain' or 'DataNodeRefs' not in p:
            continue
        refs = dict(zip(p.get('DataNodeAttributes') or [], p['DataNodeRefs']))
        cont = owner(dn[refs['Container']].get('Header', -1)) if 'Container' in refs else None
        anchor = owner(dn[refs['AnchorToWorld']].get('Header', -1)) if 'AnchorToWorld' in refs else None
        if not cont:
            continue
        prefix = re.sub(r'\d+$', '', cont)
        proxies = order_chain([b for b in skin_names if b.startswith(prefix) and re.fullmatch(r'\d+', b[len(prefix):] or 'x')])
        moving = [b for b in proxies if any(swings(i) for i in node_of.get(b, ()))]
        if not moving:
            continue
        bones = ([anchor] if anchor else []) + moving
        cd = dn[refs['ChainData']] if 'ChainData' in refs else {}
        parts = [dn[v] for k, v in refs.items() if k.startswith('Particle')]
        swing = next((x for x in parts if x.get('ConfigFlags', 0) & 3 == 2), parts[-1] if parts else {})
        name = re.sub(r'_?Proxy$', '', prefix.rstrip('_')) or prefix.rstrip('_')
        rest = [b for b in proxies if b not in moving]
        # The Cars 3 pendulum: the swinging particle hangs RelativeTarget from the
        # anchor (Fritter: 0.7 straight down), and the moving proxies ride on it
        # with their offset. A chain run straight from the anchor to Proxy01
        # instead lies sideways (1.07 on her right chain), and gravity swings it
        # hard the moment the model appears.
        particle = None
        rt = swing.get('RelativeTarget')
        if anchor and isinstance(rt, (list, tuple)) and len(rt) >= 3 and any(abs(float(x)) > 1e-6 for x in rt):
            particle = {'name': name + '_Particle', 'offset': [float(x) for x in rt[:3]]}
            bones = [anchor, particle['name']]
        chains.append({'name': name, 'bones': bones, 'anchor': anchor,
                       'particle': particle, 'riders': moving if particle else [],
                       # bones that stay in place but turn with the swing (Cars 3:
                       # their orientation is the chain's aim) - and those fixed
                       'followers': [b for b in rest if any(turns(i) for i in node_of.get(b, ()))],
                       'fixed': [b for b in rest if not any(turns(i) for i in node_of.get(b, ()))],
                       'global_damping': float(cd.get('GlobalDamping', 1.0)),
                       'iteration_rate': float(cd.get('IterationRate', 300.0)),
                       'frequency': float(swing.get('NaturalFrequency', 4.0)),
                       'damping': float(swing.get('DampingFactor', 0.5)),
                       'drag': float(swing.get('DragCoefficient', 0.0)),
                       'inverse_mass': float(swing.get('InverseMass', 1.0)),
                       'source': 'Cars 3'})
    return chains


# ------------------------------------------------------------------ normals / tangents
def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a, fallback=(0.0, 1.0, 0.0)):
    l = math.sqrt(_dot(a, a))
    return (a[0] / l, a[1] / l, a[2] / l) if l > 1e-12 else fallback


def ensure_normals(verts, indices):
    """Fill in 'Normal' (angle-weighted over the faces around each vertex)
    where the vertices have none. Vertices that are not shared between hard
    edges give hard edges, as in the Cars 3 file."""
    if not verts or 'Normal' in verts[0]:
        return
    acc = [[0.0, 0.0, 0.0] for _ in verts]
    for f in range(0, len(indices) - 2, 3):
        ids = indices[f:f + 3]
        p = [verts[i]['Position'] for i in ids]
        n = _cross(_sub(p[1], p[0]), _sub(p[2], p[0]))
        ln = math.sqrt(_dot(n, n))
        if ln < 1e-14:
            continue
        n = (n[0] / ln, n[1] / ln, n[2] / ln)
        for k in range(3):
            e1, e2 = _unit(_sub(p[(k + 1) % 3], p[k])), _unit(_sub(p[(k + 2) % 3], p[k]))
            ang = math.acos(max(-1.0, min(1.0, _dot(e1, e2))))
            for c in range(3):
                acc[ids[k]][c] += n[c] * ang
    for v, a in zip(verts, acc):
        v['Normal'] = _unit(a)


def ensure_tangents(verts, indices):
    """Fill in 'Tangent' (along +U) and 'Binormal' (along +V) for layouts that
    store them - checked against the game's own values on the Cars 2 tires
    (tangent agrees to 0.997 on average). Needs 'Normal' and 'Uv1'."""
    if not verts or ('Tangent' in verts[0] and 'Binormal' in verts[0]):
        return
    T = [[0.0, 0.0, 0.0] for _ in verts]
    B = [[0.0, 0.0, 0.0] for _ in verts]
    for f in range(0, len(indices) - 2, 3):
        a, b, c = indices[f:f + 3]
        p0, p1, p2 = verts[a]['Position'], verts[b]['Position'], verts[c]['Position']
        u0, u1, u2 = verts[a]['Uv1'], verts[b]['Uv1'], verts[c]['Uv1']
        e1, e2 = _sub(p1, p0), _sub(p2, p0)
        du1, dv1, du2, dv2 = u1[0] - u0[0], u1[1] - u0[1], u2[0] - u0[0], u2[1] - u0[1]
        r = du1 * dv2 - du2 * dv1
        if abs(r) < 1e-12:
            continue
        r = 1.0 / r
        for k in (a, b, c):
            for i in range(3):
                T[k][i] += (e1[i] * dv2 - e2[i] * dv1) * r
                B[k][i] += (e2[i] * du1 - e1[i] * du2) * r
    for v, t_, b_ in zip(verts, T, B):
        n = _unit(v['Normal'][:3])
        t = tuple(t_[i] - n[i] * _dot(n, t_) for i in range(3))
        if _dot(t, t) < 1e-14:                       # no usable UV area: any direction across the normal
            t = _cross(n, (0.0, 1.0, 0.0) if abs(n[1]) < 0.9 else (1.0, 0.0, 0.0))
        t = _unit(t)
        b = tuple(b_[i] - n[i] * _dot(n, b_) for i in range(3))
        if _dot(b, b) < 1e-14:
            b = _cross(n, t)
        b = _unit(b)
        v['Tangent'] = t + (0.0,)
        v['Binormal'] = b + (0.0,)


# ------------------------------------------------------------------ geometry
def build_geometry(tree, parts, offset=(0.0, 0.0, 0.0), keep=(), vb=None, ib=None, c3_skeleton=None,
                   add_bones=(), chains=(), c3_pose=None, node_of_bone=None, c3_ports=None,
                   bent=None, all_tangents=True, no_turn=()):
    """Parts (Cars 3 meshes: {'material': Cars 3 material name, 'verts': [{'Position', 'Uv1' (the
    game's u, v), optional 'Normal', '_bones': [(bone, weight)]}], 'indices': triangles}) -> geometry for
    c2geom.rebuild on the Cars 2 `tree`.

    offset: added to every position. keep: Cars 2 material names whose
    original meshes stay as they are (needs vb, ib: the Cars 2 buffers).
    c3_skeleton: {bone: (row-vector flat 16 matrix in Cars 2 space, parent)} of
    the Cars 3 skeleton - weights to bones the Cars 2 base lacks then keep
    their bone, added as a new bone (see plan_new_bones); without it they go
    to the body.
    add_bones: bones to add whether or not vertices use them (Add Selected
    Bones to Rig in the Cars 3 panel).
    chains: dynamic chains [{'name', 'bones' (root first), settings}] - their
    bones are added in a parent-to-child line and get a DynamicChain.
    c3_pose: {bone: row-vector flat 16 matrix} of every bone of the Cars 3
    import - when given, the base's skeleton, controls and FX markers are
    moved to those places (plan_retarget; node_of_bone maps differing names),
    and offset should be zero.
    -> (geometry or None, notes, problems, warnings)"""
    info = template_info(tree)
    st, c2mats, tmpl, skin = info['st'], info['materials'], info['nodes'], info['skin']
    vs = c2geom.plist(tree['VertexStreamPool'])
    notes, problems, warnings = [], [], []
    keep_idx = {i for i, nm in enumerate(c2mats) if str(nm).lower() in {str(k).lower() for k in keep}}
    if keep_idx and (vb is None or ib is None):
        raise ValueError('keeping Cars 2 meshes needs the Cars 2 vbuf/ibuf')

    groups = {}
    skipped = {}
    new_materials, new_index, new_template = [], {}, {}
    usable = sorted(i for i in tmpl if i not in keep_idx)
    owner = {}                                   # Cars 2 material -> the Cars 3 material on it
    exact = {}                                   # base materials by name: exporting onto an earlier
    for i, nm in enumerate(c2mats):              # export's output, its Cars 3 materials are already there
        exact.setdefault(str(nm).lower(), i)
    by_name = set()
    for part in parts:
        c3key = re.sub(r'\.\d{3}$', '', str(part['material'])).lower()
        if c3key not in exact and material_kind(part['material']) is not None:
            # the Cars 2 material's name in the material or object name
            # (mesh Cars3_McQueen_Eyeshader -> eyeshader); shadows stay out
            named = name_match(part, c2mats)
            if named is not None:
                c3key = str(c2mats[named]).lower()
                line = '%s (%s) -> %s: matched by name' % (part['material'], part.get('name', '?'), c2mats[named])
                if line not in by_name:
                    by_name.add(line)
                    notes.append(line)
        if c3key in exact and exact[c3key] in tmpl:
            mi, left_out = exact[c3key], False
        elif c3key in exact and usable:
            # the base has this material but no mesh for it: a new mesh node
            # on that same material (no duplicate material)
            mi = exact[c3key]
            if mi not in new_template:
                new_template[mi] = template_material(part['material'], c2mats, usable)
            groups.setdefault(mi, []).append(part)
            continue
        else:
            mi, left_out = pick_material(part['material'], c2mats)
        if left_out:
            skipped[part['material']] = skipped.get(part['material'], 0) + 1
            continue
        c3name = re.sub(r'\.\d{3}$', '', str(part['material']))
        if mi is not None and mi in tmpl and owner.setdefault(mi, c3name) != c3name:
            # a second Cars 3 material on the same Cars 2 one (Orange_Glass and
            # Glass -> Glass): its own copy, so each keeps its own texture
            if c3name not in new_index:
                new_index[c3name] = len(c2mats) + len(new_materials)
                new_template[new_index[c3name]] = mi
                new_materials.append({'name': c3name, 'template': mi})
                notes.append('%s: %s is already used by %s - added as a new material (a copy of %s) '
                             'with its own mesh node' % (c3name, c2mats[mi], owner[mi], c2mats[mi]))
            groups.setdefault(new_index[c3name], []).append(part)
            continue
        if mi is None or mi not in tmpl:
            name = re.sub(r'\.\d{3}$', '', str(part['material']))     # Blender's .001
            if not ADD_UNMATCHED_MATERIALS:
                problems.append('%s: no matching material on the Cars 2 character (it has: %s)'
                                % (name, ', '.join(n for n in c2mats if n)))
                continue
            if name not in new_index:
                t_mi = template_material(name, c2mats, usable)
                if t_mi is None:
                    problems.append('%s: no material on the Cars 2 character to start it from' % name)
                    continue
                new_index[name] = len(c2mats) + len(new_materials)
                new_template[new_index[name]] = t_mi
                new_materials.append({'name': name, 'template': t_mi})
                notes.append('%s: no Cars 2 counterpart - added as a new material (a copy of %s, '
                             'edit it in the .oct.json) with its own mesh node' % (name, c2mats[t_mi]))
            mi = new_index[name]
        if mi in keep_idx:
            notes.append('%s: Cars 2 mesh kept' % c2mats[mi])
            continue
        groups.setdefault(mi, []).append(part)
    names_of = list(c2mats) + [nm['name'] for nm in new_materials]
    for nm, cnt in skipped.items():
        notes.append('%s: left out (%d mesh(es))' % (nm, cnt))
    if problems:
        return None, notes, problems, warnings

    missing_bones, unweighted = {}, 0
    meshes = []
    used_names = set()
    # Tangents on every mesh: the base's mesh nodes come with either
    # Tangent+Normal+Binormal or Normal only in their second stream (Sheriff's
    # body and tires vs his glass, eyes and lights); node settings (RenderCaps
    # ...) are the same either way, the stream declares the format. With
    # all_tangents every exported mesh takes the tangent layout.
    tangent_node = None
    for ni_, n_ in enumerate(st):
        refs_ = n_.get('VertexStreamReferences')
        if refs_ is not None and {'Tangent', 'Binormal', 'Normal'} <= {
                e['Name'] for k in refs_ for e in vs[k]['Elements'].values()}:
            tangent_node = ni_
            break
    if all_tangents and tangent_node is None:
        notes.append('no base mesh has a Tangent/Binormal layout to copy - meshes keep their own layout')
    for mi, plist_ in sorted(groups.items()):
        new = mi in new_template
        node = tmpl[new_template[mi]] if new else tmpl[mi]
        verts, indices = [], []
        for part in plist_:
            base = len(verts)
            for v in part['verts']:
                w = dict(v)
                w['Position'] = tuple(v['Position'][k] + offset[k] for k in range(3))
                bones = []
                for b, wt in v.get('_bones', []):
                    if b in skin or (c3_skeleton and b in c3_skeleton):
                        bones.append((b, wt))
                    else:
                        missing_bones[b] = missing_bones.get(b, 0) + 1
                if not bones:
                    bones = [('Body' if 'Body' in skin else sorted(skin)[0], 1.0)]
                    unweighted += 1
                w['_bones'] = bones
                verts.append(w)
            indices += [i + base for i in part['indices']]
        layout_node = node
        if all_tangents and tangent_node is not None:
            layout_node = tangent_node                 # every mesh: Tangent + Normal + Binormal
        needs = {e['Name'] for k in st[layout_node]['VertexStreamReferences'] for e in vs[k]['Elements'].values()}
        ensure_normals(verts, indices)
        if 'Tangent' in needs or 'Binormal' in needs:
            ensure_tangents(verts, indices)
        # named after the model's own mesh (the Cars 3 material it holds), not
        # the base's (bodyMesh_Sheriff_Tires) - mesh node names are referenced
        # nowhere else in the file, so this is only for whoever reads it
        own = []
        for p_ in plist_:
            nm_ = re.sub(r'\.\d{3}$', '', str(p_['material']))
            if nm_ not in own:
                own.append(nm_)
        mesh_name = 'bodyMesh_' + '_'.join(own)
        while mesh_name in used_names:
            mesh_name += '_'
        used_names.add(mesh_name)
        mesh = {'name': mesh_name,
                'material': mi, 'template': node, 'verts': verts, 'indices': indices}
        if layout_node != node:
            mesh['layout'] = layout_node               # streams laid out like that node
        if not new:
            mesh['node'] = node                    # replaces the base's mesh; a new one gets a new node
        meshes.append(mesh)
        notes.append('%s <- %s (%d vertices, %d triangles)'
                     % (names_of[mi], ', '.join(p['material'] for p in plist_), len(verts), len(indices) // 3))
    for nm in sorted(missing_bones):
        warnings.append('bone %s is not on the Cars 2 skeleton: %d weight(s) dropped' % (nm, missing_bones[nm]))
    if unweighted:
        warnings.append('%d vertices had no usable weights and ride on the body' % unweighted)
    for mi in sorted(set(tmpl) - set(groups) - keep_idx):
        # v1.97.2: expected - your model replaces the base's meshes (report only)
        notes.append('%s: none of your meshes use this base material - the base mesh is removed' % c2mats[mi])

    if keep_idx:
        kept = [m for m in c2geom.extract_meshes(tree, vb, ib) if m['material'] in keep_idx]
        meshes += kept
    if not meshes:
        problems.append('nothing to export')
        return None, notes, problems, warnings
    new_bones, edits = [], []
    used = {b for m in meshes for v in m['verts'] for b, _w in v.get('_bones', [])}
    late_edits = []
    if chains and c3_skeleton:
        c3_skeleton = dict(c3_skeleton)
        for chain in chains:
            if all(b in c3_skeleton for b in chain['bones']):
                aim_chain(c3_skeleton, chain)
                # Followers keep their place and turn with the swing: same rest
                # rotation as the chain's tip, then a plain orient constraint
                # from it (exact, no offset needed); the mesh doesn't move.
                tip = chain['bones'][-1]
                rot = c3_skeleton[tip][0]
                for b in chain.get('followers', ()):
                    if b in c3_skeleton and b not in skin:
                        m, par = c3_skeleton[b]
                        c3_skeleton[b] = (list(rot[:12]) + list(m[12:16]), par)
                        used.add(b)
                        late_edits.append({'op': 'constraint', 'owner': 'Anchor/%s/Transform' % b,
                                           'targets': ['Anchor/%s/Transform' % tip],
                                           'ops': [{'op': 'orient', 'input': 0}]})
                if chain.get('followers'):
                    notes.append('chain %s: %d bones turn with %s' % (chain['name'], len(chain['followers']), tip))
    override = {}
    for chain in chains:
        for k, b in enumerate(chain['bones']):
            if b in skin:
                problems.append('chain %s: %s is already on the Cars 2 skeleton - a chain needs its own '
                                'new bones' % (chain['name'], b))
            elif not c3_skeleton or b not in c3_skeleton:
                problems.append('chain %s: %s is not a skin bone of the model' % (chain['name'], b))
            else:
                used.add(b)
                if k:
                    override[b] = chain['bones'][k - 1]          # a line, root first
    for b in add_bones:
        if b in skin:
            notes.append('%s: already on the Cars 2 skeleton - nothing to add' % b)
        elif not c3_skeleton or b not in c3_skeleton:
            problems.append('%s: added to the rig, but it is not a skin bone of the model' % b)
        else:
            used.add(b)
    if problems:
        return None, notes, problems, warnings
    extra_wheels = []
    if c3_skeleton:
        c3_skeleton = dict(c3_skeleton)
        extra_wheels = plan_extra_wheels(c3_skeleton, skin, used, override)
        bones_ = {n['NodeName']: n for n in st if n.get('Type') == 'Bone'}
        for spin_, hub_, back_ in extra_wheels:
            # measured between the model's own hubs (where the skeleton is moved to)
            pv = c3_skeleton[back_][0][12:15] if back_ in c3_skeleton else bones_[back_]['PivotPoint']
            WHEEL_DELTA[spin_] = [c3_skeleton[hub_][0][12 + k] - pv[k] for k in range(3)]
    extra = sorted(b for b in used if b not in skin)
    retarget, skin_moves, work, work_off, markers = [], {}, None, None, []
    if c3_pose:
        from c2scene import SceneNetwork
        work = SceneNetwork.from_subnetwork(copy.deepcopy(tree['SubNetworkPool']['SubNetwork#0']))
        work_off = rig_offset(tree, work)
        retarget, skin_moves, n4 = plan_retarget(tree, work, c3_pose, node_of_bone or {}, no_turn)
        notes += n4
        markers = plan_new_markers(work, c3_pose, node_of_bone or {}, work_off)
        add_markers(work, markers)          # (logged when written)
    parts = []
    if c3_ports:
        parts = plan_animated_parts(c3_ports, set(b for b in (c3_skeleton or {}) if b not in skin))
        for part in parts:
            if part['bone'] in (c3_skeleton or {}):
                used.add(part['bone'])            # its bone is added even without weights
        extra = sorted(b for b in used if b not in skin)
    if extra:
        new_bones, edits, n3, p3 = plan_new_bones(tree, extra, c3_skeleton or {}, override, work, work_off)
        notes += n3
        problems += p3
        if problems:
            return None, notes, problems, warnings
        from c2scene import SceneNetwork
        lnet = work or SceneNetwork.from_subnetwork(tree['SubNetworkPool']['SubNetwork#0'])
        # (no mounts: chain_mounts / Mater's link layout made Fritter's chains worse in-game)
        if extra_wheels:
            w_edits, w_cons = native_extra_wheels(tree, lnet, extra_wheels, warnings)
            edits = edits + w_edits
            late_edits = late_edits + w_cons
            notes.append('extra wheels built like Cars 2\'s six-wheeler (own hub, a copy of the back wheel\'s '
                         'controls that copies its rotation): %s' % ', '.join(h for _s, h, _b in extra_wheels))
        if parts:
            base_net = lnet
            def depth(b, k=0):                  # base before the parts hanging off it
                p = (c3_skeleton or {}).get(b, (None, None))[1]
                return depth(p, k + 1) if p and k < 64 else k
            parts.sort(key=lambda p: depth(p['bone']))
            left = assign_part_slots(parts, free_part_slots(base_net, bent))   # .bent or the catalog
            if left:
                warnings.append('animated parts without a free clip slot (they ride along): %s'
                                % ', '.join(left))
            parts = [p for p in parts if p.get('slots')]
            edits, part_cons = control_part_edits(edits, parts, base_net)
            late_edits = late_edits + part_cons
    return {'meshes': meshes, 'new_bones': new_bones, 'edits': edits,
            'new_materials': new_materials, 'chains': list(chains),
            'late_edits': late_edits, 'retarget': retarget,
            'skin_moves': skin_moves, 'markers': markers, 'parts': parts}, notes, problems, warnings


def cars3_skeleton(data, offset=(0.0, 0.0, 0.0)):
    """{bone: (row-vector flat 16 matrix, parent)} of a Cars 3 file's skin
    skeleton, moved by `offset`. Parents: a SpinJoint under its centerJoint,
    wheel hubs under Main, the rest under Body."""
    out = {}
    for node in _mesh_nodes(data):
        infl = node.get('Influences') or {}
        for i in range(len(infl)):
            inf = infl.get('Influence#%d' % i) or {}
            nm = inf.get('Name')
            if not nm or nm in out:
                continue
            f = inf['BindPoseSkinLocalToWorldMatrixInverseData']
            m = _inv4([f[0:4], f[4:8], f[8:12], f[12:16]])
            flat = [m[r][c] for r in range(4) for c in range(4)]
            for k in range(3):
                flat[12 + k] += offset[k]
            if nm.endswith('_SpinJoint'):
                parent = nm[:-len('_SpinJoint')] + '_centerJoint'
            elif nm.startswith('Tire') or nm == 'Body':
                parent = 'Main'
            else:
                parent = 'Body'
            out[nm] = (flat, parent)
    return out


# ------------------------------------------------------------------ the Cars 2 base as .oct or .oct.json
# The base can be the game's .oct or offsetting's .oct.json (with its
# <name>.oct.textures folder). With a .oct.json the result is a .oct.json too:
# every part of the base - materials, textures, rig - is carried over as it
# is, only the geometry is replaced, and offsetting packs it as usual.

def is_json(path):
    return str(path).lower().endswith('.json')


_UUID_TEXT = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def _from_json(v, key=None):
    """offsetting JSON -> the plugin's tree, the same as reading the .oct:
    "base64:..." strings become bytes, a Uuid's text ("499654f2-87a9-...")
    becomes its 16 bytes in the .oct's order (GUID, little-endian fields);
    "file:..." texture references stay as they are."""
    if isinstance(v, dict):
        return {k: _from_json(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [_from_json(x, key) for x in v]
    if isinstance(v, str):
        if v.startswith('base64:'):
            r = v[len('base64:'):]
            return base64.b64decode(r + '=' * (-len(r) % 4))
        if key == 'Uuid' and _UUID_TEXT.match(v):
            return uuid.UUID(v).bytes_le
    return v


def _to_json(v, key=None):
    """The tree -> offsetting JSON (the reverse of _from_json): a Uuid's 16
    bytes back to its text, other bytes as "base64:..." without padding."""
    if isinstance(v, dict):
        return {k: _to_json(x, k) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_to_json(x, key) for x in v]
    if isinstance(v, (bytes, bytearray)):
        if key == 'Uuid' and len(v) == 16:
            return str(uuid.UUID(bytes_le=bytes(v)))
        return 'base64:' + base64.b64encode(bytes(v)).decode('ascii').rstrip('=')
    return v


def load_base(path):
    """The Cars 2 character to build on: its .oct, or offsetting's .oct.json."""
    if is_json(path):
        with open(path, 'r', encoding='utf-8') as f:
            return _from_json(json.load(f))
    return c2oct.load(path)


def base_buffers(path, tree):
    """The base's vertex and index buffers (named in its buffer pools, next to the file)."""
    # v1.90: same lookup as the SubNetwork import (named file, checked by the
    # pool's Size, <stem>_0 fallback). _strip() handles .oct.json bases.
    # v1.94: a model with several buffer files -> {pool index: bytes} each (c2geom.pick)
    return c2geom.read_buffers(_strip(path) + '.oct', tree)


def _rig_and_meshes(tree, geometry, bent):
    """Retarget, new bones' nodes, chains, follower constraints; then the
    skin moves, new materials and the geometry. -> (tree, vbuf, ibuf, log)"""
    from c2scene import SceneNetwork
    log = []
    if geometry.get('retarget') or geometry.get('edits') or geometry.get('chains') \
            or geometry.get('late_edits') or geometry.get('markers') or geometry.get('parts'):
        net = SceneNetwork.from_subnetwork(tree['SubNetworkPool']['SubNetwork#0'])
        net._outer = tree
        log += apply_retarget_moves(net, geometry.get('retarget', ()))
        log += add_markers(net, geometry.get('markers', ()))
        log += c2rigedit.apply(net, bent, geometry.get('edits', []))
        for chain in geometry.get('chains', ()):
            log.append(add_dynamic_chain(net, chain))
        log += c2rigedit.apply(net, bent, geometry.get('late_edits', []))   # after the chains
        if geometry.get('retarget') and geometry.get('axle_zero', True):
            dy = axle_zero_shift(net)
            geometry['axle_shift'] = dy
            if dy:
                log.append('rig shifted %.3f down so the front axle is at the rig\'s zero, as on every Cars 2 '
                           'character (the game puts that zero at the axle height - otherwise the model floats)' % dy)
        ports, plog = add_part_channels(net, bent, geometry.get('parts', ()))
        geometry['part_ports'] = ports
        log += plog
        tree = dict(tree)
        tree['SubNetworkPool'] = {'SubNetwork#0': net.build()}
    if geometry.get('skin_moves'):
        apply_skin_moves(tree, geometry['skin_moves'])
    if geometry.get('axle_shift'):
        shift_bones_to_axle_zero(tree, geometry['axle_shift'])
    add_materials(tree, geometry.get('new_materials'))
    tree, vbuf, ibuf, glog = c2geom.rebuild(tree, geometry['meshes'], geometry.get('new_bones', ()))
    log += glog
    link_chains_lazy(tree, geometry.get('chains', ()))
    return tree, vbuf, ibuf, log


def stock_errors(tree):
    """v2.0.21: validator ERRORS the untouched base already gives - the game
    ships / loads it that way (King's renamed right exhausts: car_body slots
    22/23 bound to 'Anchor/Null/Transform'). Not caused by the export, so they
    don't block it (reported as warnings)."""
    from c2validate import validate_subnetwork, validate_associations
    try:
        sn = tree['SubNetworkPool']['SubNetwork#0']
        return set(validate_subnetwork(sn)[0]) | set(validate_associations(tree, sn)[0])
    except Exception:
        return set()


def split_stock(errs, stock_errs):
    """-> (errors the export caused, 'in the base already' warnings)"""
    return ([e for e in errs if e not in stock_errs],
            ['in the base already (kept as it is): ' + e for e in errs if e in stock_errs])


def _stock_warnings(tree):
    """v1.97.2: validator warnings the untouched base already gives (stock
    data, e.g. cinematic_zundapp's cheek constraint order) - not caused by
    the export, so not reported as warnings."""
    from c2validate import validate_subnetwork, validate_associations
    try:
        sn = tree['SubNetworkPool']['SubNetwork#0']
        return set(validate_subnetwork(sn)[1]) | set(validate_associations(tree, sn)[1])
    except Exception:
        return set()


DEFAULT_OFFSETTING = ''   # no built-in path: set offsetting.exe in the add-on preferences


def flat_paths(out_dir, stem):
    """v2.0.13: every character export writes ONE flat folder: <stem>.oct,
    <stem>_0.vbuf, <stem>_0.ibuf (+ <bent> only when its channels changed)."""
    return (os.path.join(out_dir, stem + '.oct'), os.path.join(out_dir, stem + '_0.vbuf'),
            os.path.join(out_dir, stem + '_0.ibuf'))


def inline_json_textures(tree, json_path):
    """offsetting's 'file:' texture Data -> the DDS bytes from <name>.oct.textures
    (as the .oct holds them). -> number embedded; raises when a file is missing."""
    tex = json_path[:-len('.json')] + '.textures'          # mcqueen.oct.json -> mcqueen.oct.textures
    n = 0
    for t in c2geom.plist(tree.get('TexturePool')):
        d = t.get('Data')
        if isinstance(d, str) and d.startswith('file:'):
            p = os.path.join(tex, *[x for x in d[5:].replace('\\', '/').split('/') if x])
            if not os.path.isfile(p):
                raise FileNotFoundError('texture %s is missing (%s) - the .oct.json needs its %s folder next to it'
                                        % (t.get('Name'), p, os.path.basename(tex)))
            with open(p, 'rb') as f:
                t['Data'] = f.read()
            n += 1
    return n


def export_json_base(json_path, geometry, out_dir, bent_path=None, materials=None, offsetting=None):
    """The offsetting .oct.json base: rig edits, new materials, geometry (and
    a template's material edits); the textures of its <name>.oct.textures
    folder embedded, and the result encoded to a binary .oct by offsetting.exe
    - written flat like a .oct base: <name>.oct + <name>_0.vbuf/.ibuf.
    -> (written or None, log, errors, warnings)"""
    import subprocess
    import tempfile
    from c2validate import validate_subnetwork, validate_associations
    import c2channels
    exe = offsetting or DEFAULT_OFFSETTING
    if not exe or not os.path.isfile(exe):
        return None, [], ['offsetting.exe not found (%s) - it turns the .oct.json into the .oct; set its path in '
                          'the Cars 2 Toolkit add-on preferences, or export onto the character\'s .oct' % exe], []
    tree = load_base(json_path)
    stock = _stock_warnings(tree)
    stock_e = stock_errors(tree)
    bent = c2channels.Bent.load(bent_path) if bent_path else None
    try:
        n_tex = inline_json_textures(tree, json_path)
    except FileNotFoundError as e:
        return None, [], [str(e)], []
    tree, vbuf, ibuf, log = _rig_and_meshes(tree, geometry, bent)
    log.append('embedded %d texture(s) from %s' % (n_tex, os.path.basename(json_path[:-len('.json')] + '.textures')))
    warns = []
    if materials is not None:
        mlog, merrs, _tfx = materials(tree)        # (.tfx never written: stock effects only)
        log += mlog
        if merrs:
            return None, log, merrs, warns
    sn = tree['SubNetworkPool']['SubNetwork#0']
    e1, w1 = validate_subnetwork(sn)
    e2, w2 = validate_associations(tree, sn)
    errs, warns = e1 + e2, [w for w in w1 + w2 if w not in stock]
    errs, kept = split_stock(errs, stock_e)
    warns += kept
    if bent is not None:                           # the .bent as updated (new channels)
        from c2scene import SceneNetwork
        errs += ['scene/.bent: ' + x for x in
                 c2channels.validate_pair(SceneNetwork.from_subnetwork(sn), bent)]
    if errs:
        return None, log, errs, warns
    stem = os.path.basename(_strip(json_path))
    sp, vp, ip = flat_paths(out_dir, stem)
    os.makedirs(out_dir, exist_ok=True)
    for pool, ext, data in (('VertexBufferPool', '_0.vbuf', vbuf), ('IndexBufferPool', '_0.ibuf', ibuf)):
        for b in c2geom.plist(tree.get(pool)):
            if b.get('FileName'):
                b['FileName'] = stem + ext
    fd, js = tempfile.mkstemp(suffix='.oct.json', prefix=stem + '_')
    os.close(fd)
    try:
        with open(js, 'w', encoding='utf-8') as f:
            json.dump(_to_json(tree), f)
        r = subprocess.run([exe, 'oct', 'encode', js, sp], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(sp):
            return None, log, ['offsetting encode failed: ' + (r.stderr or r.stdout or '')[-600:]], warns
    finally:
        try:
            os.remove(js)
        except OSError:
            pass
    with open(vp, 'wb') as f:
        f.write(vbuf)
    with open(ip, 'wb') as f:
        f.write(ibuf)
    written = [sp, vp, ip]
    log.append('encoded with offsetting (%s)' % os.path.basename(exe))
    if bent is not None and _bent_changed(geometry):
        bp = os.path.join(out_dir, os.path.basename(bent_path))
        bent.save(bp)
        written.append(bp)
    return written, log, errs, warns


def export_oct_base(oct_path, geometry, out_dir, bent_path=None, materials=None):
    """The .oct base: rig edits, new materials, geometry; written flat into
    out_dir: <car>.oct + <car>_0.vbuf/.ibuf (+ the .bent when its channels
    changed). -> (written or None, log, errors, warnings)"""
    from c2validate import validate_subnetwork, validate_associations
    import c2channels
    from c2scene import SceneNetwork
    tree = c2oct.load(oct_path)
    stock = _stock_warnings(tree)
    stock_e = stock_errors(tree)
    bent = c2channels.Bent.load(bent_path) if bent_path else None
    warns = []
    if bent is not None:
        inv = c2channels.bent_invariants(bent)
        if inv:
            return None, [], ['.bent: ' + x for x in inv], warns
    tree, vbuf, ibuf, log = _rig_and_meshes(tree, geometry, bent)
    if materials is not None:
        # v2.0.8: a template's game materials (charmat.exporter): only what was
        # changed is written. Base MaterialPool indices are unchanged by the
        # geometry step (new materials are appended), so the entries line up.
        mlog, merrs, _tfx = materials(tree)        # (.tfx never written: stock effects only)
        log += mlog
        if merrs:
            return None, log, merrs, warns
    sn = tree['SubNetworkPool']['SubNetwork#0']
    e1, w1 = validate_subnetwork(sn)
    e2, w2 = validate_associations(tree, sn)
    errs, warns = e1 + e2, warns + [w for w in w1 + w2 if w not in stock]
    errs, kept = split_stock(errs, stock_e)
    warns += kept
    if bent is not None:
        errs += ['scene/.bent: ' + x for x in c2channels.validate_pair(SceneNetwork.from_subnetwork(sn), bent)]
    if errs:
        return None, log, errs, warns
    stem = os.path.splitext(os.path.basename(oct_path))[0]
    sp, vp, ip = flat_paths(out_dir, stem)          # v2.0.13: one flat folder
    os.makedirs(out_dir, exist_ok=True)
    c2geom.write(tree, vbuf, ibuf, sp)
    written = [sp, vp, ip]
    if bent is not None and _bent_changed(geometry):
        bp = os.path.join(out_dir, os.path.basename(bent_path))
        bent.save(bp)
        written.append(bp)
    # v2.0.56: no <name>.cars3_ports.json any more - the add-on keeps the part
    # ports (geometry['part_ports']) on the Cars 3 armature for the clip tools
    return written, log, errs, warns


def _bent_changed(geometry):
    """Only channel edits change the .bent; borrowed slots don't."""
    return any(e.get('op') == 'channel' for e in list(geometry.get('edits', ())) + list(geometry.get('late_edits', ())))


def write_part_ports(geometry, out_dir, stem):
    ports = geometry.get('part_ports')
    if not ports:
        return []
    path = os.path.join(out_dir, '%s.cars3_ports.json' % stem)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(ports, f, indent=1)
    return [path]


def export_onto(base_path, geometry, out_dir, bent_path=None, materials=None, offsetting=None):
    """Write the result for a .oct base or a .oct.json base - the same flat
    output either way: <car>.oct + <car>_0.vbuf/.ibuf in out_dir (a .oct.json
    is encoded to the .oct by offsetting.exe). materials: optional apply(tree)
    material writer. -> (written or None, log, errors, warnings)"""
    if is_json(base_path):
        return export_json_base(base_path, geometry, out_dir, bent_path, materials, offsetting)
    return export_oct_base(base_path, geometry, out_dir, bent_path, materials)


# ------------------------------------------------------------------ whole files
def convert(cars3_json, cars2_oct, out_dir, lod=0, bent=None, keep=()):
    """Cars 3 model (.oct.json + _0.vbuf/_0.ibuf) onto the Cars 2 character
    `cars2_oct` (its .oct, or offsetting's .oct.json); writes the result into
    out_dir - the game package layout for a .oct, a .oct.json with its buffers
    and textures folder for a .oct.json. -> (written, notes, errors, warnings)"""
    data, vb3, ib3 = read_cars3(cars3_json)
    tree = load_base(cars2_oct)
    parts = cars3_parts(data, vb3, ib3, lod)
    if not parts:
        return None, [], ['no Cars 3 meshes with LOD %s in this file' % lod], []
    offset, worst, who = alignment_offset(tree, cars3_bone_positions(data))
    notes = ['moved by (%.4f, %.4f, %.4f) so the wheel hubs match the Cars 2 ones' % offset]
    vb2 = ib2 = None
    if keep:
        vb2, ib2 = base_buffers(cars2_oct, tree)
    geom, n2, problems, warnings = build_geometry(tree, parts, offset, keep, vb2, ib2,
                                                  cars3_skeleton(data, offset))
    notes += n2
    if worst > 0.05:
        warnings.append('the skeletons differ by up to %.3f (%s): this does not look like the same car'
                        % (worst, who))
    if problems:
        return None, notes, problems, warnings
    bent_path = bent or c2rigedit.find_bent(cars2_oct)
    written, log, errs, warns = export_onto(cars2_oct, geom, out_dir, bent_path)
    return written, notes + log, errs, warnings + warns
