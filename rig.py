"""Rig editing for imported Cars 2 characters.

Work the normal Blender way - add and parent bones, add Copy Location /
Rotation / Scale / Transforms constraints, add a driver with a keyed curve -
then press the matching button. Each press records an edit; nothing touches
game files until Export, which applies the edits to the original .oct/.bent
and validates the result against the rules shipping rigs follow.
"""
import os

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ExportHelper

import re
import mathutils

from mathutils import Matrix, Quaternion, Vector

import c2rigedit as rigedit
from c2scene import SceneNetwork
from . import character as ch
from . import anim
from . import geometry  # noqa: F401  (meshes are compiled on Validate / Export)

KIND_OF = {'COPY_LOCATION': ['point'], 'COPY_ROTATION': ['orient'], 'COPY_SCALE': ['scale'],
           'COPY_TRANSFORMS': ['orient', 'point', 'scale']}
PROP_OF = {('rotation_euler', 0): 'rot_x', ('rotation_euler', 1): 'rot_y', ('rotation_euler', 2): 'rot_z',
           ('location', 0): 'loc_x', ('location', 1): 'loc_y', ('location', 2): 'loc_z',
           ('scale', 0): 'scale_x', ('scale', 1): 'scale_y', ('scale', 2): 'scale_z'}


# ------------------------------------------------------------------ helpers
def selected_bone_names(context, arm):
    """Selected bones in any mode (Blender 4: Bone.select, Blender 5: PoseBone.select)."""
    if context.mode == 'EDIT_ARMATURE':
        return [eb.name for eb in arm.data.edit_bones if eb.select]
    out = []
    for pb in arm.pose.bones:
        if getattr(pb, 'select', False) or getattr(pb.bone, 'select', False):
            out.append(pb.name)
    return out


def active_bone_name(context, arm):
    if context.mode == 'EDIT_ARMATURE':
        eb = arm.data.edit_bones.active
        return eb.name if eb else None
    b = arm.data.bones.active
    return b.name if b else None


def rig_root(arm):
    paths = list(ch.bone_nodes(arm).values())
    roots = [p.split('/', 1)[0] for p in paths]
    return max(set(roots), key=roots.count) if roots else 'Anchor'


def node_base(path):
    """'Anchor/Main_Ctrl/Transform' -> 'Anchor/Main_Ctrl'."""
    return path[:-len('/Transform')] if path.endswith('/Transform') else path


def float_nodes(arm):
    """Float nodes a driver can read: {armature property label: node path}."""
    out = {}
    try:
        _t, net, _b, _l, _e, _w = ch.build(arm)
    except Exception:
        return out
    for d in net.data:
        if d['Type'] == 'Float':
            out[rigedit.label_of(d['Header'])] = d['Header']
    return out


def template_items(self, context):
    arm = ch.active_character(context)
    items = []
    if arm is not None and arm.get(ch.PROP_BENT):
        try:
            import c2channels
            bent = c2channels.Bent.load(arm[ch.PROP_BENT])
            for name, i in bent.interfaces:
                if 'BlenderChannelMap' in i:
                    tm = i['ConnectionTemplate']
                    items.append((tm, '%s (%s)' % (name, tm), 'Clips in the %s clip set' % name))
        except Exception:
            pass
    if not items and arm is not None:          # no .bent: the clip templates the rig is linked to
        try:
            _t, net, _b, _l, _e, _w = ch.build(arm)
            for tm in sorted({x['ct'].split('/')[-1][:-3] for x in net.links}):
                items.append((tm, tm, 'Clips using the %s template' % tm))
        except Exception:
            pass
    return items or [('NONE', 'No clip templates found', '')]


def _require(self, context):
    arm = ch.active_character(context)
    if arm is None:
        self.report({'ERROR'}, 'Select an imported Cars 2 character (File > Import > Cars 2 Character)')
    return arm


def _object_mode(context):
    """Commit edit-mode changes so rest matrices are current; returns the previous mode."""
    prev = context.mode
    if prev != 'OBJECT' and context.object is not None:
        bpy.ops.object.mode_set(mode='OBJECT')
    return prev


def _restore_mode(prev):
    target = {'EDIT_ARMATURE': 'EDIT', 'POSE': 'POSE'}.get(prev)
    if target:
        bpy.ops.object.mode_set(mode=target)


def add_edit(arm, edit, replace_key=None):
    edits = ch.get_edits(arm)
    if replace_key:
        edits = [e for e in edits if not replace_key(e)]
    edits.append(edit)
    ch.set_edits(arm, edits)


# --------------------------------------------------- moved rig bones (FX markers ...)
# Move / rotate a rig bone (an FX marker, Body_Final_Proxy, a *_Ctrl ...) in
# Edit Mode or Pose Mode, or scale it in Pose Mode: Record Moved Rig Bones
# (also run on Validate / Export) turns that into a node_transform edit with
# the node's new values.
# A rotated node that has no rotation field gets Orientation#Rotor (the same
# markers carry one on other characters). An added Scale is not written by
# default: tested in-game (Sheriff's exhaust), the game ignores a Scale on a
# marker that didn't have one.
ADD_MISSING_ROTATION = True
ADD_MISSING_SCALE = False
TRANSFORM_TYPES = ('Transform', 'Dilation')


def _rotor_q(r):
    x, y, z, w = r[:4]                     # stored conjugated relative to mathutils
    return Quaternion((w, -x, -y, -z))


def _q_rotor(q, like=None):
    r = [-q.x, -q.y, -q.z, q.w]
    if like is not None and sum(a * b for a, b in zip(r, like)) < 0:
        r = [-v for v in r]                # keep the original's sign
    return [float(v) for v in r]


def _node_local(n):
    d = n.get('Data') if isinstance(n.get('Data'), dict) else {}
    t = d.get('Translation') or (0.0, 0.0, 0.0)
    rot = Matrix.Identity(4)
    if 'Orientation#Rotor' in d:
        rot = _rotor_q(d['Orientation#Rotor']).to_matrix().to_4x4()
    px = d.get('Orientation#OrientProxy')
    if isinstance(px, dict):
        rot = (_rotor_q(px.get('ParentProxyToParent#Rotor', (0, 0, 0, 1))).to_matrix().to_4x4()
               @ _rotor_q(px.get('LocalToLocalProxy#Rotor', (0, 0, 0, 1))).to_matrix().to_4x4())
    sc = d.get('Scale') or (1.0, 1.0, 1.0)
    return Matrix.Translation(Vector(t[:3])) @ rot @ Matrix.Diagonal((sc[0], sc[1], sc[2], 1.0))


