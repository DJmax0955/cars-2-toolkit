"""Custom rigs: export a model rigged to a plain Cars 2 skeleton.

The rig is just an armature whose bones carry the base character's game bone
names (Main, Body, FrontEnd, Jaw, the lips, the wheel hubs ...) in the game's
hierarchy - what a plain model importer script makes from the .oct, or one
built by hand - with the model weighted to it. FX markers are bones named
FX_Marker_<Name> (or FX_Marker_<Name>_Ctrl). No control bones, no
constraints. Export as Cars 2 Character rebuilds the base character's whole
game rig (controls, constraints, clip channels, wheel setup) around it. To
change the rig itself, import the exported character with File > Import >
Cars 2 Character (the full control rig).

The work is core/c3toc2.py (derive_rig_pose + the Cars 3 -> Cars 2 export
pipeline); this file only reads the scene.
"""
import io
import math
import os

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix

import c3toc2

from . import cars3
from . import cars3_export
from . import character
from . import c2_model

PROP_BASE = 'cars2tk_template_base'        # on the armature: the Cars 2 character last exported onto
PROP_ANIM = 'c2_template_anim'             # on a bone: animated by clips (borrows a clip slot)


# ------------------------------------------------------------------ finding it
def is_template(obj):
    return obj is not None and obj.type == 'ARMATURE' and PROP_BASE in obj.keys()


def is_other_import(obj):
    """A full Cars 2 character or a Cars 3 import (character or prop): those
    aren't custom rigs."""
    return obj is not None and obj.type == 'ARMATURE' and \
        (character.PROP_SCENE in obj.keys() or cars3.PROP_SOURCE in obj.keys()
         or cars3.PROP_PROP_SOURCE in obj.keys())


def _candidates(context):
    obj = context.active_object
    out = []
    if obj is not None:
        out.append(obj)
        if obj.parent is not None:
            out.append(obj.parent)
        out += [m.object for m in obj.modifiers if m.type == 'ARMATURE' and m.object is not None]
    return out


def _has_control_rig(obj):
    return any(b.name.endswith('_Ctrl') for b in obj.data.bones)


def _usable(o):
    """A custom rig: any armature that isn't a Cars 3 import. v1.97.2: a full
    Cars 2 import (control rig) counts too - its skin bones and rest poses are
    the plain import's (verified: cinematic_pacer_welder onto pacer gives the
    same .oct/.vbuf/.ibuf either way); the control bones don't deform and are
    left out, and the picked base's rig, materials and channels are used."""
    if o is None or o.type != 'ARMATURE':
        return False
    return cars3.PROP_SOURCE not in o.keys() and cars3.PROP_PROP_SOURCE not in o.keys()


def template_armature(context):
    """The template armature: the active one (or the active mesh's), else the
    only tagged template in the scene, else the scene's only usable armature
    with a Body bone."""
    for o in _candidates(context):
        if _usable(o):
            return o
    tagged = [o for o in context.scene.objects if is_template(o)]
    if len(tagged) == 1:
        return tagged[0]
    rigs = [o for o in context.scene.objects if _usable(o) and 'Body' in o.data.bones]
    return rigs[0] if len(rigs) == 1 else None


def why_no_template(context):
    """The error text when template_armature finds nothing - says what is wrong."""
    arms = [o for o in _candidates(context) if o is not None and o.type == 'ARMATURE']
    if arms:
        o = arms[0]
        if cars3.PROP_SOURCE in o.keys() or cars3.PROP_PROP_SOURCE in o.keys():
            return '%s is a Cars 3 import - export it with Export Cars 3 as Cars 2 (Cars 3 panel)' % o.name
        return ('%s is a full Cars 2 character import (control rig) - use Export Character in the Cars 2 Rig '
                'panel, or import the base with Import Control Rig off for a custom model' % o.name)
    obj = context.active_object
    rigs = [o for o in context.scene.objects if _usable(o)]
    if obj is None:
        head = 'Nothing is selected'
    elif obj.type == 'MESH':
        head = '%s has no armature (parent it to the rig or give it an Armature modifier)' % obj.name
    else:
        head = '%s is not an armature' % obj.name
    if len(rigs) > 1:
        return '%s - click the rig to export (armatures here: %s)' % (head, ', '.join(o.name for o in rigs[:6]))
    if rigs:
        return '%s - click the rig %s (it has no Body bone)' % (head, rigs[0].name)
    return '%s - click the armature your model is rigged to (Cars 2 bone names)' % head


def model_armature(context):
    """For the operators the Cars 3 and template panels share: the active
    Cars 3 import or template, else the scene's Cars 3 import."""
    for o in _candidates(context):
        if o is not None and o.type == 'ARMATURE' and \
                (cars3.PROP_SOURCE in o.keys() or not is_other_import(o)):
            return o
    return template_armature(context) or cars3_export.cars3_armature(context)


MARKER_GROUP = 'FX_Markers'                # the plain import's group bone the markers hang under


def is_marker(name):
    return name.startswith('FX_Marker_')      # (not the FX_Markers group bone)


def marker_node(name):
    """FX_Marker_X_Ctrl / FX_Marker_X -> the rig node FX_Marker_X."""
    return name[:-len('_Ctrl')] if name.endswith('_Ctrl') else name


# ------------------------------------------------------------------ import
def _stem(path):
    b = os.path.basename(path)
    for suf in ('.oct.json', '_oct.json', '.json', '.oct'):
        if b.lower().endswith(suf):
            return b[:-len(suf)]
    return os.path.splitext(b)[0]


def _marker_bones(tree):
    """[(bone name, 4x4 game-space Matrix)] for the base's FX markers (named
    after their rig node, FX_Marker_X)."""
    from c2scene import SceneNetwork
    sn = tree.get('SubNetworkPool', {}).get('SubNetwork#0')
    if not sn or sn.get('Type') != 'Scene':
        return []
    net = SceneNetwork.from_subnetwork(sn)
    off = c3toc2.rig_offset(tree, net)
    out = []
    for i, n in enumerate(net.data):
        path = net.path_of(i)
        nm = path.split('/')[1] if '/' in path else path
        if not is_marker(nm) or n['Type'] not in ('Transform', 'Dilation') or not path.endswith('/Transform'):
            continue
        w = c3toc2._rig_world(net, i)
        m = Matrix([list(map(float, w[r])) for r in range(4)])
        for k in range(3):
            m[k][3] += off[k]
        out.append((nm, m))
    return out


