"""Import a Cars 2 character straight from its .oct (no offsetting, no JSON,
no paths to edit) and keep what the rig tools need on the armature."""
import base64
import json
import math
import os

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

import c2oct as octio            # core modules (cars2_toolkit/core is on sys.path)
import c2rigedit as rigedit
from c2scene import SceneNetwork
from . import c2_model
from . import anim

PROP_SCENE = 'cars2_scene'          # source .oct path
PROP_BENT = 'cars2_bent'            # source .bent path ('' if none)
PROP_NODES = 'cars2_bone_nodes'     # JSON {bone name: network node path}
PROP_EDITS = 'cars2_edits'          # JSON list of recorded edits


# ------------------------------------------------------------------ helpers
def tree_to_json(v):
    """Decoded .oct tree -> the shape offsetting's JSON has (binary blobs as
    'base64:' strings), which is what the model importer reads."""
    if isinstance(v, dict):
        return {k: tree_to_json(x) for k, x in v.items()}
    if isinstance(v, (bytes, bytearray)):
        return 'base64:' + base64.b64encode(bytes(v)).decode('ascii').rstrip('=')
    if isinstance(v, list):
        return [tree_to_json(x) for x in v]
    return v


def is_character(obj):
    return obj is not None and obj.type == 'ARMATURE' and PROP_SCENE in obj.keys()


def active_character(context):
    obj = context.object
    if is_character(obj):
        return obj
    if obj is not None and obj.parent is not None and is_character(obj.parent):
        return obj.parent
    chars = [o for o in context.scene.objects if is_character(o)]
    return chars[0] if len(chars) == 1 else None


def get_edits(arm):
    return json.loads(arm.get(PROP_EDITS, '[]'))


def set_edits(arm, edits):
    arm[PROP_EDITS] = json.dumps(edits)
    refresh_channel_maps(arm)


def bone_nodes(arm):
    return json.loads(arm.get(PROP_NODES, '{}'))


def set_bone_nodes(arm, mapping):
    arm[PROP_NODES] = json.dumps(mapping)


def build(arm):
    """Apply this character's edits to its source files in memory."""
    return rigedit.build(arm[PROP_SCENE], arm.get(PROP_BENT) or None, get_edits(arm))


def refresh_channel_maps(arm):
    """Teach the animation add-on every channel this character's .bent has,
    including custom ones added by edits, so clips can be imported/exported
    on them. Returns an error string or None."""
    try:
        _t, net, bent, _log, errs, _w = build(arm)
    except Exception as e:                     # edits not applicable yet
        return str(e)
    if bent is not None:
        for tm, table in rigedit.channel_labels(bent).items():
            target = anim.BUILTIN_CHANNEL_MAPS.setdefault(tm, {})
            for slot, label in table.items():
                target.setdefault(slot, label)
    # a custom part animated through a borrowed slot: that slot is the part's
    # own bone in clips (import and Export Clip)
    for tm, table in getattr(net, '_borrowed', {}).items():
        target = anim.BUILTIN_CHANNEL_MAPS.setdefault(tm, {})
        for slot, node in table.items():
            target[slot] = rigedit.label_of(node)
    return None


# ------------------------------------------------------------ export output (v2.0.13)
EXPORT_FOLDER = 'cars2_export'    # every character export: <source folder>/cars2_export/<car>.oct + _0.vbuf/.ibuf
                                  # (public 2.0.8 USER: was 'export')


def offsetting_path():
    """offsetting.exe (turns a .oct.json into the .oct on export). public 2.0.10: found like every other use of it
    (export_any.find_offsetting: the toolkit's preference, the World Importer's, the game's assets folder), else
    the old default."""
    import c3toc2
    try:
        from . import export_any
        p = export_any.find_offsetting(bpy.context)
        if p:
            return p
    except Exception:
        pass
    return c3toc2.DEFAULT_OFFSETTING


# ------------------------------------------------------------ textures
def _pool_items(pool):
    def num(k):
        tail = k.rsplit('#', 1)[-1]
        return int(tail) if tail.isdigit() else 0
    return [v for _k, v in sorted((pool or {}).items(), key=lambda kv: num(kv[0]))]