def _node_world(net, i, over, depth=0):
    n = net.data[i]
    loc = over[i] if i in over else _node_local(n)
    b = n.get('BasisDataNodeRef')
    return _node_world(net, b, over, depth + 1) @ loc if b is not None and depth < 256 else loc


def _animated(arm, curves_out=None):
    """{bone name: {'location', 'rotation', 'scale'}} keyed by the active
    action; curves_out (a dict) is filled with {bone name: [fcurves]}."""
    out = {}
    act = arm.animation_data.action if arm.animation_data else None
    if act is None:
        return out
    curves = list(getattr(act, 'fcurves', None) or [])
    if not curves and hasattr(act, 'layers'):                     # Blender 4.4+
        for layer in act.layers:
            for strip in layer.strips:
                for bag in getattr(strip, 'channelbags', []):
                    curves += list(bag.fcurves)
    for fc in curves:
        m = re.match(r'pose\.bones\["(.+?)"\]\.(location|rotation_\w+|scale)', fc.data_path)
        if m:
            out.setdefault(m.group(1), set()).add('rotation' if m.group(2).startswith('rotation')
                                                  else m.group(2))
            if curves_out is not None:
                curves_out.setdefault(m.group(1), []).append(fc)
    return out


def _clip_basis(pb, curves, frame):
    """The bone's location/rotation as the active clip has it at `frame`."""
    loc = Vector(pb.location)
    mode = pb.rotation_mode
    if mode == 'QUATERNION':
        rot = list(pb.rotation_quaternion)
    elif mode == 'AXIS_ANGLE':
        rot = list(pb.rotation_axis_angle)
    else:
        rot = list(pb.rotation_euler)
    for fc in curves:
        prop = fc.data_path.rsplit('.', 1)[-1]
        if prop == 'location':
            loc[fc.array_index] = fc.evaluate(frame)
        elif prop in ('rotation_quaternion', 'rotation_euler', 'rotation_axis_angle') and \
                prop.endswith({'QUATERNION': 'quaternion', 'AXIS_ANGLE': 'axis_angle'}.get(mode, 'euler')):
            rot[fc.array_index] = fc.evaluate(frame)
    if mode == 'QUATERNION':
        q = Quaternion(rot).normalized()
    elif mode == 'AXIS_ANGLE':
        q = Quaternion(Vector(rot[1:4]), rot[0])
    else:
        from mathutils import Euler
        q = Euler(rot, mode).to_quaternion()
    return Matrix.Translation(loc) @ q.to_matrix().to_4x4()


def _same(a, b, eps=1e-5):
    return a is not None and b is not None and len(a) == len(b) and \
        all(abs(float(x) - float(y)) < eps for x, y in zip(a, b))