def import_plain(context, filepath, import_textures=True, with_meshes=True, import_markers=True):
    """The Cars 2 import without the SubNetwork (the game rig): the game bones
    only, as one plain hierarchy (bone head = PivotPoint, as a plain importer
    script makes it), the meshes skinned to them, materials and textures -
    ready to export as a custom rig - and the FX markers, as bones
    (FX_Marker_<Name>) in their own group: under an FX_Markers bone (under
    Body) and in the FX_Markers bone collection. -> (armature, markers, meshes)"""
    tree = c3toc2.load_base(filepath)
    stem = _stem(filepath)
    data = c2_model.OctData(character.tree_to_json(tree))
    if c3toc2.is_json(filepath):
        tex = c2_model.guess_textures_folder(filepath)       # offsetting's <name>.oct.textures
    else:
        tex = c2_model.guess_textures_folder(filepath + '.json')
        if import_textures and with_meshes:
            tex = character.extract_textures(tree, data, tex)
    use_tex = import_textures and os.path.isdir(tex)
    prefs = context.preferences.edit
    prev_undo, prev_rig = prefs.use_global_undo, c2_model.BUILD_CONTROL_RIG
    prefs.use_global_undo = False
    c2_model.BUILD_CONTROL_RIG = False              # the game bones only
    meshes = []
    try:
        arm, _ = c2_model.build_armature(data, name=stem, json_path=filepath)
        if arm is None:
            raise ValueError('no skeleton in this file (props have no bones)')
        if with_meshes:
            vb, ib = c3toc2.base_buffers(filepath, tree)
            materials = c2_model.load_materials(data, tex, use_tex)
            wrap = lambda b: {i: io.BytesIO(x) for i, x in b.items()} if isinstance(b, dict) else io.BytesIO(b)
            meshes = c2_model.build_merged_mesh_objects(data, materials, wrap(vb), wrap(ib), arm)
            try:                              # v2.0: game materials, the Scene Editor's way (charmat)
                from . import charmat
                charmat.setup(context, arm, tree, materials, tex, use_tex, filepath)
            except Exception as e:
                c2_model.log('WARNING: game materials skipped: %s' % e)
        # the FX markers, where the rig has them
        markers = _marker_bones(tree) if import_markers else []   # v2.0.6: Import FX Markers option
        context.view_layer.objects.active = arm
        bpy.ops.object.mode_set(mode='EDIT')
        eb = arm.data.edit_bones
        parent = eb.get('Body') or eb.get('Main')
        made = []
        if markers:
            grp = eb.new(MARKER_GROUP)               # the group: a plain bone at Body, no deform
            grp.head = parent.head.copy() if parent else (0.0, 0.0, 0.0)
            grp.tail = grp.head + (parent.tail - parent.head if parent else c2_model.Vector((0.0, 0.1, 0.0)))
            if parent:
                grp.roll = parent.roll
            grp.parent = parent
            grp.use_deform = False
            made.append(MARKER_GROUP)
            parent = grp
        for name, m in markers:
            b = eb.new(name)
            c2_model._set_edit_bone(b, m, 0.1)
            b.parent = parent
            b.use_deform = False
            made.append(name)
        bpy.ops.object.mode_set(mode='OBJECT')
        try:
            coll = None if not made else \
                (arm.data.collections.get(MARKER_GROUP) or arm.data.collections.new(MARKER_GROUP))
            for name in made:
                coll.assign(arm.data.bones[name])
        except AttributeError:
            pass
        for b in arm.data.bones:                    # (the full import's bookkeeping - not used here)
            for k in ('oct_rest', 'oct_data_node'):
                if k in b.keys():
                    del b[k]
        arm.rotation_euler = (math.radians(c2_model.GLOBAL_ROTATE_X_DEGREES), 0, 0)
        for o in meshes:
            if o.parent is None:
                o.parent = arm
        arm.show_in_front = True
        arm[PROP_BASE] = os.path.abspath(filepath)
        if getattr(c2_model, 'MODEL_COLLECTION', False):
            col = bpy.data.collections.new(stem)
            context.scene.collection.children.link(col)
            for o in [arm] + list(meshes):
                for c in list(o.users_collection):
                    c.objects.unlink(o)
                col.objects.link(o)
    finally:
        c2_model.BUILD_CONTROL_RIG = prev_rig
        prefs.use_global_undo = prev_undo
        c2_model.flush_log()
    return arm, len([m for m in made if m != MARKER_GROUP]), len(meshes)


# ------------------------------------------------------------------ character template (v2.0.8)
PROP_TEMPLATE = 'cars2tk_template_materials'   # on the armature: the .oct its game materials came from


def _model_meshes(context, arm):
    """The meshes rigged to arm (parented to it or deformed by it)."""
    return [o for o in context.scene.objects if o.type == 'MESH' and
            (o.parent == arm or any(m.type == 'ARMATURE' and m.object == arm for m in o.modifiers))]


def template_materials(arm):
    """The game materials a character template put on this rig."""
    from . import charmat
    return charmat.materials_of(arm) if arm.get(PROP_TEMPLATE) else []


def _norm(text):
    import re
    return re.sub(r'[^a-z0-9]', '', c3toc2._stem(text).lower())


def loose_match(texts, names):
    """Second pass after c3toc2.name_match: names compared without case,
    spaces, '_' / '-' / '.' and a plural 's' (Headlight_L -> Headlights,
    'Brake Lights' -> BrakeLights). Longest Cars 2 name wins. -> index or None."""
    best = None
    for text in texts:
        t = _norm(text or '')
        if len(t) < 4:
            continue
        for i, nm in enumerate(names):
            k = _norm(nm)
            if len(k) < 4 or k == 'error':
                continue
            keys = {k, k[:-1]} if k.endswith('s') and len(k) > 5 else {k}
            if any(x in t for x in keys) or (len(t) >= 5 and t in k):
                if best is None or len(k) > len(_norm(names[best])):
                    best = i
        if best is not None:
            return best
    return None