def extract_textures(tree, data, folder):
    """Textures live inside the .oct as DDS data. Write each one into
    `folder` the way offsetting does (<name>.oct.textures/characters/<car>/
    maps/<texture>.dds, mirroring SourceFilePath) and point the texture at
    its file, so the model importer loads them exactly as it does after an
    offsetting extract. Files already there are kept. Returns the folder
    used (a temp folder if the .oct's folder isn't writable)."""
    import tempfile
    entries = _pool_items(tree.get('TexturePool'))
    for target in (folder, os.path.join(tempfile.gettempdir(), 'cars2_textures',
                                        os.path.basename(folder))):
        try:
            written = 0
            for i, tex in enumerate(entries):
                blob = tex.get('Data')
                if not isinstance(blob, (bytes, bytearray)) or blob[:4] != b'DDS ':
                    continue
                src = (tex.get('SourceFilePath') or tex.get('Name') or 'texture_%d' % i)
                parts = [x for x in src.replace('\\', '/').split('/') if x]
                base = parts[-1]
                stem = base.rsplit('.', 1)[0] if '.' in base else base
                rel = parts[:-1] + [(stem or tex.get('Name') or 'texture_%d' % i) + '.dds']
                path = os.path.join(target, *rel)
                if not (os.path.isfile(path) and os.path.getsize(path) == len(blob)):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, 'wb') as f:
                        f.write(blob)
                    written += 1
                if i < len(data.textures):
                    data.textures[i]['Data'] = 'file:' + '\\'.join(rel)
            if written:
                c2_model.log('Textures: extracted %d embedded texture(s) to %s' % (written, target))
            return target
        except OSError as e:
            c2_model.log('WARNING: could not write textures to %s (%s)' % (target, e))
    return folder


# --------------------------------------------------------------- the import
def drop_fx_markers(context, arm):
    """v2.0.6 (Import FX Markers off): delete the FX marker bones (FX_Marker_*)
    and their empty bone collection. The .oct keeps its marker nodes - export
    writes the rig network as it is, so the game still has them."""
    names = [b.name for b in arm.data.bones if b.name.startswith('FX_Marker')]
    if not names:
        return 0
    prev = context.view_layer.objects.active
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.data.edit_bones
    for n in names:
        b = eb.get(n)
        if b is not None:
            for c in b.children:             # (markers are leaves; keep anything under one)
                c.parent = b.parent
            eb.remove(b)
    bpy.ops.object.mode_set(mode='OBJECT')
    context.view_layer.objects.active = prev
    try:
        for cname in ('FX Markers', 'FX_Markers'):
            c = arm.data.collections.get(cname)
            if c is not None and not len(c.bones):
                arm.data.collections.remove(c)
    except AttributeError:
        pass
    return len(names)