def record_moved(arm):
    """Replace the character's node_transform edits with the rig bones that
    are moved now, and refresh the placement of bones added with Add
    Selected Bones to Rig. A rig bone counts as moved when:
      - its rest was moved/rotated in Edit Mode, or
      - it is moved/rotated in Pose Mode (its own location/rotation channels,
        so you see the mesh follow) - not for bones with constraints (the
        game drives those) or channels keyed by the active animation;
      - scaled in Pose Mode (markers that already have a Scale).
    Pose Mode moves, once recorded, are kept even if the pose is later reset
    (importing a clip resets it); remove one with the X in the edit list.
    Returns (bone names recorded, notes)."""
    for o in bpy.data.objects:
        if o.mode == 'EDIT' and o.type == 'ARMATURE':
            o.update_from_editmode()
    remove_body_height_slider(arm)
    arm.update_tag()                                     # re-evaluate drivers (Body Height)
    bpy.context.view_layer.update()
    net = SceneNetwork.load(arm[ch.PROP_SCENE])          # the unedited source rig
    mapping = ch.bone_nodes(arm)
    driven = set()
    for fc in (arm.animation_data.drivers if arm.animation_data else []):
        m = re.match(r'pose\.bones\["(.+?)"\]\.', fc.data_path)
        if m:
            driven.add(m.group(1))
    old_edits = ch.get_edits(arm)
    edits = [e for e in old_edits if e['op'] != 'node_transform']
    # (a driven bone - the Body Height slider - is always read as it is now)
    kept_pose = {e['path']: e for e in old_edits
                 if e['op'] == 'node_transform' and e.get('source') == 'pose'
                 and e.get('bone') not in driven}
    added = {e['path']: e for e in edits if e['op'] == 'add_node'}
    bones = arm.data.bones
    for b in bones:                          # bones added to the rig follow the bone
        e = added.get(mapping.get(b.name))
        if e is None or b.parent is None:
            continue
        # Where the bone is: its Edit Mode rest plus its own Pose Mode move
        # (what you see). A pose move is kept once recorded, even if a clip
        # import resets the pose later. A rotation-copying constraint owns
        # the rotation, so then only the pose position is used.
        pb = arm.pose.bones[b.name]
        ploc, prot, _ps = pb.matrix_basis.decompose()
        if any(c.type in ('COPY_ROTATION', 'COPY_TRANSFORMS') for c in pb.constraints):
            prot = Quaternion()
        posed = ploc.length > 1e-6 or prot.angle > 1e-6
        if not posed and e.get('from_pose'):
            continue                         # keep the recorded pose move
        place = b.matrix_local @ Matrix.Translation(ploc) @ prot.to_matrix().to_4x4()
        loc, q, _s = (b.parent.matrix_local.inverted() @ place).decompose()
        e['translation'], e['rotor'] = list(loc), [-q.x, -q.y, -q.z, q.w]
        if posed:
            e['from_pose'] = True
        else:
            e.pop('from_pose', None)
    off = Matrix.Translation(Vector(arm.get('cars2_rig_offset', (0, 0, 0))))
    curves = {}
    animated = _animated(arm, curves)
    frame = bpy.context.scene.frame_current
    todo, notes = [], []
    for b in bones:
        path = mapping.get(b.name)
        if not path or path in added or 'oct_data_node' not in b or 'oct_rest' not in b \
                or not net.has(path):
            continue
        i = net.index(path)
        if net.data[i]['Type'] not in TRANSFORM_TYPES:
            continue
        pb = arm.pose.bones[b.name]
        keyed = animated.get(b.name, set())
        r = list(b['oct_rest'])
        orig = Matrix([r[0:4], r[4:8], r[8:12], r[12:16]])
        now, source = b.matrix_local, 'rest'
        ploc, prot, _ps = pb.matrix_basis.decompose()
        if ploc.length > 1e-6 or prot.angle > 1e-6:
            if len(pb.constraints):
                notes.append('%s: has constraints (the game drives it) - its pose is ignored' % b.name)
            elif keyed & {'location', 'rotation'}:
                # The clip poses this bone. What differs from the clip at
                # this frame is the user's own adjustment (Blender drops it
                # when the frame changes, so this is read now).
                clip = _clip_basis(pb, curves.get(b.name, []), frame)
                cur = Matrix.Translation(ploc) @ prot.to_matrix().to_4x4()
                adj = clip.inverted() @ cur
                aloc, arot, _as = adj.decompose()
                if aloc.length > 1e-5 or arot.angle > 1e-5:
                    now = b.matrix_local @ Matrix.Translation(aloc) @ arot.to_matrix().to_4x4()
                    source = 'pose'
                    notes.append('%s: moved on top of the loaded clip - recorded (export or '
                                 'record before changing frame: Blender drops it then)' % b.name)
            else:
                # the bone's own pose on top of its rest - not motion inherited
                # from animated parents
                now = b.matrix_local @ Matrix.Translation(ploc) @ prot.to_matrix().to_4x4()
                source = 'pose'
        delta = None
        if any(abs(now[x][y] - orig[x][y]) > 1e-5 for x in range(4) for y in range(4)):
            delta = now @ orig.inverted()
        f = Vector(pb.scale)
        if all(abs(c - 1.0) < 1e-5 for c in f):
            f = None
        elif 'scale' in keyed:
            notes.append('%s: scale comes from the active animation - ignored' % b.name)
            f = None
        if delta is not None or f is not None:
            todo.append((b.name, path, i, delta, f, source if delta is not None else 'pose'))

    def depth(i):
        k = 0
        while net.data[i].get('BasisDataNodeRef') is not None and k < 256:
            i = net.data[i]['BasisDataNodeRef']
            k += 1
        return k
    # A node with no position field (e.g. Body_Final_Proxy, Data {}) can't be
    # moved in the file - the game ignores fields added to nodes that lack
    # them. Move the nearest parent that has one instead, if that is
    # equivalent: the parent isn't a clip channel or written by the rig, and
    # every node in between has this as its only child (Mater/McQueen/
    # Sheriff: Body_Final_Proxy -> Body_Final_ProxyGrp).
    linked = set(d for ls in net.links for d in ls['nodes'] if d >= 0)
    kids = {}
    for k, nd in enumerate(net.data):
        if nd['Type'] in TRANSFORM_TYPES and nd.get('BasisDataNodeRef') is not None:
            kids.setdefault(nd['BasisDataNodeRef'], []).append(k)

    def has_pos(k):
        dd = net.data[k].get('Data')
        return isinstance(dd, dict) and 'Translation' in dd

    def redirect(k):
        j = k
        while True:
            pj = net.data[j].get('BasisDataNodeRef')
            if pj is None or kids.get(pj) != [j]:
                return None
            if pj in linked or net.writer_of(pj) is not None:
                return None
            if has_pos(pj):
                return pj
            j = pj
    def redirect_down(k):
        # first node below a clip-driven control, along a chain with nothing
        # branching off, that clips don't animate and the rig doesn't drive
        j = k
        while True:
            ch_ = kids.get(j, [])
            if len(ch_) != 1:
                return None
            c = ch_[0]
            if c not in linked and net.writer_of(c) is None and has_pos(c):
                return c
            j = c
    retarget = []
    for t in todo:
        name, path, i, delta, f, source = t
        if delta is not None and i in linked:
            # Clip-driven controls (Body_Ctrl ...) are animated, and their rest
            # is also kept in the rig's rest table - move the first free node
            # below them instead (Body_Ctrl -> Body_Final_ProxyGrp).
            j = redirect_down(i)
            if j is not None:
                notes.append('%s: animated by clips - moved %s instead (same effect)'
                             % (name, rigedit.label_of(net.path_of(j))))
                retarget.append((name, net.path_of(j), j, delta, None, source))
                if f is not None:
                    retarget.append((name, path, i, None, f, source))
                continue
            notes.append('%s: animated by clips and nothing below it can take the move - '
                         'it may be overridden in-game' % name)
        if delta is not None and not has_pos(i) and delta.translation.length > 1e-5:
            j = redirect(i)
            if j is not None:
                notes.append('%s: its node has no position field - moved %s instead (same effect)'
                             % (name, rigedit.label_of(net.path_of(j))))
                retarget.append((name, net.path_of(j), j, delta, None, source))
                t = (name, path, i, None, f, source) if f is not None else None
        if t is not None:
            retarget.append(t)
    todo = retarget
    todo.sort(key=lambda t: depth(t[2]))                  # parents first
    over, new, names = {}, [], []
    for name, path, i, delta, f, source in todo:
        n = net.data[i]
        d = n.get('Data') if isinstance(n.get('Data'), dict) else {}
        e = {'op': 'node_transform', 'path': path, 'source': source, 'bone': name}
        if delta is not None:
            new_world = off.inverted() @ delta @ off @ _node_world(net, i, over)
            pbi = n.get('BasisDataNodeRef')
            pw = _node_world(net, pbi, over) if pbi is not None else Matrix.Identity(4)
            loc = pw.inverted() @ new_world
            t, q, _s = loc.decompose()
            if 'Translation' in d:
                if not _same(list(t), d['Translation']):
                    e['translation'] = [float(v) for v in t]
            elif t.length > 1e-5:
                notes.append('%s: its node has no position field - move not exported' % name)
            if delta.to_quaternion().angle > 1e-4:
                if 'Orientation#Rotor' in d:
                    e['rotor'] = _q_rotor(q, d['Orientation#Rotor'])
                elif isinstance(d.get('Orientation#OrientProxy'), dict):
                    px = d['Orientation#OrientProxy']
                    ppp = _rotor_q(px.get('ParentProxyToParent#Rotor', (0, 0, 0, 1)))
                    e['llp_rotor'] = _q_rotor(ppp.inverted() @ q, px.get('LocalToLocalProxy#Rotor'))
                elif ADD_MISSING_ROTATION and n['Type'] == 'Transform':
                    e['rotor'] = _q_rotor(q)
                    notes.append('%s: had no rotation in the file - Orientation#Rotor added' % name)
                else:
                    notes.append('%s: its node stores no rotation - rotation not exported' % name)
            over[i] = loc
        if f is not None:
            if 'Scale' in d:
                e['scale'] = [float(d['Scale'][k] * f[k]) for k in range(3)]
            elif ADD_MISSING_SCALE:
                e['scale'] = [float(f[k]) for k in range(3)]
            else:
                notes.append('%s: has no Scale in the file (the game ignores an added one) - '
                             'scale not exported' % name)
        if len(e) > 3:
            new.append(e)
            names.append(name)
            kept_pose.pop(path, None)
    # Pose Mode moves recorded earlier whose pose has since been reset
    # (e.g. by importing a clip) stay until removed from the edit list.
    still = list(kept_pose.values())
    ch.set_edits(arm, edits + new + still)
    return names + ['%s (kept)' % rigedit.label_of(e['path']) for e in still], notes