def object_names_material(obj_name, names):
    """v2.0.34: the base material an object's own name says it is - the name is that material's
    (Barry_Chrome), the game's mesh node name for it (bodyMesh_Barry_Chrome) or ends in _<material>
    (Car_Barry_Chrome). The longest such name wins. -> index or None.
    A model often gives several meshes one material (a chrome part split off the body keeps the body's
    material): matched by material first, the chrome mesh became Barry_Body and was merged into the body
    on export although the base has a Barry_Chrome material and the mesh is named after it."""
    o = _norm(obj_name or '')
    if len(o) < 4:
        return None
    best = None
    for i, nm in enumerate(names):
        k = _norm(nm)
        if len(k) < 4 or k == 'error':
            continue
        raw = c3toc2._stem(obj_name).lower()
        if o == k or o == 'bodymesh' + k or raw.endswith('_' + str(nm).lower()):
            if best is None or len(k) > len(_norm(names[best])):
                best = i
    return best


def game_uv(me):
    """The game shaders (Game Look and the Blender materials) read the UV map
    named Uv1, as a Cars 2 import names it; an FBX brings UVMap / map1 /
    UVChannel_1 ... The export reads the ACTIVE UV map, so that one becomes
    Uv1 (a different map already called Uv1 is renamed out of the way).
    -> old name or None."""
    uv = me.uv_layers.active
    if uv is None or uv.name == 'Uv1':
        return None
    other = me.uv_layers.get('Uv1')
    if other is not None:
        other.name = 'Uv1_old'
    was = uv.name
    uv.name = 'Uv1'
    return was


def leftover_slots(context, arm):
    """[(object, material name)] of the model's slots without a template material."""
    own = {m.as_pointer() for m in template_materials(arm)}
    out = []
    for o in _model_meshes(context, arm):
        for s in o.material_slots:
            if s.material is None or s.material.as_pointer() not in own:
                out.append((o.name, s.material.name if s.material else '(empty slot)'))
    return out


PROP_UNDO = 'cars2tk_template_undo'         # on the armature: what the template changed (JSON), for Remove


def _undo_record(arm):
    import json
    try:
        rec = json.loads(arm.get(PROP_UNDO, ''))
    except (TypeError, ValueError):
        rec = None
    if not isinstance(rec, dict):
        rec = {'base_prev': arm.get(PROP_BASE) or None, 'slots': {}, 'uv': {}, 'renamed': {}, 'faked': [],
               'images_before': sorted(im.name for im in bpy.data.images)}
    for k, d in (('slots', {}), ('uv', {}), ('renamed', {}), ('faked', []), ('images_before', [])):
        rec.setdefault(k, d)
    return rec


def _original(rec, name):
    """The model's own material recorded as `name` (renamed '<name>_fbx' when a Cars 2 material took its name)."""
    if name is None:
        return None
    return bpy.data.materials.get(rec['renamed'].get(name, name))


def import_template(context, arm, base, import_textures=True):
    """Bring a Cars 2 character's materials and textures onto a rig-ready
    model (an FBX ...): the base's MaterialPool as game materials owned by
    arm (its TexturePool in arm.c2tex, as a Cars 2 import has them), each of
    the model's material slots given the base material its material or mesh
    name carries (c3toc2.name_match, then loose_match - as the export pairs
    them). Run again, it starts over from the .oct (edits made since are lost);
    with another .oct, slots are matched again from the model's OWN materials.
    Each mesh's active UV map is renamed Uv1 (game_uv). The model's own
    materials are kept (fake user, renamed <name>_fbx where a Cars 2 material
    needs the name) and everything changed is recorded for remove_template.
    -> (matched [(object, slot material, base material)], unmatched [(object, slot name)],
        renamed UV maps [(object, old name)])"""
    import json
    from . import charmat
    if charmat.api() is None:
        raise RuntimeError('the material system is not running')
    tree = c3toc2.load_base(base)
    if c3toc2.is_json(base):
        c3toc2.inline_json_textures(tree, base)    # 'file:' refs -> DDS bytes, as a .oct holds them
    data = c2_model.OctData(character.tree_to_json(tree))
    if c3toc2.is_json(base):
        tex = c2_model.guess_textures_folder(base)
    else:
        tex = c2_model.guess_textures_folder(base + '.json')
        if import_textures:
            tex = character.extract_textures(tree, data, tex)
    use_tex = import_textures and os.path.isdir(tex)
    rec = _undo_record(arm)
    old = {m.as_pointer(): m for m in template_materials(arm)}
    prev = arm.get(PROP_TEMPLATE)
    same_base = bool(prev) and os.path.normcase(os.path.abspath(prev)) == os.path.normcase(os.path.abspath(base))
    old_index = {p: m.c2.index for p, m in old.items()} if same_base else {}
    names = [str(m.get('Name', '')) for m in data.materials]
    old_images = _template_images(arm)
    mats = c2_model.load_materials(data, tex, use_tex)
    try:
        arm.c2tex.clear()                  # a fresh texture table (charmat.setup fills it)
    except AttributeError:
        pass
    charmat.setup(context, arm, tree, mats, tex, use_tex, base)
    c2_model.flush_log()
    matched, unmatched, uvs = [], [], []
    kept = []                              # the model's own materials taken off their slots

    def pick_by_material(m, o):
        i = c3toc2.name_match({'material': m.name if m else None, 'name': o.name}, names)
        if i is None:
            i = loose_match([m.name if m else None, o.name], names)
        return i

    def pick(m, o):
        i = pick_by_material(m, o)
        # v2.0.34: a one-material mesh named after a base material gets that one, whatever material it
        # shares with other meshes (bodyMesh_Barry_Chrome with the body's material -> Barry_Chrome)
        if len(o.material_slots) <= 1:
            j = object_names_material(o.name, names)
            if j is not None:
                return j
        return i
    done_me = set()
    for o in _model_meshes(context, arm):
        if o.data.as_pointer() not in done_me:
            done_me.add(o.data.as_pointer())
            was = game_uv(o.data)
            if was:
                uvs.append((o.name, was))
                rec['uv'].setdefault(o.data.name, was)
        if o.name not in rec['slots']:     # the model as it was before any template
            rec['slots'][o.name] = [(sl.material.name if sl.material is not None else None)
                                    for sl in o.material_slots]
        own = rec['slots'][o.name]
        if not o.material_slots:
            i = pick(None, o)
            if i is not None:
                o.data.materials.append(mats[i])
                matched.append((o.name, '(none)', names[i]))
            else:
                unmatched.append((o.name, '(no material)'))
            continue
        for k, slot in enumerate(o.material_slots):
            m = slot.material
            i = None
            if m is not None and m.as_pointer() in old_index:      # same template again: same entry
                i = old_index[m.as_pointer()]
                # v2.0.34: unless that entry was only the old material-first match (not picked by hand)
                # and the mesh's name says otherwise: matched again, so importing the template once more
                # fixes a chrome mesh that an earlier version put on the body
                own_m = _original(rec, own[k]) if k < len(own) and own[k] else None
                if own_m is not None and len(o.material_slots) <= 1 and i == pick_by_material(own_m, o):
                    j = object_names_material(o.name, names)
                    if j is not None:
                        i = j
            if i is None and m is not None and m.as_pointer() in old and k < len(own):
                m = _original(rec, own[k])                         # another template: match the model's own
            if i is None:
                i = pick(m, o)
            label = (own[k] if k < len(own) and own[k] else (m.name if m else '(empty slot)'))
            if i is None or not 0 <= i < len(mats):
                if slot.material is not None and slot.material.as_pointer() in old:
                    slot.material = m                              # back to the model's own
                unmatched.append((o.name, label))
                continue
            matched.append((o.name, label, names[i]))
            if m is not None and m.as_pointer() not in old and m not in mats and m not in kept:
                kept.append(m)
            slot.material = mats[i]
    for m in old.values():                 # the previous template's materials
        m.use_fake_user = False
        if m.users == 0:
            bpy.data.materials.remove(m)
    _remove_unused_images(old_images, rec if old else None)
    for m in kept:                         # the model's own materials stay in the file (Remove puts them back)
        if not m.use_fake_user and m.name not in rec['faked']:
            m.use_fake_user = True
            rec['faked'].append(m.name)
    for i, m in enumerate(mats):
        # every Cars 2 material stays in the file (fake user), so a slot left
        # unmatched can be given one from the normal material dropdown
        m.use_fake_user = True
        if m.name != names[i]:
            other = bpy.data.materials.get(names[i])
            if other is not None and other in kept:       # the model's own material has the Cars 2 name
                orig = other.name
                other.name = orig + '_fbx'
                rec['renamed'][orig] = other.name
                if orig in rec['faked']:
                    rec['faked'][rec['faked'].index(orig)] = other.name
            if names[i] not in bpy.data.materials:
                m.name = names[i]             # the Cars 2 name (McQueen_Body, not McQueen_Body.001)
    arm[PROP_TEMPLATE] = os.path.abspath(base)
    arm[PROP_BASE] = os.path.abspath(base)
    arm[PROP_UNDO] = json.dumps(rec)
    return matched, unmatched, uvs