def import_character(context, filepath, import_textures=True, import_markers=True):
    tree = octio.load(filepath)
    stem = os.path.splitext(os.path.basename(filepath))[0]
    folder = os.path.dirname(filepath)
    import c2geom
    # v1.90: the buffer files the .oct names (a port may name another car's)
    vbuf, ibuf = c2geom.buffer_files(filepath, tree)
    multi = c2geom.buffer_sets(filepath, tree)          # v1.94: several buffer files (mcqueenfabulous _0 + _1)
    for p in ([vbuf, ibuf] if not multi else list(multi[0].values()) + list(multi[1].values())):
        if not os.path.isfile(p):
            raise FileNotFoundError('expected %s next to the .oct' % os.path.basename(p))
    bent_path = rigedit.find_bent(filepath)
    if bent_path:                       # channel names first, so custom controls get named bones
        try:
            import c2channels
            for tm, table in rigedit.channel_labels(c2channels.Bent.load(bent_path)).items():
                target = anim.BUILTIN_CHANNEL_MAPS.setdefault(tm, {})
                for slot, label in table.items():
                    target.setdefault(slot, label)
        except Exception as e:
            c2_model.log('WARNING: could not read %s: %s' % (bent_path, e))
    data = c2_model.OctData(tree_to_json(tree))
    tex = c2_model.guess_textures_folder(filepath + '.json')     # <name>.oct.textures
    if import_textures:
        tex = extract_textures(tree, data, tex)
    use_tex = import_textures and os.path.isdir(tex)

    prefs = context.preferences.edit
    prev_undo = prefs.use_global_undo
    prefs.use_global_undo = False
    try:
        import io
        if multi:
            vb = {i: io.BytesIO(open(p, 'rb').read()) for i, p in multi[0].items()}
            ib = {i: io.BytesIO(open(p, 'rb').read()) for i, p in multi[1].items()}
        else:
            vb, ib = io.BytesIO(open(vbuf, 'rb').read()), io.BytesIO(open(ibuf, 'rb').read())
        materials = c2_model.load_materials(data, tex, use_tex)
        arm, _ = c2_model.build_armature(data, name=stem, json_path=filepath + '.json')
        meshes = c2_model.build_merged_mesh_objects(data, materials, vb, ib, arm)
        try:                                  # v2.0: game materials, the Scene Editor's way (charmat)
            from . import charmat
            charmat.setup_eyes(arm, c2_model._LAST_RIG_PLAN if arm else None)
            n = charmat.setup(context, arm, tree, materials, tex, use_tex, filepath)
            c2_model.log('Materials: %d game materials (Game Look in Rendered view, every MaterialPool setting '
                         'editable)' % n)
        except Exception as e:
            import traceback
            traceback.print_exc()
            c2_model.log('WARNING: game materials skipped: %s' % e)
        rot = math.radians(c2_model.GLOBAL_ROTATE_X_DEGREES)
        if arm:
            arm.rotation_euler = (rot, 0, 0)
            for o in meshes:
                if o.parent is None:
                    o.parent = arm
        else:
            for o in meshes:
                o.rotation_euler = (rot, 0, 0)
        if getattr(c2_model, 'MODEL_COLLECTION', False):
            col = bpy.data.collections.new(stem)
            context.scene.collection.children.link(col)
            for o in ([arm] if arm else []) + list(meshes):
                for c in list(o.users_collection):
                    c.objects.unlink(o)
                col.objects.link(o)
    finally:
        prefs.use_global_undo = prev_undo
        c2_model.flush_log()
    if arm is None:
        return None, 'no skeleton in this file (props import as meshes only)'
    if not import_markers:
        drop_fx_markers(context, arm)

    # bone -> network node, so edits can be expressed in the game's terms
    sn = tree.get('SubNetworkPool', {}).get('SubNetwork#0')
    mapping = {}
    if sn and sn.get('Type') == 'Scene':
        net = SceneNetwork.from_subnetwork(sn)
        plan = c2_model._LAST_RIG_PLAN or {}
        for bone, i in plan.get('node_of_bone', {}).items():
            if 0 <= i < len(net.data) and bone in arm.data.bones:
                mapping[bone] = net.path_of(i)
        roots = sorted({d['Header'].split('/', 1)[0] for d in net.data})
        for b in arm.data.bones:
            if b.name in mapping:
                continue
            for r in roots:
                p = '%s/%s/Transform' % (r, b.name)
                if net.has(p):
                    mapping[b.name] = p
                    break
    from . import geometry
    geometry.tag_import(arm, materials, tree)
    try:
        import c2geom
        vb_, ib_ = c2geom.read_buffers(filepath, tree)
        geometry.store_originals(arm, tree, vb_, ib_)
    except Exception as e:                       # never block an import over this
        c2_model.log('WARNING: original vertex data not stored: %s' % e)
    arm[PROP_SCENE] = os.path.abspath(filepath)
    arm[PROP_BENT] = bent_path or ''
    set_bone_nodes(arm, mapping)
    arm[PROP_EDITS] = '[]'

    refresh_channel_maps(arm)
    try:                                         # v1.97: a Cars 2 Scene Editor map in the scene lights it
        from . import c2selink
        c2selink.sync(context.scene)
    except Exception as e:
        c2_model.log('WARNING: Scene Editor map light not applied: %s' % e)
    return arm, None


class CARS2TK_OT_import_character(bpy.types.Operator, ImportHelper):
    """Import a Cars 2 character: its game bones and meshes (plain hierarchy, to rig a model and export it), or with Import Control Rig its full game rig, ready for animation and rig edits"""
    bl_idname = 'cars2tk.import_character'
    bl_label = 'Import Cars 2 Character'
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = ''
    filter_glob: StringProperty(default='*.oct;*.json', options={'HIDDEN'})
    # v2.0.6: labels renamed (ids kept, so scripts calling subnetwork= / import_textures= still work)
    subnetwork: BoolProperty(name='Import Control Rig', default=False,
                             description="Imports the character's control rig (SubNetworkPool), for animation purposes")
    import_textures: BoolProperty(name='Extract Textures', default=True,
                                  description='Writes the .oct textures to <character>.oct.textures next to it '
                                              'and loads them')
    import_markers: BoolProperty(name='Import FX Markers', default=True,
                                 description="Imports the character's FX markers, for weapons, exhausts etc.")

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, 'subnetwork')
        col.prop(self, 'import_textures')
        col.prop(self, 'import_markers')

    def execute(self, context):
        if not self.subnetwork:
            from . import template
            try:
                arm, markers, meshes = template.import_plain(context, self.filepath, self.import_textures,
                                                                 import_markers=self.import_markers)
            except Exception as e:
                self.report({'ERROR'}, str(e))
                return {'CANCELLED'}
            self.report({'INFO'}, 'Imported %s: %d game bones%s, %d meshes (no control rig - export it with '
                                  'Export as Cars 2 Character)' % (
                                      arm.name, sum(1 for b in arm.data.bones if b.use_deform),
                                      ', %d FX markers' % markers if markers else '', meshes))
            return {'FINISHED'}
        if not self.filepath.lower().endswith('.oct'):
            self.report({'ERROR'}, 'Import Control Rig needs the game .oct (the .oct.json works without it)')
            return {'CANCELLED'}
        try:
            arm, msg = import_character(context, self.filepath, self.import_textures,
                                            import_markers=self.import_markers)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        if arm is not None:
            try:
                level_extra_wheels(context, arm, self.filepath)
            except Exception as e:
                print('Cars 2 import: extra wheels not levelled: %s' % e)
        if arm is None:
            self.report({'WARNING'}, msg)
            return {'FINISHED'}
        bent = arm[PROP_BENT]
        self.report({'INFO'}, 'Imported %s: %d bones, %d mapped to the rig%s' % (
            arm.name, len(arm.data.bones), len(bone_nodes(arm)),
            '' if bent else ' (no .bent found - animation channels cannot be added)'))
        return {'FINISHED'}