BODY_PROP = 'cars2_body_height'          # the Body Height slider of v1.8/1.9 (removed)


def remove_body_height_slider(arm):
    """Characters imported with v1.8/1.9 carry a Body Height property and
    drivers on the body bone; take them off (the slider was removed - move
    Body_Ctrl instead). Returns True if anything was removed."""
    removed = False
    if arm.animation_data:
        for fc in list(arm.animation_data.drivers):
            if any(v.targets[0].data_path == '["%s"]' % BODY_PROP
                   for v in fc.driver.variables if v.targets):
                arm.animation_data.drivers.remove(fc)
                removed = True
    if BODY_PROP in arm:
        name = None
        for bn, p in ch.bone_nodes(arm).items():
            if p.endswith('/Body_Final_Proxy/Transform') and bn in arm.pose.bones:
                name = bn
        if name is not None and removed:
            arm.pose.bones[name].location = (0.0, 0.0, 0.0)
        del arm[BODY_PROP]
        removed = True
    return removed


def rig_summary(arm):
    """Human-readable list of what the export changes in the rig."""
    out = []
    for e in ch.get_edits(arm):
        if e['op'] == 'node_transform':
            bits = []
            if 'translation' in e:
                bits.append('position (%.3f, %.3f, %.3f)' % tuple(e['translation']))
            if 'rotor' in e or 'llp_rotor' in e:
                bits.append('rotation')
            if 'scale' in e:
                bits.append('scale (%.3f, %.3f, %.3f)' % tuple(e['scale']))
            via = '' if rigedit.label_of(e['path']) == e.get('bone') else ' (from moving %s)' % e.get('bone')
            out.append('%s: %s%s' % (rigedit.label_of(e['path']), ', '.join(bits), via))
        else:
            out.append(EDIT_TEXT.get(e['op'], lambda x: x['op'])(e))
    return out or ['NONE - nothing moved (see notes)']


def _popup(context, lines):
    if bpy.app.background or not getattr(context, 'window', None):
        return                                    # no screen to show it on
    def draw(self, _context):
        for ln in lines:
            self.layout.label(text=ln)
    try:
        context.window_manager.popup_menu(draw, title='Cars 2 export', icon='INFO')
    except Exception:
        pass


IMPORT_MARKS = ('oct_rest', 'oct_data_node')


def user_added_bones(arm):
    """Bones the user added: those added with Add Selected Bones to Rig (an
    add_node edit), plus any bone without the importer's mark. A bone made by
    duplicating an imported one (Shift+D) copies that mark, so the edit is
    what counts."""
    mapping = ch.bone_nodes(arm)
    added_paths = {e['path'] for e in ch.get_edits(arm) if e['op'] == 'add_node'}
    has_notes = any('oct_rest' in b for b in arm.data.bones)
    out = set()
    for b in arm.data.bones:
        if mapping.get(b.name) in added_paths or (has_notes and 'oct_rest' not in b):
            out.add(b.name)
    return out


def clear_import_marks(arm, names):
    for n in names:
        b = arm.data.bones.get(n)
        if b is not None:
            for k in IMPORT_MARKS:
                if k in b:
                    del b[k]