def _template_images(arm):
    try:
        return [t.image for t in arm.c2tex if getattr(t, 'image', None) is not None]
    except AttributeError:
        return []


def _remove_unused_images(images, rec=None):
    """Remove these images when nothing uses them - and (rec) every image made
    since the first template that nothing uses (the template's textures)."""
    for im in images:
        try:
            if im.users == 0:
                bpy.data.images.remove(im)
        except ReferenceError:
            pass
    if rec is not None:
        before = set(rec.get('images_before', ()))
        for im in list(bpy.data.images):
            if im.name not in before and im.users == 0:
                bpy.data.images.remove(im)


def remove_template(context, arm):
    """Undo Import Character Template: the model's own materials back in their
    slots (and their own names), its UV map names back, the template's
    materials, textures and settings removed. -> (slots restored, uv maps restored)"""
    import json
    if not arm.get(PROP_TEMPLATE):
        raise RuntimeError('%s has no character template' % arm.name)
    try:
        rec = json.loads(arm.get(PROP_UNDO, ''))
    except (TypeError, ValueError):
        rec = None
    if not isinstance(rec, dict):
        raise RuntimeError('this template was imported by an early test build (the model\'s own materials were not kept) - '
                           'use Ctrl+Z, or import the FBX again')
    tmats = template_materials(arm)
    tptr = {m.as_pointer() for m in tmats}
    images = _template_images(arm)
    renamed = rec.get('renamed', {})
    n_slots = 0
    for oname, own in rec.get('slots', {}).items():
        o = bpy.data.objects.get(oname)
        if o is None or o.type != 'MESH':
            continue
        if not own:                        # it had no material: the one the template added goes
            if any(sl.material is not None and sl.material.as_pointer() in tptr for sl in o.material_slots):
                o.data.materials.clear()
                n_slots += 1
            continue
        for k, slot in enumerate(o.material_slots):
            if k < len(own) and slot.material is not None and slot.material.as_pointer() in tptr:
                slot.material = _original(rec, own[k])
                n_slots += 1
    for m in tmats:                        # the template's materials (also any you gave to other slots)
        m.use_fake_user = False
    for o in context.scene.objects:        # a template material picked by hand for a slot: emptied
        if o.type == 'MESH':
            for slot in o.material_slots:
                if slot.material is not None and slot.material.as_pointer() in tptr:
                    slot.material = None
    for m in tmats:
        if m.users == 0:
            bpy.data.materials.remove(m)
    for orig, cur in renamed.items():      # the model's own names back
        m = bpy.data.materials.get(cur)
        if m is not None and orig not in bpy.data.materials:
            m.name = orig
    for name in rec.get('faked', []):
        m = bpy.data.materials.get(name) or bpy.data.materials.get(
            next((o for o, c in renamed.items() if c == name), ''))
        if m is not None:
            m.use_fake_user = False
    n_uv = 0
    for mname, was in rec.get('uv', {}).items():
        me = bpy.data.meshes.get(mname)
        if me is None:
            continue
        uv = me.uv_layers.get('Uv1')
        if uv is not None:
            uv.name = was
            n_uv += 1
        other = me.uv_layers.get('Uv1_old')
        if other is not None:
            other.name = 'Uv1'
    try:
        arm.c2tex.clear()
    except AttributeError:
        pass
    from . import charmat
    charmat.drop_texture_cache(arm)
    _remove_unused_images(images, rec)
    for k in (PROP_TEMPLATE, PROP_UNDO, 'c2_source', 'c2_stem', 'c2_effects_dir', 'c2_assets', 'c2tex_version'):
        if k in arm.keys():
            del arm[k]
    if rec.get('base_prev'):
        arm[PROP_BASE] = rec['base_prev']
    elif PROP_BASE in arm.keys():
        del arm[PROP_BASE]
    return n_slots, n_uv


