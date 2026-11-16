"""Cars 3 eyes in the viewport: lids, irises and the look, live from the rig.

In Cars 3 (Driven to Win) a car's eye material shows a render target that the
game draws each frame (EyesComponent):
white clear, the two eyeballs, a shadow copy of the top lid, the lids. Its
code is the Cars 2 eye code carried over line for line (same control points,
notch, Hermite tangents, eyeball quads and lid texture coordinates), so the
toolkit's Cars 2 eye graph (c2_model.build_eye_graph / group 'Cars2 Eye')
draws it exactly - only the parameters are packed differently:

* Values: the eye material's perInstance block in the character's .mtb, named
  by the shader's parameter table (.shd: r_hashCstr name hashes + offsets):
  g_carEyes_eyeBalls_configuration (scale u, scale v, spacing), _position,
  g_carEyes_eyeLids_<top|bottom>_<left|right>_<sides|middle>, _top_notch_all /
  _single, _bottom_notch, _configuration (notch offset), _shadowColor (rgb,
  opacity), _shadowParameters (offset), g_diffuse0_color (lid colour).
  McQueen: eyeballs 0.27 x 0.39, spacing 0.26; shadow black 0.75, 0.03 lower.
* Rig: FloatConnections copy the 30 Eyes_Ctrl sliders onto single components
  (TOP_L_Bow -> g_carEyes_eyeLids_top_right_middle__y ...) every frame, so the
  sliders' values - not the .mtb's - are what shows; the look is a
  RadialBasisPosition over the Eyes control (Cars 3 stores the exponent term
  -1/(2 sd^2) itself, plus a Neutral per output) driving the eyeball position
  and the notch offset.

Viewport: per-frame values reach the material through custom properties on
the eye mesh objects (driven from the armature) read by Attribute nodes -
drivers inside a material tree make Blender rebuild it every frame (the 1.96
lesson). Game Look draws Cars 3 materials flat; this is for Material Preview /
Eevee / Cycles.
"""
import json
import math
import re
import struct

import bpy

from . import c2_model

EYE_TEXTURE = 'g_carEyes_renderTarget_texture'
PROP_VALUES = 'c3_eye'              # material: decoded eye values (JSON)
PROP_PREFIX = 'c3e_'                # eye mesh object: one property per driven eye input
LOOK_BONE = 'Eyes_Ctrl'

_LIDS = ['g_carEyes_eyeLids_%s_%s_%s' % (a, b, c)
         for a in ('top', 'bottom') for b in ('left', 'right') for c in ('sides', 'middle')]
NAMES = ['g_diffuse0_color', 'g_carEyes_eyeBalls_configuration', 'g_carEyes_eyeBalls_position',
         'g_carEyes_eyeLids_configuration', 'g_carEyes_eyeLids_shadowColor',
         'g_carEyes_eyeLids_shadowParameters', 'g_carEyes_eyeLids_top_notch_all',
         'g_carEyes_eyeLids_top_notch_single', 'g_carEyes_eyeLids_bottom_notch'] + _LIDS


# ------------------------------------------------------------------------------------- .shd parameter table
def name_hash(s):
    """r_hashCstr (case-sensitive): the name hash Cars 3 shaders store for parameters."""
    crc = 0
    i = 0
    b = s.encode('latin1') + b'\0'
    v = b[0]
    while v & 0xff:
        v = (v << 8) & 0xffffffff
        i += 1
        v |= b[i]
        if (i & 3) == 3:
            crc = ((crc + v) * 37) & 0xffffffff
    return (crc + ((v << (24 - ((i & 3) << 3))) & 0xffffffff)) & 0xffffffff


_HASHES = {name_hash(n): n for n in NAMES}