def bake_pose_to_rest(arm, names):
    """Make the pose of bones the user added their rest position: move the
    mesh vertices weighted to them by the pose (blended by weight), move the
    bones' rest there, clear their pose. Only the bones' own pose is used, not
    motion from posed parents (e.g. a loaded clip). Returns the bones baked."""
    deltas = {}
    mine = user_added_bones(arm)
    clear_import_marks(arm, [n for n in names if n in mine])   # copied by Shift+D
    for n in names:
        b = arm.data.bones.get(n)
        if b is None or n not in mine:            # only bones the user added
            continue
        loc, rot, _sc = arm.pose.bones[n].matrix_basis.decompose()
        if loc.length < 1e-6 and rot.angle < 1e-6:
            continue
        new_rest = b.matrix_local @ Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
        deltas[n] = (new_rest, new_rest @ b.matrix_local.inverted())
    if not deltas:
        return []
    # 1. the mesh: every vertex weighted to a baked bone moves with it
    for o in bpy.data.objects:
        if o.type != 'MESH' or not any(m.type == 'ARMATURE' and m.object == arm for m in o.modifiers):
            continue
        groups = {o.vertex_groups[n].index: d for n, (_r, d) in deltas.items() if n in o.vertex_groups}
        if not groups:
            continue
        to_arm = arm.matrix_world.inverted() @ o.matrix_world
        back = to_arm.inverted()
        for v in o.data.vertices:
            p = to_arm @ v.co
            moved = p.copy()
            for g in v.groups:
                d = groups.get(g.group)
                if d is not None and g.weight > 0.0:
                    moved += ((d @ p) - p) * g.weight
            if (moved - p).length > 1e-9:
                v.co = back @ moved
        o.data.update()
    # 2. the bones' rest, 3. their pose
    prev = bpy.context.view_layer.objects.active
    mode = arm.mode
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    for n, (new_rest, _d) in deltas.items():
        eb = arm.data.edit_bones[n]
        length = eb.length
        eb.matrix = new_rest
        eb.length = length
    bpy.ops.object.mode_set(mode='POSE')
    for n in deltas:
        pb = arm.pose.bones[n]
        pb.location = (0.0, 0.0, 0.0)
        pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pb.rotation_euler = (0.0, 0.0, 0.0)
    edits = ch.get_edits(arm)                 # their placement now comes from the rest
    for e in edits:
        e.pop('from_pose', None)
    ch.set_edits(arm, edits)
    if mode != 'POSE':
        bpy.ops.object.mode_set(mode=mode if mode in ('OBJECT', 'EDIT') else 'OBJECT')
    bpy.context.view_layer.objects.active = prev
    return list(deltas)


# ---------------------------------------------------------------- operators
class CARS2TK_OT_add_bones(bpy.types.Operator):
    """Add the selected new bones to the game rig (their parent must already be in it)"""
    bl_idname = 'cars2tk.add_bones'
    bl_label = 'Add Selected Bones to Rig'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        arm = _require(self, context)
        if arm is None:
            return {'CANCELLED'}
        names = selected_bone_names(context, arm)
        prev = _object_mode(context)
        try:
            mapping = ch.bone_nodes(arm)
            todo = [n for n in names if n not in mapping]
            if not todo:
                self.report({'WARNING'}, 'No new bones selected (selected bones are already in the rig)')
                return {'CANCELLED'}
            bones = arm.data.bones
            todo.sort(key=lambda n: len(bones[n].parent_recursive))      # parents first
            root = rig_root(arm)
            added = []
            for name in todo:
                b = bones[name]
                if '/' in name:
                    self.report({'ERROR'}, '%s: bone names cannot contain "/"' % name)
                    return {'CANCELLED'}
                if b.parent is None or b.parent.name not in mapping:
                    self.report({'ERROR'}, '%s: parent it to a bone that is already in the rig' % name)
                    return {'CANCELLED'}
                local = b.parent.matrix_local.inverted() @ b.matrix_local
                loc, q, scale = local.decompose()
                if max(abs(x - 1.0) for x in scale) > 1e-4:
                    self.report({'WARNING'}, '%s: bone scale is ignored by the game rig' % name)
                path = '%s/%s/Transform' % (root, name)
                add_edit(arm, {'op': 'add_node', 'path': path, 'parent': mapping[b.parent.name],
                               'translation': list(loc), 'rotor': [-q.x, -q.y, -q.z, q.w]})
                mapping[name] = path
                ch.set_bone_nodes(arm, mapping)
                added.append(name)
            # a bone made by duplicating an imported one (Shift+D) carries the
            # importer's marks - it's a new bone now
            clear_import_marks(arm, added)
        finally:
            _restore_mode(prev)
        self.report({'INFO'}, 'Added to rig: %s' % ', '.join(added))
        return {'FINISHED'}


class CARS2TK_OT_add_channel(bpy.types.Operator):
    """Let clips animate the active bone (or a float control): adds it as a new animation channel"""
    bl_idname = 'cars2tk.add_channel'
    bl_label = 'Animate in Clips'
    bl_options = {'REGISTER', 'UNDO'}
    templates: EnumProperty(name='Clip sets', items=template_items, options={'ENUM_FLAG'})
    float_label: StringProperty(name='Float control', default='',
                                description='Leave empty to use the active bone')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        col = self.layout.column()
        col.label(text='Clip sets whose clips should be able to animate it:')
        col.prop(self, 'templates')
        col.prop(self, 'float_label')

    def execute(self, context):
        arm = _require(self, context)
        if arm is None:
            return {'CANCELLED'}
        if self.float_label and not arm.get(ch.PROP_BENT):
            self.report({'ERROR'}, 'No .bent was found for this character; float channels live in the .bent')
            return {'CANCELLED'}
        if not self.templates or 'NONE' in self.templates:
            self.report({'ERROR'}, 'Pick at least one clip set')
            return {'CANCELLED'}
        if self.float_label:
            path = float_nodes(arm).get(self.float_label)
            if path is None:
                self.report({'ERROR'}, 'No float control labelled "%s"' % self.float_label)
                return {'CANCELLED'}
        else:
            bone = active_bone_name(context, arm)
            path = ch.bone_nodes(arm).get(bone)
            if path is None:
                self.report({'ERROR'}, 'The active bone is not in the rig yet (Add Selected Bones to Rig)')
                return {'CANCELLED'}
        add_edit(arm, {'op': 'channel', 'path': path, 'templates': sorted(self.templates)},
                 replace_key=lambda e: e['op'] == 'channel' and e['path'] == path)
        err = ch.refresh_channel_maps(arm)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        self.report({'INFO'}, '"%s" can now be animated by %s clips' % (
            short(path), ', '.join(sorted(self.templates))))
        return {'FINISHED'}


def _chain_order(arm, names):
    bones = arm.data.bones
    if all(re.search(r'\d+$', n) for n in names):
        return sorted(names, key=lambda n: (int(re.search(r'(\d+)$', n).group(1)), n))
    sel = set(names)
    return sorted(names, key=lambda n: sum(1 for p in bones[n].parent_recursive if p.name in sel))