class CARS2TK_OT_remove_template(bpy.types.Operator):
    """Remove the character template: your model's own materials and UV map names come back, the template's materials and textures are removed"""
    bl_idname = 'cars2tk.remove_template'
    bl_label = 'Remove Character Template'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        arm = template_armature(context)
        if arm is None:
            self.report({'ERROR'}, why_no_template(context))
            return {'CANCELLED'}
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        name = os.path.basename(arm.get(PROP_TEMPLATE) or '')
        try:
            n_slots, n_uv = remove_template(context, arm)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Template %s removed: %d material slot(s) and %d UV map name(s) restored'
                    % (name, n_slots, n_uv))
        return {'FINISHED'}


class CARS2TK_OT_import_template(bpy.types.Operator, ImportHelper):
    """Pick the Cars 2 character this model is based on: its materials and textures go onto your meshes (matched by material or mesh name). On export only what you changed in them is written; bone positions come from your rig"""
    bl_idname = 'cars2tk.import_template'
    bl_label = 'Import Character Template'
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = ''
    filter_glob: StringProperty(default='*.oct;*.json', options={'HIDDEN'})
    filepath: StringProperty(subtype='FILE_PATH')
    import_textures: BoolProperty(name='Extract Textures', default=True,
                                  description='Writes the .oct textures to <character>.oct.textures next to it '
                                              'and loads them')

    def invoke(self, context, event):
        arm = template_armature(context)
        if arm is None:
            self.report({'ERROR'}, why_no_template(context))
            return {'CANCELLED'}
        last = arm.get(PROP_BASE)
        if last and os.path.isfile(last):
            self.filepath = last
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        arm = template_armature(context)
        if arm is None:
            self.report({'ERROR'}, why_no_template(context))
            return {'CANCELLED'}
        base = bpy.path.abspath(self.filepath) if self.filepath else ''
        if not base or not os.path.isfile(base) or not base.lower().endswith(('.oct', '.json')):
            self.report({'ERROR'}, "Select the Cars 2 character's .oct")
            return {'CANCELLED'}
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            matched, unmatched, uvs = import_template(context, arm, base, self.import_textures)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, 'Template not imported: %s' % e)
            return {'CANCELLED'}
        text = bpy.data.texts.get('CharacterTemplateReport') or bpy.data.texts.new('CharacterTemplateReport')
        text.clear()
        text.write('\n'.join(['CHARACTER TEMPLATE: ' + base, '', 'MATCHED (mesh: your material -> Cars 2 material)']
                             + ['  %s: %s -> %s' % x for x in matched]
                             + ['', 'NOT MATCHED - pick a Cars 2 material for these in the material slot '
                                    '(all of them are in the file), else they export as new materials']
                             + ['  %s: %s' % x for x in unmatched]
                             + ['', 'UV MAP RENAMED TO Uv1 (the name the game shaders read)']
                             + ['  %s: %s' % x for x in uvs]) + '\n')
        for o, m in unmatched[:5]:
            self.report({'WARNING'}, '%s: %s has no Cars 2 material of that name' % (o, m))
        self.report({'INFO'}, 'Template %s: %d material slot(s) matched, %d not - list in the '
                              'CharacterTemplateReport text' % (os.path.basename(base), len(matched), len(unmatched)))
        return {'FINISHED'}


# ------------------------------------------------------------------ export
def game_space(arm):
    """armature space -> game space, as the model is SEEN in Blender.
    v2.0.22: the rotation is cars3_export.game_frame (from the
    2.0.20 fix: Rx(-90) . the armature's world rotation,
    read from matrix_basis up the parent chain, snapped to exact 0/+-1); on top
    of it the armature's world SCALE (an FBX from Maya / Max: rig in cm,
    armature scaled 0.01 - without it the model came out 100x too big).
    Location is not applied (the rig's zero stays the game origin).
    None = identity (every toolkit import, Blender FBX round trip) - exported
    exactly as before."""
    g = cars3_export.game_frame(arm)
    world, o = Matrix.Identity(4), arm
    while o is not None:
        world = o.matrix_parent_inverse @ o.matrix_basis @ world if o.parent is not None else o.matrix_basis @ world
        o = o.parent
    s = world.to_scale()
    if max(abs(x - 1.0) for x in s) > 1e-6:
        g = g @ Matrix.Diagonal((s[0], s[1], s[2], 1.0))
    if max(abs(g[r][c] - (1.0 if r == c else 0.0)) for r in range(4) for c in range(4)) < 1e-6:
        return None
    return g


def _game_rows(b, G):
    """bone b's rest matrix in game space, row-vector flat 16 (rotation without scale)."""
    m = b.matrix_local
    if G is not None:
        loc, rot, _scale = (G @ m).decompose()
        m = Matrix.LocRotScale(loc, rot, None)
    rv = m.transposed()
    return [rv[r][c] for r in range(4) for c in range(4)]


def template_pose(arm, game_bones=(), G=None):
    """{bone: row-vector flat 16, game space} - the skin bones and FX
    markers. game_bones (the base's bone names) count even with Deform off
    (root bones without weights often have it unticked). G = game_space(arm)."""
    out = {}
    for b in arm.data.bones:
        if b.name != MARKER_GROUP and (b.use_deform or is_marker(b.name) or b.name in game_bones):
            out[b.name] = _game_rows(b, G)
    return out


def template_skeleton(arm, game_bones=(), G=None):
    """{skin bone: (row-vector flat 16, parent)} - every deforming bone (not
    markers), and every base game bone even with Deform off."""
    out = {}
    for b in arm.data.bones:
        if not (b.use_deform or b.name in game_bones) or is_marker(b.name) or b.name == MARKER_GROUP:
            continue
        out[b.name] = (_game_rows(b, G), b.parent.name if b.parent else None)
    return out