def instance_parameters(shd):
    """{name: (byte offset, float count)} of the shader's perInstance scope, plus '__defaults__'
    (the scope's default block). Layout (read from, and checked on, all 570 shipped Cars 3 shaders): counts at
    0x18, scope headers at 0x68 / 0xA0, 88-byte passes from 0xE0, technique words, then name hashes,
    technique hashes, parameter words (cols-1 bits 4-5, rows-1 6-7, elements 9-15, offset 16-31),
    sampler hashes, then the two default blocks (16-aligned)."""
    n_par, n_pass, n_tech, _n_samp = struct.unpack_from('<4B', shd, 0x18)
    ni, nsi, bsi = struct.unpack_from('<BBH', shd, 0x68)
    _nm, nsm, _bsm = struct.unpack_from('<BBH', shd, 0xA0)
    o = 0xE0 + 88 * n_pass
    o = (o + 2 * n_tech + 3) & ~3
    crcs = struct.unpack_from('<%dI' % n_par, shd, o)
    o += 4 * n_par + 4 * n_tech
    words = struct.unpack_from('<%dI' % n_par, shd, o)
    o += 4 * n_par + 4 * (nsi + nsm)
    o = (o + 15) & ~15
    out = {'__defaults__': shd[o:o + bsi]}
    for crc, w in zip(crcs[:ni], words[:ni]):
        name = _HASHES.get(crc)
        if name:
            n = (((w >> 4) & 3) + 1) * (((w >> 6) & 3) + 1) * max(1, (w >> 9) & 127)
            out[name] = (w >> 16, n)
    return out


def decode_values(record, layout):
    """{parameter: [floats]} from the material's record (its perInstance block comes first; the
    shader's defaults where the record is short)."""
    vals = {}
    defaults = layout.get('__defaults__', b'')
    for name, spec in layout.items():
        if name.startswith('__'):
            continue
        off, n = spec
        for block in (record, defaults):
            if len(block) >= off + 4 * n:
                v = list(struct.unpack_from('<%df' % n, block, off))
                if all(math.isfinite(x) for x in v):
                    vals[name] = v
                    break
    return vals


def eye_kind(vals):
    """single (one strip, notch between the eyes) / double (two windows) / eyeLids (lids only),
    told apart the way EyesComponent does: by the parameters the shader has."""
    if 'g_carEyes_eyeLids_configuration' in vals:
        return 'single'
    if 'g_carEyes_eyeBalls_configuration' in vals:
        return 'double'
    return 'eyeLids'


def fix_texture_slots(L, imp, ref, records):
    """Eye materials: their texture bindings sit right after the perInstance block (8 bytes per
    material sampler: TEXB index, flags; ffffffff = bound at run time). The decoder's lenient packet
    search takes the longest run of valid bindings, and when the 8 bytes before the packet are zero
    (a TEXB index 0) it starts one slot early - Cars 3 Sheriff got texture 0 (a hubcap) as its iris
    and every other slot shifted. Read them where the shader's table puts them."""
    try:
        data = imp.src.read(imp.src.sibling(ref, '.mtb'))
        tp = L.mtb_sections(data).get(b'TEXB')
    except Exception:
        return 0
    if not tp:
        return 0
    hashes = [data[tp[0] + 24 + i * 16:tp[0] + 32 + i * 16].hex() for i in range(tp[3])]
    fixed = 0
    for rec in records.values():
        key = rec.get('shader')
        if not key or EYE_TEXTURE not in imp.semantics(key).get('textures', {}):
            continue
        shd = imp.shaderzip.read(key + '.shd')
        _ni, _nsi, bsi = struct.unpack_from('<BBH', shd, 0x68)
        _nm, nsm, _bsm = struct.unpack_from('<BBH', shd, 0xA0)
        raw = bytes.fromhex(rec.get('raw_parameters_hex', ''))
        if len(raw) < bsi + 8 * nsm:
            continue
        ids = [struct.unpack_from('<2I', raw, bsi + 8 * i) for i in range(nsm)]
        if not all((i < len(hashes) and f == 0) or (i == f == 0xffffffff) for i, f in ids):
            continue
        tex = [hashes[i] if i != 0xffffffff else None for i, _f in ids]
        old = list(rec.get('textures') or [])
        new = tex + old[len(tex):]
        if new != old:
            rec['textures'] = new
            fixed += 1
    return fixed


def read_material(imp, mat, record):
    """Called by cars3_textures for every material: an eye material gets its decoded values."""
    if not record or not getattr(imp, 'shaderzip', None):
        return False
    if EYE_TEXTURE not in imp.semantics(record['shader']).get('textures', {}):
        return False
    layout = instance_parameters(imp.shaderzip.read(record['shader'] + '.shd'))
    vals = decode_values(bytes.fromhex(record.get('raw_parameters_hex', '')), layout)
    mat[PROP_VALUES] = json.dumps({'kind': eye_kind(vals), 'values': vals, 'shader': record['shader']})
    return True