def refresh_add_node_edits(arm, names):
    """Re-store the add_node edits of these bones from their current rest
    (after their rest frames were turned)."""
    edits = ch.get_edits(arm)
    bones = arm.data.bones
    mapping = ch.bone_nodes(arm)
    root = rig_root(arm)
    for e in edits:
        if e.get('op') != 'add_node':
            continue
        nm = e['path'].split('/')[1]
        if nm not in names or nm not in bones or bones[nm].parent is None:
            continue
        b = bones[nm]
        loc, q, _s = (b.parent.matrix_local.inverted() @ b.matrix_local).decompose()
        e['translation'] = list(loc)
        e['rotor'] = [-q.x, -q.y, -q.z, q.w]
    ch.set_edits(arm, edits)


class CARS2TK_OT_c2_make_chain(bpy.types.Operator):
    """Make the selected bones swing in the game as the car moves"""
    bl_idname = 'cars2tk.c2_make_chain'
    bl_label = 'Make Dynamic Chain'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        import c3toc2
        arm = _require(self, context)
        if arm is None:
            return {'CANCELLED'}
        names = selected_bone_names(context, arm)
        if len(names) < 2:
            self.report({'ERROR'}, 'Select the chain\'s bones (at least two)')
            return {'CANCELLED'}
        mapping = ch.bone_nodes(arm)
        added = {e['path'].split('/')[1] for e in ch.get_edits(arm) if e.get('op') == 'add_node'}
        order = _chain_order(arm, names)
        bones = arm.data.bones
        for k, n in enumerate(order):
            if n not in mapping or n not in added:
                self.report({'ERROR'}, '%s: add it to the rig first (Add Selected Bones to Rig) - a chain is '
                                       'made of new bones' % n)
                return {'CANCELLED'}
            if k and (bones[n].parent is None or bones[n].parent.name != order[k - 1]):
                self.report({'ERROR'}, 'Parent the chain\'s bones in a line, root first (%s under %s)'
                            % (n, order[k - 1]))
                return {'CANCELLED'}
        prev = _object_mode(context)
        try:
            context.view_layer.objects.active = arm
            bpy.ops.object.mode_set(mode='EDIT')
            eb = arm.data.edit_bones
            heads = [eb[n].head.copy() for n in order]
            z = None
            for k, n in enumerate(order):                 # +Z at the next link (the tip keeps the last)
                if k + 1 < len(order) and (heads[k + 1] - heads[k]).length > 1e-6:
                    z = (heads[k + 1] - heads[k]).normalized()
                b = eb[n]
                m = b.matrix.copy()
                x = m.col[0].xyz
                x = (x - z * x.dot(z))
                if x.length < 1e-6:
                    x = m.col[1].xyz - z * m.col[1].xyz.dot(z)
                x.normalize()
                y = z.cross(x)
                nm_ = mathutils.Matrix((x, y, z)).transposed().to_4x4()
                nm_.translation = heads[k]
                b.matrix = nm_
            bpy.ops.object.mode_set(mode='OBJECT')
            kids = [b.name for b in bones if b.parent is not None and b.parent.name in order]
            refresh_add_node_edits(arm, set(order) | set(kids))
        finally:
            _restore_mode(prev)
        name = re.sub(r'_?\d+$', '', order[0]) or order[0]
        add_edit(arm, dict(c3toc2.MATER_CHAIN, op='chain', name=name, bones=order),
                 replace_key=lambda e: e['op'] == 'chain' and set(e['bones']) & set(order))
        self.report({'INFO'}, 'Dynamic chain %s: %s' % (name, ' > '.join(order)))
        return {'FINISHED'}


class CARS2TK_OT_convert_constraints(bpy.types.Operator):
    """Turn the active bone's Copy Location / Rotation / Scale / Transforms constraints into a game constraint"""
    bl_idname = 'cars2tk.convert_constraints'
    bl_label = 'Convert Constraints'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        arm = _require(self, context)
        if arm is None:
            return {'CANCELLED'}
        bone = active_bone_name(context, arm)
        mapping = ch.bone_nodes(arm)
        if bone not in mapping:
            self.report({'ERROR'}, 'The active bone is not in the rig yet')
            return {'CANCELLED'}
        targets, ops, skipped = [], [], []
        for con in arm.pose.bones[bone].constraints:
            muted = (not con.enabled) if hasattr(con, 'enabled') else con.mute
            if con.type not in KIND_OF or muted:
                skipped.append(con.name)
                continue
            if con.target != arm or con.subtarget not in mapping:
                skipped.append('%s (target must be a rig bone of this armature)' % con.name)
                continue
            if getattr(con, 'use_offset', False) or getattr(con, 'mix_mode', 'REPLACE') not in ('REPLACE',):
                skipped.append('%s (offset / mix modes are not supported)' % con.name)
                continue
            if con.influence < 0.999:
                skipped.append('%s (partial influence is not supported)' % con.name)
                continue
            t = mapping[con.subtarget]
            if t not in targets:
                targets.append(t)
            for kind in KIND_OF[con.type]:
                ops.append({'op': kind, 'input': targets.index(t)})
        if not ops:
            if skipped:
                self.report({'ERROR'}, 'No convertible constraints on %s: %s' % (bone, '; '.join(skipped)))
            elif len(arm.constraints):
                # the most common mix-up: the Object Constraints tab
                self.report({'ERROR'}, '%s has no constraints - the one you added is on the whole '
                            'armature (Object Constraints tab). Remove it there and add it in the '
                            'Bone Constraints tab with %s active' % (bone, bone))
            else:
                # only bones the user added (imported bones carry the
                # importer's own rebuilt constraints)
                others = [pb.name for pb in arm.pose.bones
                          if pb.name != bone and pb.name in mapping and 'oct_rest' not in pb.bone and
                          any(c.type in KIND_OF for c in pb.constraints) and
                          not any(e.get('owner') == mapping[pb.name] for e in ch.get_edits(arm))]
                hint = (' (%s has one - is the right bone active?)' % ', '.join(others[:3])) if others else ''
                self.report({'ERROR'}, '%s has no constraints%s. In Pose Mode with %s active, open the '
                            'Bone Constraints tab, add Copy Location / Rotation / Scale / Transforms, '
                            'set Target to this armature and Bone to the part to copy'
                            % (bone, hint, bone))
            return {'CANCELLED'}
        order = {'orient': 0, 'point': 1, 'scale': 2}          # shipping order
        ops.sort(key=lambda o: (order[o['op']], o['input']))
        owner = mapping[bone]
        add_edit(arm, {'op': 'constraint', 'owner': owner, 'targets': targets, 'ops': ops},
                 replace_key=lambda e: e['op'] == 'constraint' and e['owner'] == owner)
        msg = '%s follows %s (%s)' % (bone, ', '.join(short(t) for t in targets),
                                     ', '.join(o['op'] for o in ops))
        if skipped:
            self.report({'WARNING'}, msg + '; skipped: ' + '; '.join(skipped))
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