def level_extra_wheels(context, arm, filepath):
    """Extra wheels exported by Cars 3 to Cars 2 (Mack's middle tires) hang
    on another wheel's hub through a ..._Level rig node that copies the
    chassis' orientation - they follow the hub's position, not its tilt.
    That node has no bone here, so show the same with the bone's Inherit
    Rotation off (it still spins by its own driver)."""
    import c2oct
    from c2scene import SceneNetwork
    tree = c2oct.load(filepath)
    net = SceneNetwork.from_subnetwork(tree['SubNetworkPool']['SubNetwork#0'])
    bones = []
    for i, n in enumerate(net.data):
        path = net.path_of(i)
        p = n.get('BasisDataNodeRef')
        if p is not None and net.path_of(p).split('/')[1].endswith('_Level'):
            nm = path.split('/')[1]
            if nm in arm.data.bones:
                bones.append(nm)
    if not bones:
        return 0
    root = 'Main' if 'Main' in arm.data.bones else None
    prev = context.view_layer.objects.active
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.data.edit_bones
    internals = arm.data.collections.get('Rig Internals') if hasattr(arm.data, 'collections') else None
    made = []
    for nm in bones:
        b = eb[nm]
        hub = b.parent
        if hub is None or root is None:
            b.use_inherit_rotation = False
            continue
        lv = eb.get(nm + '_LevelHub') or eb.new(nm + '_LevelHub')      # at the hub, chassis-oriented
        lv.head, lv.tail = hub.head.copy(), hub.head + (eb[root].tail - eb[root].head).normalized() * 0.1
        lv.roll = eb[root].roll
        lv.parent, lv.use_deform = hub, False
        ps = eb.get(nm + '_LevelPos') or eb.new(nm + '_LevelPos')      # the wheel's rest place on it
        ps.head, ps.tail, ps.roll = b.head.copy(), b.tail.copy(), b.roll
        ps.parent, ps.use_deform = lv, False
        b.use_inherit_rotation = False
        for x in (lv, ps):
            if internals is not None:
                internals.assign(x)
        made.append(nm)
    bpy.ops.object.mode_set(mode='OBJECT')
    for nm in made:
        lv = arm.pose.bones[nm + '_LevelHub']
        if not any(c.name == 'Cars2 Level' for c in lv.constraints):
            c = lv.constraints.new('COPY_ROTATION')
            c.name, c.target, c.subtarget = 'Cars2 Level', arm, root
        pb = arm.pose.bones[nm]
        if not any(c.name == 'Cars2 Level' for c in pb.constraints):
            c = pb.constraints.new('COPY_LOCATION')
            c.name, c.target, c.subtarget = 'Cars2 Level', arm, nm + '_LevelPos'
    context.view_layer.objects.active = prev
    return len(bones)


def menu_import(self, context):
    self.layout.operator(CARS2TK_OT_import_character.bl_idname, text='Cars 2 Character (.oct)')
    self.layout.operator('cars2tk.import_cars3', text='Cars 3 Character (.oct)')


@bpy.app.handlers.persistent
def _on_load(_dummy):
    for obj in bpy.data.objects:
        if is_character(obj):
            refresh_channel_maps(obj)


classes = (CARS2TK_OT_import_character,)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.app.handlers.load_post.append(_on_load)


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