# ------------------------------------------------------------------------------------- Cars 3 -> eye graph names
_COMP = 'xyzw'


def c2_targets(kind):
    """(Cars 3 parameter, component) -> Cars 2 eye graph input."""
    m = {}

    def put(name, *targets):
        for i, t in enumerate(targets):
            if t:
                m[(name, i)] = t
    put('g_carEyes_eyeBalls_configuration', 'eyeBallScale_u', 'eyeBallScale_v', 'eyeBallSpacing_u')
    if kind == 'double':
        put('g_carEyes_eyeBalls_position', 'eyeBall_left_offset_u', 'eyeBall_left_offset_v',
            'eyeBall_right_offset_u', 'eyeBall_right_offset_v')
        put('g_carEyes_eyeLids_bottom_notch', 'eyeLidBottom_notch_leftPos_v', 'eyeLidBottom_notch_rightPos_v')
    else:
        put('g_carEyes_eyeBalls_position', 'eyeBallOffset_u', 'eyeBallOffset_v')
        put('g_carEyes_eyeLids_bottom_notch', 'eyeLidBottom_notchPos_v')
        put('g_carEyes_eyeLids_configuration', 'eyeLidOffset_u')
        put('g_carEyes_eyeLids_top_notch_single', 'eyeLidTop_notch_leftPos_u', 'eyeLidTop_notchPos_v',
            'eyeLidTop_notch_rightPos_u')
    put('g_carEyes_eyeLids_top_notch_all', 'eyeLidTop_notch_leftPos_v', 'eyeLidTop_notch_rightPos_v')
    for a, A in (('top', 'Top'), ('bottom', 'Bottom')):
        # sides = (angle at the outer end, outer position, angle at the inner end)
        # middle = (middle u percent, middle position, middle angle)  - EyesComponent's structs
        put('g_carEyes_eyeLids_%s_left_sides' % a, 'eyeLid%s_left_leftAngle' % A,
            'eyeLid%s_left_leftPos_v' % A, 'eyeLid%s_left_rightAngle' % A)
        put('g_carEyes_eyeLids_%s_left_middle' % a, 'eyeLid%s_left_middlePercent_u' % A,
            'eyeLid%s_left_middlePos_v' % A, 'eyeLid%s_left_middleAngle' % A)
        put('g_carEyes_eyeLids_%s_right_sides' % a, 'eyeLid%s_right_leftAngle' % A,
            'eyeLid%s_right_rightPos_v' % A, 'eyeLid%s_right_rightAngle' % A)
        put('g_carEyes_eyeLids_%s_right_middle' % a, 'eyeLid%s_right_middlePercent_u' % A,
            'eyeLid%s_right_middlePos_v' % A, 'eyeLid%s_right_middleAngle' % A)
    return m


def to_c2(vals, kind):
    out = {}
    for (name, i), target in c2_targets(kind).items():
        v = vals.get(name)
        if v is not None and i < len(v):
            out[target] = float(v[i])
    return out


# ------------------------------------------------------------------------------------- the rig's eye network
def _split(name):
    m = re.match(r'^(g_carEyes_\w+?)__([xyzw])$', name or '')
    return (m.group(1), _COMP.index(m.group(2))) if m else None