class CARS2TK_OT_remove_edit(bpy.types.Operator):
    """Remove this edit"""
    bl_idname = 'cars2tk.remove_edit'
    bl_label = 'Remove Edit'
    bl_options = {'REGISTER', 'UNDO'}
    index: IntProperty()

    def execute(self, context):
        arm = _require(self, context)
        if arm is None:
            return {'CANCELLED'}
        edits = ch.get_edits(arm)
        if 0 <= self.index < len(edits):
            gone = edits.pop(self.index)
            if gone['op'] == 'add_node':                        # forget the bone mapping too
                m = ch.bone_nodes(arm)
                ch.set_bone_nodes(arm, {b: p for b, p in m.items() if p != gone['path']})
            ch.set_edits(arm, edits)
        return {'FINISHED'}


class CARS2TK_OT_validate(bpy.types.Operator):
    """Check the edited rig against the rules every shipping Cars 2 rig follows"""
    bl_idname = 'cars2tk.validate'
    bl_label = 'Validate'

    def execute(self, context):
        arm = _require(self, context)
        if arm is None:
            return {'CANCELLED'}
        from . import geometry
        problems, gwarn = [], []
        try:
            _names, notes = record_moved(arm)        # moved FX markers / rig bones
            gwarn += notes
        except Exception as e:
            gwarn.append('moved rig bones not recorded: %s' % e)
        try:
            geom = geometry.prepare(arm, problems, gwarn)
            _t, _vb, _ib, _b, log, errs, warns = rigedit.build_with_geometry(
                arm[ch.PROP_SCENE], arm.get(ch.PROP_BENT) or None, ch.get_edits(arm), geom)
        except Exception as e:
            self.report({'ERROR'}, 'Edits cannot be applied: %s' % e)
            return {'CANCELLED'}
        errs = problems + errs
        warns = gwarn + warns
        text = bpy.data.texts.get('Cars2RigReport') or bpy.data.texts.new('Cars2RigReport')
        text.clear()
        text.write('\n'.join(['EDITS'] + ['  ' + x for x in log] + ['', 'ERRORS'] + ['  ' + x for x in errs]
                             + ['', 'WARNINGS'] + ['  ' + x for x in warns]) + '\n')
        if errs:
            self.report({'ERROR'}, '%d error(s) - see the Cars2RigReport text: %s' % (len(errs), errs[0]))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Character OK (%d step(s), %d warning(s); details in Cars2RigReport)' % (
            len(log), len(warns)))
        return {'FINISHED'}


class CARS2TK_OT_export_character(bpy.types.Operator, ExportHelper):
    """Write the edited character (.oct, .bent and, with meshes, .vbuf/.ibuf) into a folder laid out like the game package"""
    bl_idname = 'cars2tk.export_character'
    bl_label = 'Export Character'
    filename_ext = ''
    use_filter_folder = True
    filepath: StringProperty(subtype='DIR_PATH')
    include_geometry: BoolProperty(name='Include meshes', default=True,
                                   description='Rebuild the geometry from the meshes in the scene '
                                               '(needed for new or changed meshes and new skin bones)')
    pack_zip: BoolProperty(name='Pack as game .zip', default=False,
                           description='Also pack the whole character package (the game archive the character came '
                                       'from, with the exported files in place of the originals) into a .zip the '
                                       'game loads, with offsetting.exe zip-c2 (path in the add-on preferences, or '
                                       'found in the assets folder of the game)')
    keep_json: BoolProperty(name='Keep JSON + Textures', default=False,
                            description='Also write <car>.oct.json and its <car>.oct.textures folder next to the '
                                        'export (offsetting.exe oct decode -t), as in the unpacked game folders')

    def invoke(self, context, event):
        arm = ch.active_character(context)
        if arm is not None:
            folder = os.path.join(os.path.dirname(arm[ch.PROP_SCENE]), ch.EXPORT_FOLDER)
            # public 2.0.8: made first - the file browser can't open a folder that doesn't exist yet and showed
            # whichever folder it had last (a cars3_as_cars2 folder next to the character)
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError:
                pass
            self.filepath = folder + os.sep
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, 'include_geometry')
        layout.prop(self, 'pack_zip')
        layout.prop(self, 'keep_json')
        if (self.pack_zip or self.keep_json) and _offsetting(context) is None:
            col = layout.column()
            col.alert = True
            col.label(text='offsetting.exe not found:', icon='ERROR')
            col.label(text='set it in the add-on preferences')

    def execute(self, context):
        arm = _require(self, context)
        if arm is None:
            return {'CANCELLED'}
        try:
            _names, notes = record_moved(arm)        # moved FX markers / rig bones
        except Exception as e:
            notes = ['moved rig bones not recorded: %s' % e]
        summary = rig_summary(arm)
        out = self.filepath if os.path.isdir(self.filepath) else os.path.dirname(self.filepath)
        geom = None
        if self.include_geometry:
            from . import geometry
            problems, gwarn = [], []
            geom = geometry.prepare(arm, problems, gwarn)
            if problems:
                self.report({'ERROR'}, 'Meshes not exportable: %s' % '; '.join(problems[:3]))
                return {'CANCELLED'}
            for w in gwarn:
                self.report({'WARNING'}, w)
        mats = None
        from . import charmat               # v2.0: the game materials (mat.c2) and the character's textures
        missing = charmat.missing(context.scene, arm)
        if missing:
            print('Cars 2 Toolkit: not exported, texture slots need a texture:')
            for line in missing:
                print('   ', line)
            self.report({'ERROR'}, 'Not exported: %d texture slot(s) need a texture: %s%s (pick textures in the '
                                   'Cars 2 Material panel; full list in the console)'
                        % (len(missing), '; '.join(missing[:3]), ' ...' if len(missing) > 3 else ''))
            return {'CANCELLED'}
        if charmat.materials_of(arm):
            mats = {'apply': charmat.exporter(context.scene, arm),
                    'car': os.path.splitext(os.path.basename(arm[ch.PROP_SCENE]))[0]}
        try:
            written, log, errs, warns = rigedit.export(arm[ch.PROP_SCENE], arm.get(ch.PROP_BENT) or None,
                                                       ch.get_edits(arm), out, geom, mats)
        except Exception as e:
            self.report({'ERROR'}, 'Export failed: %s' % e)
            return {'CANCELLED'}
        if errs:
            self.report({'ERROR'}, 'Not exported, %d error(s): %s' % (len(errs), errs[0]))
            return {'CANCELLED'}
        from . import VERSION
        text = bpy.data.texts.get('Cars2RigReport') or bpy.data.texts.new('Cars2RigReport')
        text.clear()
        text.write('\n'.join(['EXPORT (Cars 2 Toolkit %s)' % VERSION]
                              + ['  wrote ' + w for w in written]
                              + ['', 'RIG CHANGES IN THE EXPORTED .oct'] + ['  ' + x for x in summary]
                              + ['', 'NOTES'] + ['  ' + x for x in notes]
                              + ['', 'STEPS'] + ['  ' + x for x in log]) + '\n')
        from . import export_any            # public 2.0.10: the extras of every character export
        extras, packed = export_any.after_export(self, context, out, os.path.basename(arm[ch.PROP_SCENE]),
                                                 arm[ch.PROP_SCENE], self.pack_zip, self.keep_json, text)
        self.report({'INFO'}, 'Cars 2 Toolkit %s exported%s%s. Rig changes: %s' % (
            VERSION, (' with %s' % ', '.join(os.path.basename(x) for x in extras)) if extras else '',
            (' and packed: %s' % os.path.basename(packed)) if packed else '', '; '.join(summary)))
        _popup(context, ['Rig changes in the exported file:'] + ['   ' + x for x in summary]
               + ([''] + ['Note: ' + n for n in notes[:6]] if notes else []))
        return {'FINISHED'}