def _hubs_on_spin(skeleton, pose):
    """v2.0.23: each wheel's hub = its SpinJoint (the bone the tire mesh is
    weighted to). A port can leave TireX_centerJoint somewhere else (King:
    rear / front-right centerJoints 35 cm off their wheels, all 8 cm low);
    the export puts the SpinJoint and the wheel rig on the centerJoint, so
    the wheels landed there in game. Here the centerJoint is moved onto its
    SpinJoint first (stock cars have them together: nothing changes).
    skeleton / pose changed in place -> notes"""
    moved = []
    for w in ('FLeft', 'FRight', 'BLeft', 'BRight'):
        c, s = 'Tire%s_centerJoint' % w, 'Tire%s_SpinJoint' % w
        if c not in skeleton or s not in skeleton:
            continue
        sp = skeleton[s][0][12:15]
        cp = skeleton[c][0][12:15]
        d = max(abs(sp[k] - cp[k]) for k in range(3))
        if d < 1e-4:
            continue
        for tbl in (skeleton, pose):
            if c in tbl:
                rows = tbl[c][0] if tbl is skeleton else tbl[c]
                rows[12:15] = list(sp)
        moved.append('%s (%.0f mm)' % (w, d * 1000))
    return (['wheel hubs taken from the SpinJoints (centerJoint was off its wheel): %s' % ', '.join(moved)]
            if moved else [])


def _facing_problem(tree, skeleton):
    """The model faces another way than the base (front axle -> back axle, from
    the wheel bones): the game would drive it sideways / backwards."""
    st = c3toc2.c2geom.plist(tree['SceneTreeNodePool'])
    piv = {n['NodeName']: n['PivotPoint'] for n in st if n.get('Type') == 'Bone'}
    F = ('TireFLeft_centerJoint', 'TireFRight_centerJoint')
    B = ('TireBLeft_centerJoint', 'TireBRight_centerJoint')
    if not all(n in piv and n in skeleton for n in F + B):
        return None

    def axis(get):
        f = [sum(get(n)[k] for n in F) / 2 for k in range(3)]
        b = [sum(get(n)[k] for n in B) / 2 for k in range(3)]
        d = [f[k] - b[k] for k in (0, 2)]               # along the ground (game x / z)
        ln = math.hypot(*d)
        return [x / ln for x in d] if ln > 1e-6 else None
    mine = axis(lambda n: skeleton[n][0][12:15])
    base = axis(lambda n: piv[n])
    if mine is None or base is None or mine[0] * base[0] + mine[1] * base[1] > 0.7:
        return None
    return ('your model faces another way than the base (front wheels -> back wheels): in Blender its front '
            'must point the way an imported Cars 2 character points (-Y, the Front view). Rotate the model '
            'and its armature, then export again')


def _body_problem(arm):
    """Why the rig has no usable Body bone - names what is there instead."""
    names = [b.name for b in arm.data.bones]
    near = [n for n in names if n.split('.')[0].split(':')[-1].split('_')[-1].strip().lower() == 'body']
    if near:
        return ('no bone named exactly "Body" - rename %s to Body (case, prefixes and .001 suffixes count)'
                % ', '.join(repr(n) for n in near))
    roots = [b.name for b in arm.data.bones if b.parent is None]
    return ('no Body bone in %s - the bones must carry the Cars 2 game bone names (Main > Body > FrontEnd ..., '
            'Tire..._centerJoint). Root bones now: %s' % (arm.name, ', '.join(roots[:8]) or 'none'))


def anim_bones_of(arm):
    return [b.name for b in cars3_export._bones_now(arm) if b.get(PROP_ANIM)]


def set_anim_bone(arm, name, on):
    for coll in ((arm.data.edit_bones, arm.data.bones) if arm.mode == 'EDIT' else (arm.data.bones,)):
        b = coll.get(name)
        if b is None:
            continue
        if on:
            b[PROP_ANIM] = True
        elif PROP_ANIM in b:
            del b[PROP_ANIM]