def rig_network(data):
    """-> {'links': {(param, comp): (slider, rest)}, 'look': {...} or None} from the character's .oct."""
    from .cars3 import _pool
    sn = next((s for s in _pool(data.get('SubNetworkPool'))
               if s.get('DataNodePool') and s.get('ProcessorNodePool')), None)
    if sn is None:
        return {'links': {}, 'look': None}
    dn = _pool(sn['DataNodePool'])
    procs = _pool(sn['ProcessorNodePool'])
    HS, HI = sn.get('HeaderStrings') or [], sn.get('HeaderStringIndices') or []

    def name(i):
        if not isinstance(i, int) or not 0 <= i < len(dn):
            return None
        h = (dn[i].get('Header') or 0) - 1
        return HS[HI[h]] if 0 <= h < len(HI) and 0 <= HI[h] < len(HS) else None

    links = {}
    for p in procs:
        refs = p.get('DataNodeRefs') or []
        if p.get('Type') != 'FloatConnection' or len(refs) != 2:
            continue
        target = _split(name(refs[1]))
        slider = name(refs[0])
        if target and slider:
            rest = dn[refs[0]].get('Data')
            links.setdefault(target, (slider, float(rest) if isinstance(rest, (int, float)) else 0.0))
    look = None
    pos = next((p for p in procs if p.get('Type') == 'RadialBasisPosition'
                and len(p.get('DataNodeRefs') or []) == 4), None)
    if pos is not None:
        _ctrl, k_ref, t_ref, out_ref = pos['DataNodeRefs']
        keys, terms = dn[k_ref].get('Data'), dn[t_ref].get('Data')
        outs = []
        for p in procs:
            refs = p.get('DataNodeRefs') or []
            if p.get('Type') == 'RadialBasisApplyFloat' and len(refs) == 4 and refs[0] == out_ref:
                target = _split(name(refs[3]))
                w, neutral = dn[refs[1]].get('Data'), dn[refs[2]].get('Data')
                if target and isinstance(w, list):
                    outs.append((target, w, float(neutral) if isinstance(neutral, (int, float)) else 0.0))
        if isinstance(keys, list) and isinstance(terms, list) and outs:
            look = {'keys': keys, 'terms': terms, 'outputs': outs}
    return {'links': links, 'look': look}


def look_value(look, x, y, weights, neutral):
    """Neutral + sum w_i exp(|p - k_i|^2 * term_i) (Samurai RadialBasisCompute / Apply)."""
    k, t = look['keys'], look['terms']
    s = neutral
    for i, w in enumerate(weights):
        dx, dy, dz = x - k[3 * i], y - k[3 * i + 1], -k[3 * i + 2]
        s += w * math.exp((dx * dx + dy * dy + dz * dz) * t[i])
    return s


# ------------------------------------------------------------------------------------- the material
def _image_of(mat, slot):
    for n in mat.node_tree.nodes:
        if n.type == 'TEX_IMAGE' and n.label.endswith('[slot %d]' % slot) and n.image is not None:
            return n.image
    return None


def _attribute(tree, name, loc):
    n = tree.nodes.new('ShaderNodeAttribute')
    n.attribute_type = 'OBJECT'
    n.attribute_name = name
    n.location = loc
    n.hide = True
    n['c3_eye_node'] = True
    return n.outputs['Fac']


def _prop_driver(obj, prop, arm, path, expression='v'):
    try:
        obj.driver_remove('["%s"]' % prop)
    except TypeError:
        pass
    fc = obj.driver_add('["%s"]' % prop)
    drv = fc.driver
    drv.type = 'SCRIPTED'
    var = drv.variables.new()
    var.name = 'v'
    var.type = 'SINGLE_PROP'
    var.targets[0].id_type = 'OBJECT'
    var.targets[0].id = arm
    var.targets[0].data_path = path
    drv.expression = expression