def _offsetting(context):
    from . import export_any
    return export_any.find_offsetting(context)


# -------------------------------------------------------------------- panel
def short(path):
    """Node path as the user knows it - the bone name ('Anchor/Spoiler_Ctrl/Transform'
    -> 'Spoiler_Ctrl'; floats keep their control: 'Main_Ctrl/WingAngle')."""
    p = path.split('/', 1)[-1]
    return p[:-len('/Transform')] if p.endswith('/Transform') else p


EDIT_TEXT = {
    'add_node': lambda e: 'Bone %s%s' % (short(e['path']), ' (placed where you posed it)' if e.get('from_pose') else ''),
    'add_float': lambda e: 'Float %s' % short(e['path']),
    'channel': lambda e: 'Animated: %s in %s' % (short(e['path']), ', '.join(e['templates'])),
    'chain': lambda e: 'Dynamic chain %s: %s' % (e['name'], ' > '.join(e['bones'])),
    'constraint': lambda e: '%s follows %s' % (short(e['owner']), ', '.join(short(t) for t in e['targets'])),
    'driven_key': lambda e: '%s.%s <- %s' % (short(e['outputs'][0][0]), e['outputs'][0][1], short(e['driver'])),
    'node_transform': lambda e: 'Moved %s%s (%s)' % (short(e['path']), ' in Pose Mode' if e.get('source') == 'pose' else '', ', '.join(
        w for k, w in (('translation', 'position'), ('rotor', 'rotation'), ('llp_rotor', 'rotation'),
                       ('scale', 'scale')) if k in e)),
}


class CARS2TK_PT_rig(bpy.types.Panel):
    bl_label = 'Cars 2 Rig'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Cars 2 Character'

    def draw(self, context):
        layout = self.layout
        arm = ch.active_character(context)
        if arm is None:
            layout.operator('cars2tk.import_character', icon='IMPORT')
            layout.label(text='Import a character to edit its rig')
            return
        from . import VERSION
        box = layout.box()
        box.label(text='%s  (Cars 2 Toolkit %s)' % (arm.name, VERSION), icon='ARMATURE_DATA')
        box.label(text='.bent: %s' % (os.path.basename(arm[ch.PROP_BENT]) if arm.get(ch.PROP_BENT) else 'not found'),
                  icon='CHECKMARK' if arm.get(ch.PROP_BENT) else 'ERROR')
        col = layout.column(align=True)
        col.label(text='Add to the game rig')
        col.operator('cars2tk.add_bones', icon='BONE_DATA')
        col.operator('cars2tk.add_channel', icon='ACTION')
        col.operator('cars2tk.c2_make_chain', icon='LINKED')
        col.operator('cars2tk.convert_constraints', icon='CONSTRAINT_BONE')
        edits = ch.get_edits(arm)
        box = layout.box()
        box.label(text='Edits (%d)' % len(edits))
        for i, e in enumerate(edits):
            row = box.row()
            row.label(text=EDIT_TEXT.get(e['op'], lambda x: x['op'])(e))
            row.operator('cars2tk.remove_edit', text='', icon='X').index = i
        row = layout.row(align=True)
        row.operator('cars2tk.validate', icon='CHECKMARK')
        row.operator('cars2tk.export_cars2', text='Export Cars 2 Character', icon='EXPORT')


# v2.0.56 (user): Make Pose the Rest Position, Record Moved Rig Bones (still run on
# Validate / Export), Add Float Control and Convert Drivers removed
classes = (CARS2TK_OT_add_bones, CARS2TK_OT_add_channel,
           CARS2TK_OT_c2_make_chain,
           CARS2TK_OT_convert_constraints, CARS2TK_OT_remove_edit,
           CARS2TK_OT_validate, CARS2TK_OT_export_character, CARS2TK_PT_rig)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