def export_template(context, arm, base, out, keep_eyes=False, marker_rotation=False):
    """-> (written, notes, warnings, log, errors)"""
    import c2rigedit
    problems, warnings, notes = [], [], []
    tree = c3toc2.load_base(base)
    base_bones = {n['NodeName'] for n in c3toc2.c2geom.plist(tree['SceneTreeNodePool']) if n.get('Type') == 'Bone'}
    mats = [str(m) for m in c3toc2.template_info(tree)['materials']]

    def material_of(obj):
        # a mesh without materials made by a plain importer script, named after
        # the base's mesh node (bodyMesh_McQueen_Body): that node's material
        stem = obj.name.split('.')[0]
        for m in sorted(mats, key=len, reverse=True):
            if stem.lower().endswith(m.lower()):
                warnings.append('%s: no material - using the base\'s %s (from its name)' % (obj.name, m))
                return m
        return None
    G = game_space(arm)
    if G is not None:
        notes.append('orientation: taken as the model is seen in Blender (armature turned %s, scaled %s)'
                     % (tuple(round(math.degrees(x), 1) for x in arm.matrix_world.to_euler()),
                        tuple(round(x, 4) for x in arm.matrix_world.to_scale())))
    parts = cars3_export.collect_parts(arm, None, set(context.scene.objects.keys()), problems, warnings,
                                       material_of,
                                       world_to=None if G is None else G @ arm.matrix_world.inverted())
    if problems:
        return None, notes, warnings, [], problems
    if not parts:
        return None, notes, warnings, [], ['no meshes under %s (parent them to it / give them an Armature '
                                           'modifier)' % arm.name]
    skeleton = template_skeleton(arm, base_bones, G)
    pose = template_pose(arm, base_bones, G)
    if 'Body' not in skeleton:
        return None, notes, warnings, [], [_body_problem(arm)]
    notes += _hubs_on_spin(skeleton, pose)
    facing = _facing_problem(tree, skeleton)
    if facing:
        return None, notes, warnings, [], [facing]
    if 'Main' not in skeleton:
        # plain importer scripts often make Main the armature object instead of
        # a bone (root bones Body + the hubs): Main then stays where the base
        # has it - the ground origin - like any other base bone the rig lacks
        notes.append('no Main bone: Main stays where the base has it (the ground origin); %s are taken as its '
                     'children' % ', '.join(b.name for b in arm.data.bones if b.parent is None))
    missing = sorted(b for b in base_bones if b not in skeleton and b != 'Main')
    if missing:
        # v1.97.2: expected (a simpler rig - bg / cinematic models have no
        # cheek-lower / lip-corner / tongue bones) and handled: report only
        notes.append('bones of the base not in your rig stay where the base has them: %s'
                     % ', '.join(missing[:10]) + (' ...' if len(missing) > 10 else ''))
    pose, derived, n0 = c3toc2.derive_rig_pose(tree, pose)
    notes += n0
    no_turn = set(derived)
    if not marker_rotation:
        # markers the base has keep its rotation (a bone's roll rarely matches
        # the way the effect should point); new markers take the bone's
        from c2scene import SceneNetwork
        net0 = SceneNetwork.from_subnetwork(tree['SubNetworkPool']['SubNetwork#0'])
        no_turn |= {b for b in pose if is_marker(b) and net0.has('Anchor/%s/Transform' % marker_node(b))}
    marked = cars3_export.new_bones_of(arm)
    bad = ['%s: %s' % (n, why) for n in marked for why in [cars3_export.check_new_bone(arm, n)] if why]
    chains = cars3_export.chains_of(arm)
    bad += ['chain %s: %s' % (c['name'], why) for c in chains for why in [cars3_export.check_chain(arm, c)] if why]
    anim = anim_bones_of(arm)
    for b in anim:
        if b in base_bones:
            warnings.append('%s: already a bone of the base - its own control animates it (not borrowed)' % b)
        elif b not in skeleton:
            bad.append('%s: animated in clips, but it is not a skin bone' % b)
    if bad:
        return None, notes, warnings, [], bad
    ports = {'Anchor/%s_Ctrl/Transform' % b: None for b in anim if b not in base_bones and b in skeleton}
    keep, vb2, ib2 = [], None, None
    if keep_eyes:
        keep = [m for m in c3toc2.template_info(tree)['materials'] if 'eye' in str(m).lower()]
        vb2, ib2 = c3toc2.base_buffers(base, tree)
    bent_path = c2rigedit.find_bent(base)
    bent_obj = None
    if bent_path:
        import c2channels
        try:
            bent_obj = c2channels.Bent.load(bent_path)
        except Exception as e:
            notes.append('.bent not read (%s)' % e)
    node_of_bone = {b: marker_node(b) for b in pose if is_marker(b) and marker_node(b) != b}
    frontend = any(p_.lower() == 'frontend' for p_ in os.path.normpath(base).split(os.sep))
    geom, n2, problems, w2 = c3toc2.build_geometry(
        tree, parts, (0.0, 0.0, 0.0), keep, vb2, ib2, skeleton, marked, chains, pose, node_of_bone,
        ports, bent_obj, True, no_turn=no_turn)
    notes += n2
    warnings += w2
    if problems or geom is None:
        return None, notes, warnings, [], problems or ['nothing to export']
    geom['axle_zero'] = not frontend
    notes.append('frontend package: the rig keeps the base\'s zero (no axle shift)' if frontend else
                 'in-race package: the rig\'s zero goes to the front axle (as the game places cars)')
    apply = None
    # v2.0.26: also a character imported the normal way (its game materials came from arm['c2_source'],
    # no template): before, File > Export dropped every material edit of it silently (effect switch,
    # glow map, Emissive Multiplier, Emissive Property)
    from . import charmat as _cm
    tpl = arm.get(PROP_TEMPLATE) or (arm.get('c2_source') if _cm.materials_of(arm) else None)
    if tpl and _cm.materials_of(arm):                   # v2.0.8: the character template's materials
        same = os.path.normcase(os.path.abspath(tpl)) == os.path.normcase(os.path.abspath(base))
        if not same:
            warnings.append('materials came from template %s, exporting onto %s - material edits not written '
                            "(the base's own materials are used)" % (os.path.basename(tpl), os.path.basename(base)))
        else:
            from . import charmat
            gaps = charmat.missing(context.scene, arm)
            if gaps:
                return None, notes, warnings, [], ['%d texture slot(s) need a texture: %s' % (
                    len(gaps), '; '.join(gaps[:3]))]
            apply = charmat.exporter(context.scene, arm)
            notes.append('materials and textures: %s, only your changes written' % ('from the template' if arm.get(PROP_TEMPLATE) else 'your material edits'))
    written, log, errs, w3 = c3toc2.export_onto(base, geom, out, bent_path, apply, character.offsetting_path())
    return written, notes, warnings + w3, log, errs


class CARS2TK_OT_export_template(bpy.types.Operator, ImportHelper):
    """Pick the Cars 2 character to build on (its rig, materials and animation channels are kept) and export your model, rigged to its plain skeleton, on its full rig"""
    bl_idname = 'cars2tk.export_template'
    bl_label = 'Export as Cars 2 Character'
    bl_options = {'REGISTER'}
    filename_ext = ''
    filter_glob: StringProperty(default='*.oct;*.json', options={'HIDDEN'})
    filepath: StringProperty(subtype='FILE_PATH')
    # v2.0.14/15: no dialog options (user: not needed). The result always goes to an 'export' folder next to
    # the Cars 2 character picked (<car>.oct + <car>_0.vbuf/.ibuf); your model's meshes are always used, and
    # markers the base has keep its rotation.

    def invoke(self, context, event):
        arm = template_armature(context)
        if arm is None:
            self.report({'ERROR'}, why_no_template(context))
            return {'CANCELLED'}
        last = arm.get(PROP_BASE)
        if last and os.path.isfile(last):
            self.filepath = last
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        arm = template_armature(context)
        if arm is None:
            self.report({'ERROR'}, why_no_template(context))
            return {'CANCELLED'}
        if arm.mode == 'EDIT':
            arm.update_from_editmode()
        base = bpy.path.abspath(self.filepath) if self.filepath else ''
        if not base or not os.path.isfile(base) or not base.lower().endswith(('.oct', '.json')):
            self.report({'ERROR'}, "Select the Cars 2 character's .oct or .oct.json to build on")
            return {'CANCELLED'}
        out = os.path.join(os.path.dirname(base), character.EXPORT_FOLDER)
        try:
            written, notes, warnings, log, errs = export_template(context, arm, base, out)
        except Exception as e:
            self.report({'ERROR'}, 'Export failed: %s' % e)
            return {'CANCELLED'}
        if errs:
            self.report({'ERROR'}, 'Not exported: %s' % '; '.join(errs[:3]))
            return {'CANCELLED'}
        arm[PROP_BASE] = base
        from . import VERSION
        text = bpy.data.texts.get('CustomRigReport') or bpy.data.texts.new('CustomRigReport')
        text.clear()
        text.write('\n'.join(['CUSTOM RIG -> CARS 2 EXPORT (Cars 2 Toolkit %s)' % VERSION, '',
                              'built on ' + base, '']
                             + ['  wrote ' + w for w in written]
                             + ['', 'MODEL'] + ['  ' + x for x in notes]
                             + ['', 'WARNINGS'] + ['  ' + x for x in warnings]
                             + ['', 'STEPS'] + ['  ' + x for x in log]) + '\n')
        for w in warnings[:5]:
            self.report({'WARNING'}, w)
        self.report({'INFO'}, 'Wrote %d files to %s - details in the CustomRigReport text' % (len(written), out))
        return {'FINISHED'}