def build_material(mat, objs, arm, net):
    """Rebuild one Cars 3 eye material on the Cars 2 eye graph. -> (driven inputs, note)."""
    info = json.loads(mat.get(PROP_VALUES) or '{}')
    kind, vals = info.get('kind'), info.get('values') or {}
    if kind not in ('single', 'double'):
        return 0, 'eye type %s not previewed' % kind
    tree = mat.node_tree
    bsdf = next((n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is None:
        return 0, 'no Principled BSDF'
    iris_img, lid_img = _image_of(mat, 0), _image_of(mat, 1)
    for n in [n for n in tree.nodes if n.get('c3_eye_node')]:
        tree.nodes.remove(n)

    def node(kind_, loc, **kw):
        n = tree.nodes.new(kind_)
        n.location = loc
        n['c3_eye_node'] = True
        for k, v in kw.items():
            setattr(n, k, v)
        return n

    # rest values: the .mtb's, overwritten by the sliders' own rests and the look at rest (the game
    # writes both every frame)
    c2 = to_c2(vals, kind)
    targets = c2_targets(kind)
    links = net.get('links') or {}
    look = net.get('look')
    for key, (slider, rest) in links.items():
        if key in targets:
            c2[targets[key]] = rest
    if look:
        for key, w, neutral in look['outputs']:
            if key in targets:
                c2[targets[key]] = look_value(look, 0.0, 0.0, w, neutral)

    group = c2_model._get_eye_group(kind == 'double')
    g = node('ShaderNodeGroup', (-900, 0), node_tree=group, width=260)
    for name in c2_model.EYE_PARAMS:
        if name in g.inputs:
            g.inputs[name].default_value = c2.get(name, 0.0)
    shadow = vals.get('g_carEyes_eyeLids_shadowColor', [0.0, 0.0, 0.0, 0.75])
    for name, v in (('sclera', 1.0),                      # the target is cleared to white
                    ('lid_line_width', (vals.get('g_carEyes_eyeLids_shadowParameters') or [0.03])[0]),
                    ('lid_line_darkness', shadow[3])):
        if name in g.inputs:
            g.inputs[name].default_value = v

    # --- live inputs (rig present): object properties on the eye meshes, driven from the armature
    driven = 0
    rig = arm is not None and arm.type == 'ARMATURE'
    if rig:
        from . import anim
        try:
            rests = json.loads(arm.get(c2_model.SLIDER_REST_PROP, '{}'))
        except (TypeError, ValueError):
            rests = {}
        for key, (slider, rest) in sorted(links.items()):
            target = targets.get(key)
            if target is None or target not in g.inputs:
                continue
            label = 'Eyes_Ctrl/' + slider
            if label not in arm.keys():
                arm[label] = rest
                anim.ensure_scalar(arm, label)
            rests.setdefault(label, rest)
            prop = PROP_PREFIX + target
            for o in objs:
                o[prop] = float(arm[label])
                _prop_driver(o, prop, arm, '["%s"]' % label)
            tree.links.new(_attribute(tree, prop, (-1150, -40 * driven)), g.inputs[target])
            driven += 1
        arm[c2_model.SLIDER_REST_PROP] = json.dumps(rests)
        if look and LOOK_BONE in arm.pose.bones:
            look_in = {}
            for i, axis in enumerate(('look_x', 'look_y')):
                prop = PROP_PREFIX + axis
                for o in objs:
                    o[prop] = 0.0
                    _prop_driver(o, prop, arm, 'pose.bones["%s"].location[%d]' % (LOOK_BONE, i))
                look_in[axis] = _attribute(tree, prop, (-1900, 300 - 60 * i))
            # Cars 3 stores term = -1/(2 sd^2); the Cars 2 graph wants sd
            sds = [math.sqrt(-0.5 / t) if t < 0 else 1e3 for t in look['terms']]
            outputs, neutrals = [], {}
            for key, w, neutral in look['outputs']:
                target = targets.get(key)
                if target in g.inputs:
                    outputs.append((target, w))
                    neutrals[target] = neutral
            roots = c2_model.build_look_graph(look['keys'], sds, outputs)
            roots = {k: (r + neutrals[k]) if neutrals.get(k) else r for k, r in roots.items()}
            before = set(tree.nodes)
            sockets = c2_model.emit_shader_nodes(tree, c2_model.canonicalize(roots), look_in, x0=-1750, y0=700)
            for n in set(tree.nodes) - before:
                n['c3_eye_node'] = True
            for target, sock in sockets.items():
                if not isinstance(sock, float):
                    tree.links.new(sock, g.inputs[target])
                    driven += 1

    # --- colour: white, eyeballs, the top lid's shadow copy, the lids (EyesComponent draw order)
    out = g.outputs
    comb = node('ShaderNodeCombineXYZ', (-550, 250))
    tree.links.new(out['iris_u'], comb.inputs[0])
    tree.links.new(out['iris_v'], comb.inputs[1])
    if iris_img is not None:
        # Cars 3 eyeball textures are opaque (white around the iris): the game draws the whole quad,
        # white on white. Edge clamped + an inside test (the tool shader's), no CLIP fringe.
        img = node('ShaderNodeTexImage', (-380, 300), image=iris_img, extension='EXTEND')
        tree.links.new(comb.outputs[0], img.inputs[0])
        iris_col, iris_a = img.outputs['Color'], img.outputs['Alpha']
    else:
        iris_col, iris_a = (0.05, 0.05, 0.05), 1.0
    inside = None
    for k, (sock, op, edge) in enumerate(((out['iris_u'], 'GREATER_THAN', 0.0), (out['iris_u'], 'LESS_THAN', 1.0),
                                          (out['iris_v'], 'GREATER_THAN', 0.0), (out['iris_v'], 'LESS_THAN', 1.0))):
        t = node('ShaderNodeMath', (-550, 450 - 40 * k), operation=op, hide=True)
        tree.links.new(sock, t.inputs[0])
        t.inputs[1].default_value = edge
        if inside is None:
            inside = t.outputs[0]
        else:
            m = node('ShaderNodeMath', (-400, 450 - 40 * k), operation='MULTIPLY', hide=True)
            tree.links.new(inside, m.inputs[0])
            tree.links.new(t.outputs[0], m.inputs[1])
            inside = m.outputs[0]
    fac = node('ShaderNodeMath', (-150, 250), operation='MULTIPLY')
    tree.links.new(inside, fac.inputs[0])
    if hasattr(iris_a, 'is_output'):
        tree.links.new(iris_a, fac.inputs[1])
    else:
        fac.inputs[1].default_value = iris_a
    col = c2_model._mix_color(tree, fac.outputs[0], out['sclera'], iris_col, (50, 250))
    col.node['c3_eye_node'] = True
    sfac = node('ShaderNodeMath', (50, 50), operation='MULTIPLY')
    tree.links.new(out['line'], sfac.inputs[0])
    sfac.inputs[1].default_value = shadow[3]
    col = c2_model._mix_color(tree, sfac.outputs[0], col, shadow[:3], (250, 100))
    col.node['c3_eye_node'] = True
    tint = (vals.get('g_diffuse0_color') or [1.0, 1.0, 1.0])[:3]
    if lid_img is not None:
        lc = node('ShaderNodeCombineXYZ', (-550, -250))
        tree.links.new(out['lid_u'], lc.inputs[0])
        tree.links.new(out['lid_v'], lc.inputs[1])
        limg = node('ShaderNodeTexImage', (-380, -250), image=lid_img)
        tree.links.new(lc.outputs[0], limg.inputs[0])
        lid = c2_model._mix_color(tree, 1.0, limg.outputs['Color'], tint, (-100, -250))
        lid.node['c3_eye_node'] = True
        try:
            lid.node.blend_type = 'MULTIPLY'
        except AttributeError:
            pass
    else:
        lid = tint
    col = c2_model._mix_color(tree, out['lid'], col, lid, (450, 0))
    col.node['c3_eye_node'] = True
    tree.links.new(col, bsdf.inputs['Base Color'])
    for n in tree.nodes:                     # the decoder's own iris / lid nodes are now unused
        if n.type == 'TEX_IMAGE' and not n.get('c3_eye_node') and n.label.endswith(('[slot 0]', '[slot 1]')):
            n.mute = True
    mat['c3_eye_shader'] = 'Cars 3 eyes drawn like the game draws its eye render target'
    return driven, ''


def setup(arm, meshes, data):
    """After the Cars 3 import: every eye material on the character's meshes. -> note for the report."""
    eye_mats = {}
    for o in meshes:
        for slot in o.material_slots:
            m = slot.material
            if m is not None and m.get(PROP_VALUES):
                eye_mats.setdefault(m.name, (m, []))[1].append(o)
    if not eye_mats:
        return ''
    try:
        net = rig_network(data)
    except Exception as e:
        print('Cars 3 eyes: rig network not read: %s' % e)
        net = {'links': {}, 'look': None}
    has_rig = arm is not None and any(b.name == LOOK_BONE for b in arm.data.bones)
    notes = []
    for m, objs in eye_mats.values():
        try:
            n, note = build_material(m, objs, arm if has_rig else None, net)
            notes.append('%s: %s' % (m.name, note) if note else
                         '%s: %d inputs from the rig' % (m.name, n) if n else '%s: rest pose' % m.name)
        except Exception as e:
            import traceback
            traceback.print_exc()
            notes.append('%s: eyes not built (%s)' % (m.name, e))
    return 'eyes ' + '; '.join(notes)