class CARS2TK_OT_template_anim(bpy.types.Operator):
    """Animate the selected new bones in clips: each borrows an empty clip slot of the base (Mater's tow-crane slots first) and gets a control, built like the face"""
    bl_idname = 'cars2tk.template_anim'
    bl_label = 'Animate in Clips'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from .rig import selected_bone_names
        arm = template_armature(context)
        if arm is None:
            self.report({'ERROR'}, 'No custom rig selected')
            return {'CANCELLED'}
        names = selected_bone_names(context, arm)
        if not names:
            self.report({'ERROR'}, 'Select the bones to animate (Edit Mode or Pose Mode)')
            return {'CANCELLED'}
        done = []
        for n in names:
            why = cars3_export.check_new_bone(arm, n)
            if why:
                self.report({'WARNING'}, '%s: %s' % (n, why))
                continue
            set_anim_bone(arm, n, True)
            cars3_export.set_new_bone(arm, n, True)      # added even without weights
            done.append(n)
        if not done:
            return {'CANCELLED'}
        self.report({'INFO'}, 'Animated in clips on export: %s' % ', '.join(done))
        return {'FINISHED'}


class CARS2TK_OT_template_unanim(bpy.types.Operator):
    """Don't animate this bone in clips (it still rides along)"""
    bl_idname = 'cars2tk.template_unanim'
    bl_label = 'Remove'
    bl_options = {'REGISTER', 'UNDO'}
    name: StringProperty()

    def execute(self, context):
        arm = template_armature(context)
        if arm is not None:
            set_anim_bone(arm, self.name, False)
        return {'FINISHED'}


class CARS2TK_PT_template(bpy.types.Panel):
    bl_label = 'Custom Model'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Cars 2 Character'

    def draw(self, context):
        arm = template_armature(context)
        col = self.layout.column(align=True)
        if arm is None:
            col.label(text='Select the armature your model is rigged to')
            col.label(text='(game bones with Cars 2 names).')
            return
        base = arm.get(PROP_BASE)
        col.label(text='%s - base: %s' % (arm.name, os.path.basename(base) if base else 'pick on export'))
        tpl = arm.get(PROP_TEMPLATE)
        if tpl:
            row = col.row(align=True)
            row.label(text='Template: %s (%d materials)' % (os.path.basename(tpl), len(template_materials(arm))),
                      icon='MATERIAL')
            row.operator(CARS2TK_OT_remove_template.bl_idname, text='', icon='X')
        if not tpl:                        # v2.0.12: with a template, its X removes it (then import another)
            self.layout.operator(CARS2TK_OT_import_template.bl_idname, icon='IMPORT')
        if tpl:
            left = leftover_slots(context, arm)
            if left:
                box = self.layout.box()
                box.label(text='Not Cars 2 materials (%d):' % len(left), icon='ERROR')
                for o, m in left[:12]:
                    box.label(text='%s: %s' % (o, m))
                if len(left) > 12:
                    box.label(text='... %d more' % (len(left) - 12))
                box.label(text='Pick a Cars 2 material in the slot')
                box.label(text='(Material tab), or they export as new.')
        col = self.layout.column(align=True)
        col.operator(cars3_export.CARS2TK_OT_c3_add_bones.bl_idname, icon='BONE_DATA')
        col.operator(CARS2TK_OT_template_anim.bl_idname, icon='ACTION')
        col.operator(cars3_export.CARS2TK_OT_c3_make_chain.bl_idname, icon='LINKED')
        marked = cars3_export.new_bones_of(arm)
        anim = set(anim_bones_of(arm))
        if marked or anim:
            box = self.layout.box()
            box.label(text='New bones (%d)' % len(set(marked) | anim))
            for n in sorted(set(marked) | anim):
                row = box.row(align=True)
                why = cars3_export.check_new_bone(arm, n)
                row.label(text=n + ('  (clips)' if n in anim else ''), icon='ERROR' if why else 'BONE_DATA')
                if n in anim:
                    row.operator(CARS2TK_OT_template_unanim.bl_idname, text='', icon='ACTION').name = n
                row.operator(cars3_export.CARS2TK_OT_c3_remove_bone.bl_idname, text='', icon='X').name = n
        chains = cars3_export.chains_of(arm)
        if chains:
            box = self.layout.box()
            box.label(text='Dynamic chains (%d)' % len(chains))
            for c in chains:
                row = box.row(align=True)
                why = cars3_export.check_chain(arm, c)
                row.label(text='%s: %d links' % (c['name'], len(c['bones'])), icon='ERROR' if why else 'LINKED')
                row.operator(cars3_export.CARS2TK_OT_c3_remove_chain.bl_idname, text='', icon='X').name = c['name']
        self.layout.operator('cars2tk.export_cars2', text='Export Cars 2 Character', icon='EXPORT')


def menu_export(self, context):
    self.layout.operator(CARS2TK_OT_export_template.bl_idname, text='Cars 2 Character (.oct)')


classes = (CARS2TK_OT_import_template, CARS2TK_OT_remove_template, CARS2TK_OT_export_template, CARS2TK_OT_template_anim,
           CARS2TK_OT_template_unanim, CARS2TK_PT_template)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    # public 2.0.10: File > Export > Cars 2 Character is export_any's (every kind of character)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
