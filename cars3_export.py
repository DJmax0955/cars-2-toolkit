"""Export an imported Cars 3 character as a Cars 2 character.

the Cars 3 to Cars 2 panel (v2.0.56: no File > Export entry) in
the Cars 2 Character tab. Pick the Cars 2 character to build on (its .oct; the .bent,
.vbuf and .ibuf are found next to it) and the folder to write to.

The Cars 2 character keeps its rig, skeleton, materials and animation
channels; the meshes of the Cars 3 model - as they are in the scene now, so
delete or edit what you like first - replace its geometry. The conversion
itself is core/c3toc2.py (plain Python, tested outside Blender); this file
only reads the scene.
"""
import json
import math
import os
import re

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

import c2oct
import c2rigedit
import c3toc2

from . import cars3
from . import geometry

PROP_TEMPLATE = 'cars2tk_c3_template'       # on the armature: the Cars 2 .oct used last time
PROP_NEW_BONE = 'c3_new_bone'               # on a bone: add it to the Cars 2 rig on export


def _bones_now(arm):
    """The bones as they are now: in Edit Mode Blender keeps its own copies
    and writes them back over the bones when you leave it, so a mark has to
    be read from / set on those copies then."""
    return arm.data.edit_bones if arm.mode == 'EDIT' else arm.data.bones


def new_bones_of(arm):
    return [b.name for b in _bones_now(arm) if b.get(PROP_NEW_BONE)]


def set_new_bone(arm, name, on):
    for coll in ((arm.data.edit_bones, arm.data.bones) if arm.mode == 'EDIT' else (arm.data.bones,)):
        b = coll.get(name)
        if b is None:
            continue
        if on:
            b[PROP_NEW_BONE] = True
        elif PROP_NEW_BONE in b:
            del b[PROP_NEW_BONE]


def chains_of(arm):
    try:
        return json.loads(arm.get(cars3.PROP_CHAINS, '[]'))
    except ValueError:
        return []


def set_chains(arm, chains):
    arm[cars3.PROP_CHAINS] = json.dumps(chains)


def check_chain(arm, chain):
    """None if every bone of the chain can be one, else why not."""
    if len(chain['bones']) < 2:
        return 'a chain needs at least two bones'
    for b in chain['bones']:
        why = check_new_bone(arm, b) if b in _bones_now(arm) else 'no bone %s' % b
        if why:
            return '%s: %s' % (b, why)
    return None


def check_new_bone(arm, name):
    """None if the bone can go into the Cars 2 rig, else why not."""
    b = _bones_now(arm).get(name)
    if b is None:
        return 'it no longer exists'
    if not b.use_deform:
        return 'it is a control/rig bone (only skin bones - bones meshes deform with - become game bones)'
    if b.parent is None:
        return 'it has no parent - parent it to a skin bone such as Body, Main or a wheel hub'
    if not b.parent.use_deform:
        return ('its parent %s is a control/rig bone - parent it to a skin bone such as Body, '
                'Main or a wheel hub' % b.parent.name)
    return None


def cars3_armature(context):
    """The imported Cars 3 armature: the active object, its armature, or the first one in the scene."""
    obj = context.active_object
    cands = []
    if obj is not None:
        cands.append(obj)
        if obj.parent is not None:
            cands.append(obj.parent)
        cands += [m.object for m in obj.modifiers if m.type == 'ARMATURE' and m.object is not None]
    cands += list(context.scene.objects)
    for o in cands:
        if o is not None and o.type == 'ARMATURE' and cars3.PROP_SOURCE in o.keys():
            return o
    return None


def _model(context):
    """The Cars 3 import or rig template the shared buttons work on."""
    from . import template
    return template.model_armature(context)


def game_frame(arm):
    """Armature space -> game space (v2.0.19): what stands upright in Blender
    exports upright, whichever way the armature's data is stored.

    The game is Y-up; Blender is Z-up. The toolkit's own imports keep the
    data in game space and stand it up with a +90 degree X turn on the
    armature OBJECT, so for them this is exactly the identity and nothing
    changes. A model built in Blender (an .fbx, a rig with its rotation
    applied) has Z-up data and no object turn: taken as it was, it went into
    the game lying on its back or standing on its nose. This turns the
    armature's own world orientation (location and scale are left as they
    were) back by the import's +90 X, i.e. Blender's up (+Z) becomes the
    game's up (+Y) and Blender's front (-Y) the game's front (+Z)."""
    from mathutils import Matrix
    # the world rotation from the objects' own transforms, not matrix_world: that is only updated when Blender
    # next evaluates the scene (a script importing and exporting in one go would see the old value)
    world, o = Matrix.Identity(4), arm
    while o is not None:
        world = o.matrix_parent_inverse @ o.matrix_basis @ world if o.parent is not None else o.matrix_basis @ world
        o = o.parent
    turn = Matrix.Rotation(-math.radians(90.0), 4, 'X') @ world.to_quaternion().to_matrix().to_4x4()
    for r in range(4):                      # exact 0 / +-1 where it is one (no float noise on the usual cases)
        for c in range(4):
            v = turn[r][c]
            if abs(v - round(v)) < 1e-6:
                turn[r][c] = float(round(v))
    return turn


def skin_bone_positions(arm):
    """{deforming bone: position} in game space (rig bones don't deform)."""
    g = game_frame(arm)
    return {b.name: tuple((g @ b.matrix_local).translation) for b in arm.data.bones if b.use_deform}


def cars3_pose(arm):
    """{bone: row-vector flat 16 matrix, game space} for every bone of the
    Cars 3 import - skin bones, controls, FX markers - as placed on import.
    v2.0.16: an import without the control rig has no control bones: those
    (and markers not imported) come from the Cars 3 file itself, so the export
    is the same either way; the armature's own bones win. The FX_Markers
    group bone of a rig-less import is not a marker."""
    mats = {}
    if not any(b.name.endswith('_Ctrl') for b in arm.data.bones) and arm.get(cars3.PROP_SOURCE):
        try:
            mats.update(cars3.full_rig_pose(arm[cars3.PROP_SOURCE]))
        except Exception as e:
            print('Cars 3 export: rig not read from %s (%s)' % (arm[cars3.PROP_SOURCE], e))
    for b in arm.data.bones:
        if b.name == cars3.MARKER_GROUP:
            continue
        if b.name.startswith('FX_Marker') and not b.name.endswith('_Ctrl') and b.name + '_Ctrl' in mats:
            mats.pop(b.name + '_Ctrl')          # a rig-less import's marker (FX_Marker_X) replaces the rig's
        mats[b.name] = game_frame(arm) @ b.matrix_local
    out = {}
    for n, m in mats.items():
        rv = m.transposed()
        out[n] = [rv[r][c] for r in range(4) for c in range(4)]
    return out


def node_of_bone():
    """Bone name -> rig node name where the Cars 2 names differ (FX_Marker_X_Ctrl -> FX_Marker_X)."""
    return {bone: raw for raw, bone in cars3.C2_NODE_TO_BONE.items() if bone != raw}


def cars3_skeleton(arm, offset):
    """{deforming bone: (row-vector flat 16 matrix in Cars 2 space, parent)} -
    the Cars 3 skeleton as imported (game space), moved by the mesh offset."""
    out = {}
    for b in arm.data.bones:
        if not b.use_deform:
            continue
        m = game_frame(arm) @ b.matrix_local
        for k in range(3):
            m[k][3] += offset[k]
        rv = m.transposed()
        out[b.name] = ([rv[r][c] for r in range(4) for c in range(4)],
                       b.parent.name if b.parent else None)
    return out


def collect_parts(arm, lod, scene_names, problems, warnings, material_of=None, world_to=None):
    """The Cars 3 meshes under `arm` as parts for c3toc2.build_geometry (positions in
    game space: armature space turned by game_frame - the importer's own
    armatures are already game space).

    lod: objects of this LOD (the 'c3_lod' the importer set), plus any object
    without one (meshes you added). A part that has no object at this LOD
    (Fritter's Orange_Glass / TeamYellow_underbelly exist only at LOD 2) is
    taken from the nearest LOD it has - a mesh you kept is exported. None =
    every object."""
    parts = []
    # world_to (v2.0.22): the custom-model export passes world -> game space with the armature's scale too
    to_arm = game_frame(arm) @ arm.matrix_world.inverted() if world_to is None else world_to
    objs = [o for o in geometry.character_meshes(arm) if o.name in scene_names]
    # a mesh that belongs to the character by name but isn't part of the
    # armature (moved out, duplicated, joined) would be skipped silently - say so
    mine = {o.name for o in objs}
    stems = {re.sub(r'_LOD\d+$', '', re.sub(r'\.\d{3}$', '', o.name)).lower() for o in objs}
    prefix = arm.name.lower().split('.')[0]
    for o in bpy.data.objects:
        if o.type != 'MESH' or o.name in mine or o.name not in scene_names:
            continue
        stem = re.sub(r'_LOD\d+$', '', re.sub(r'\.\d{3}$', '', o.name)).lower()
        if stem in stems or (prefix and prefix in o.name.lower()) or \
                any(stem.startswith(s_.rsplit('_', 1)[0]) for s_ in stems if '_' in s_):
            warnings.append('%s: not part of %s (not parented to it / no Armature modifier) - NOT exported'
                            % (o.name, arm.name))
    if lod is not None:
        def part_key(o):
            return re.sub(r'_LOD\d+$', '', re.sub(r'\.\d{3}$', '', o.name))
        lods = {}
        for o in objs:
            if o.get('c3_lod') is not None:
                lods.setdefault(part_key(o), set()).add(int(o['c3_lod']))
        chosen = {k: (lod if lod in v else min(v, key=lambda x: (abs(x - lod), x))) for k, v in lods.items()}
        for k, l in sorted(chosen.items()):
            if l != lod and 'shadow' not in k.lower():
                warnings.append('%s: only at LOD %d - exported from that' % (k, l))
        objs = [o for o in objs if o.get('c3_lod') is None or int(o['c3_lod']) == chosen[part_key(o)]]
    for obj in objs:
        # modifiers (Smooth by Angle after Shade Flat/Auto Smooth ...) applied, as the viewport shows it
        me, free = geometry.mesh_with_modifiers(obj, warnings)
        try:
            parts += _object_parts(obj, me, to_arm, problems, material_of)
        finally:
            free()
    return parts


def _object_parts(obj, me, to_arm, problems, material_of):
    """collect_parts for one object, from its mesh me (one part per material)."""
    parts = []
    uv = me.uv_layers.active
    if uv is None:
        problems.append('%s: has no UV map' % obj.name)
        return parts
    M = to_arm @ obj.matrix_world
    N3 = M.to_3x3().inverted().transposed()
    me.calc_loop_triangles()
    normals = geometry._corner_normals(me)
    groups = {g.index: g.name for g in obj.vertex_groups}
    by_mat = {}
    for tri in me.loop_triangles:
        by_mat.setdefault(tri.material_index, []).append(tri)
    for mi, tris in sorted(by_mat.items()):
        mat = obj.material_slots[mi].material if mi < len(obj.material_slots) else None
        mat_name = mat.name if mat is not None else (material_of(obj) if material_of else None)
        if mat_name is None:
            problems.append('%s: some faces have no material' % obj.name)
            continue
        verts, index_of, indices = [], {}, []
        for tri in tris:
            for li in tri.loops:
                vi = me.loops[li].vertex_index
                n = (N3 @ normals[li]).normalized()
                u = tuple(uv.data[li].uv)
                key = (vi, round(u[0], 5), round(u[1], 5), round(n.x, 4), round(n.y, 4), round(n.z, 4))
                k = index_of.get(key)
                if k is None:
                    p = M @ me.vertices[vi].co
                    bones = [(groups[g.group], g.weight) for g in me.vertices[vi].groups
                             if g.group in groups and g.weight > 0]
                    verts.append({'Position': tuple(p), 'Normal': tuple(n),
                                  'Uv1': (u[0], 1.0 - u[1]),        # back to the game's convention
                                  '_bones': bones})
                    k = index_of[key] = len(verts) - 1
                indices.append(k)
        parts.append({'material': mat_name, 'name': obj.name, 'verts': verts, 'indices': indices})
    return parts


class CARS2TK_OT_export_cars3_as_cars2(bpy.types.Operator, ImportHelper):
    """Pick the Cars 2 character (.oct or .oct.json) to put the Cars 3 meshes on - its rig, skeleton, materials and textures are kept"""
    bl_idname = 'cars2tk.export_cars3_as_cars2'
    bl_label = 'Export Cars 3 as Cars 2'
    bl_options = {'REGISTER'}
    # The file you pick in the browser is the Cars 2 character to build on.
    filename_ext = ''
    filter_glob: StringProperty(default='*.oct;*.json', options={'HIDDEN'})
    filepath: StringProperty(subtype='FILE_PATH')
    retarget: BoolProperty(name='Move skeleton to Cars 3', default=True,
                           description="Move the Cars 2 character's bones, controls and FX markers to where the "
                                       'Cars 3 model has them (off: fit the Cars 3 meshes onto the Cars 2 skeleton '
                                       'as it is)')
    output_dir: StringProperty(name='Write to', subtype='DIR_PATH', default='',
                               description="Folder for the result: <car>.oct + <car>_0.vbuf/.ibuf. Empty: an "
                                           "'export' folder next to the Cars 2 character you picked")
    # (older scripts passed the base here; still accepted)
    cars2_oct: StringProperty(name='Cars 2 character', subtype='FILE_PATH', options={'HIDDEN'})
    # v2.0.56 (user): no LOD / Keep the Cars 2 eyes options - always the full
    # detail meshes (LOD 0; a part only in a lower LOD comes from that one) and
    # the model's own eyes
    lod = 0
    keep_cars2_eyes = False

    def invoke(self, context, event):
        arm = cars3_armature(context)
        if arm is None:
            self.report({'ERROR'}, 'Import a Cars 3 character first (File > Import > Cars 3 Character)')
            return {'CANCELLED'}
        last = arm.get(PROP_TEMPLATE)
        if last and os.path.isfile(last):
            self.filepath = last                     # the Cars 2 character used last time
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        arm = cars3_armature(context)
        if arm is None:
            self.report({'ERROR'}, 'No imported Cars 3 character in the scene')
            return {'CANCELLED'}
        if arm.mode == 'EDIT':
            arm.update_from_editmode()            # bones (and their marks) as edited
        picked = self.cars2_oct or self.filepath
        oct_path = bpy.path.abspath(picked) if picked else ''
        if not oct_path or not os.path.isfile(oct_path) or \
                not oct_path.lower().endswith(('.oct', '.json')):
            self.report({'ERROR'}, "Select the Cars 2 character's .oct or .oct.json (e.g. mcqueen.oct.json) "
                                   'in the file browser - that is the character the Cars 3 meshes go onto')
            return {'CANCELLED'}
        from . import character
        out = bpy.path.abspath(self.output_dir) if self.output_dir else \
            os.path.join(os.path.dirname(oct_path), character.EXPORT_FOLDER)
        bent_path = c2rigedit.find_bent(oct_path)          # used if it is next to / above the character
        problems, warnings, notes = [], [], []
        try:
            tree = c3toc2.load_base(oct_path)
            parts = collect_parts(arm, self.lod, set(context.scene.objects.keys()), problems, warnings)
            if problems:
                self.report({'ERROR'}, 'Meshes not exportable: %s' % '; '.join(problems[:3]))
                return {'CANCELLED'}
            if not parts:
                self.report({'ERROR'}, 'No meshes of LOD %d under the armature' % self.lod)
                return {'CANCELLED'}
            if self.retarget:
                offset, worst, who = (0.0, 0.0, 0.0), 0.0, ''
                notes.append('skeleton, controls and FX markers moved to the Cars 3 places; meshes as they are')
            else:
                offset, worst, who = c3toc2.alignment_offset(tree, skin_bone_positions(arm))
                notes.append('meshes moved by (%.4f, %.4f, %.4f) so the wheel hubs match the Cars 2 ones' % offset)
            if worst > 0.05:
                warnings.append('the skeletons differ by up to %.3f (%s): this does not look like the same car'
                                % (worst, who))
            keep, vb2, ib2 = [], None, None
            if self.keep_cars2_eyes:
                keep = [m for m in c3toc2.template_info(tree)['materials'] if 'eye' in str(m).lower()]
                vb2, ib2 = c3toc2.base_buffers(oct_path, tree)
            marked = new_bones_of(arm)
            bad = ['%s: %s' % (n, why) for n in marked for why in [check_new_bone(arm, n)] if why]
            if bad:
                self.report({'ERROR'}, 'Not exported - bones added to the rig: %s' % '; '.join(bad[:3]))
                return {'CANCELLED'}
            chains = chains_of(arm)
            bad = ['chain %s: %s' % (c['name'], why) for c in chains for why in [check_chain(arm, c)] if why]
            if bad:
                self.report({'ERROR'}, 'Not exported - %s' % '; '.join(bad[:3]))
                return {'CANCELLED'}
            try:                                   # the Cars 3 model's clip ports (animated parts)
                data3 = cars3.read_cars3(arm[cars3.PROP_SOURCE])[0]
                c3_ports = c3toc2.cars3_ports(data3)
            except Exception as e:
                c3_ports = {}
                notes.append('Cars 3 ports not read (%s) - no animated parts' % e)
            bent_obj = None
            if bent_path:
                import c2channels
                try:
                    bent_obj = c2channels.Bent.load(bent_path)
                except Exception as e:
                    notes.append('.bent not read (%s)' % e)
            # the axle-at-zero shift is for in-race packages (the game places the
            # car from its wheels - without it a taller model floats); the
            # frontend places characters another way and sank a shifted model,
            # so a base inside a 'frontend' folder keeps its own zero
            frontend = any(p_.lower() == 'frontend' for p_ in os.path.normpath(oct_path).split(os.sep))
            geom, n2, problems, w2 = c3toc2.build_geometry(
                tree, parts, offset, keep, vb2, ib2, cars3_skeleton(arm, offset), marked, chains,
                cars3_pose(arm) if self.retarget else None, node_of_bone(), c3_ports, bent_obj, True)
            if geom is not None:
                geom['axle_zero'] = not frontend
                notes.append('frontend package: the rig keeps the base\'s zero (no axle shift - the frontend '
                             'places characters by it)' if frontend else
                             'in-race package: the rig\'s zero goes to the front axle (as the game places cars)')
            notes += n2
            warnings += w2
            if problems:
                self.report({'ERROR'}, 'Not exported: %s' % '; '.join(problems[:3]))
                return {'CANCELLED'}
            written, log, errs, w3 = c3toc2.export_onto(oct_path, geom, out, bent_path, None,
                                                        character.offsetting_path())
            warnings += w3
        except Exception as e:
            self.report({'ERROR'}, 'Export failed: %s' % e)
            return {'CANCELLED'}
        if errs:
            self.report({'ERROR'}, 'Not exported, %d error(s): %s' % (len(errs), errs[0]))
            return {'CANCELLED'}
        arm[PROP_TEMPLATE] = oct_path
        # v2.0.56: which Cars 2 channels this character's extra Cars 3 parts got
        # (Fritter's blade) - Convert Cars 3 Clips / Import Cars 3 Clip read it
        # from here, so the clips animate those parts with no file to pick
        from . import anim
        anim.store_part_ports(arm, geom.get('part_ports'))
        from . import VERSION
        text = bpy.data.texts.get('Cars3ToCars2Report') or bpy.data.texts.new('Cars3ToCars2Report')
        text.clear()
        text.write('\n'.join(['CARS 3 -> CARS 2 EXPORT (Cars 2 Toolkit %s)' % VERSION, '',
                              'built on ' + oct_path, '']
                             + ['  wrote ' + w for w in written]
                             + ['', 'MESHES'] + ['  ' + x for x in notes]
                             + ['', 'WARNINGS'] + ['  ' + x for x in warnings]
                             + ['', 'STEPS'] + ['  ' + x for x in log]) + '\n')
        for w in warnings[:5]:
            self.report({'WARNING'}, w)
        if c3toc2.is_json(oct_path):
            self.report({'INFO'}, 'Wrote %s with its buffers and textures folder to %s - pack it with '
                                  'offsetting. Details in the Cars3ToCars2Report text.'
                        % (os.path.basename(oct_path), out))
        else:
            self.report({'INFO'}, 'Wrote %d files to %s - copy all of them over the Cars 2 originals. '
                                  'Details in the Cars3ToCars2Report text.' % (len(written), out))
        return {'FINISHED'}


class CARS2TK_OT_c3_add_bones(bpy.types.Operator):
    """Add the selected bones to the Cars 2 rig on export (rig node, game bone, and wheel spin for a ..._SpinJoint) - even before any vertices are weighted to them"""
    bl_idname = 'cars2tk.c3_add_bones'
    bl_label = 'Add Selected Bones to Rig'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from .rig import selected_bone_names
        arm = _model(context)
        if arm is None:
            self.report({'ERROR'}, 'No Cars 3 import or rig template selected')
            return {'CANCELLED'}
        names = selected_bone_names(context, arm)
        if not names:
            self.report({'ERROR'}, 'Select the bones to add (Edit Mode or Pose Mode)')
            return {'CANCELLED'}
        added, refused = [], []
        for n in names:
            why = check_new_bone(arm, n)
            if why:
                refused.append('%s: %s' % (n, why))
            else:
                set_new_bone(arm, n, True)
                added.append(n)
        for r in refused:
            self.report({'WARNING'}, r)
        if not added:
            self.report({'ERROR'}, 'Nothing added - ' + refused[0])
            return {'CANCELLED'}
        self.report({'INFO'}, 'Will be added to the Cars 2 rig on export: %s' % ', '.join(added))
        return {'FINISHED'}


class CARS2TK_OT_c3_remove_bone(bpy.types.Operator):
    """Don't add this bone to the Cars 2 rig (bones with weights the base lacks are still added)"""
    bl_idname = 'cars2tk.c3_remove_bone'
    bl_label = 'Remove'
    bl_options = {'REGISTER', 'UNDO'}
    name: StringProperty()

    def execute(self, context):
        arm = _model(context)
        if arm is not None:
            set_new_bone(arm, self.name, False)
        return {'FINISHED'}


class CARS2TK_OT_c3_connect_controls(bpy.types.Operator):
    """Make the skin bones follow their controls (Jaw <- Jaw_Ctrl, Body <- Body_Final_Proxy, wheel hubs <- wheel controls), so animation clips move the model in Blender. New imports do this already; use it on a Cars 3 model imported with an older version"""
    bl_idname = 'cars2tk.c3_connect_controls'
    bl_label = 'Connect Controls to Skin'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        arm = cars3_armature(context)
        if arm is None:
            self.report({'ERROR'}, 'No imported Cars 3 character in the scene')
            return {'CANCELLED'}
        n = cars3.connect_controls(context, arm)
        total = len(cars3.follow_pairs(arm))
        self.report({'INFO'}, 'Connected %d skin bones to their controls (%d in all)' % (n, total) if n
                    else 'Already connected (%d skin bones follow their controls)' % total)
        return {'FINISHED'}


class CARS2TK_OT_c3_make_chain(bpy.types.Operator):
    """Make the selected bones swing in the game as the car moves"""
    bl_idname = 'cars2tk.c3_make_chain'
    bl_label = 'Make Dynamic Chain'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from .rig import selected_bone_names
        arm = _model(context)
        if arm is None:
            self.report({'ERROR'}, 'No Cars 3 import or rig template selected')
            return {'CANCELLED'}
        names = selected_bone_names(context, arm)
        if len(names) < 2:
            self.report({'ERROR'}, 'Select the bones of the chain (at least two)')
            return {'CANCELLED'}
        bones = _bones_now(arm)
        if all(re.search(r'\d+$', n) for n in names):
            order = c3toc2.order_chain(names)
        else:                                  # along the parent line, root first
            sel = set(names)
            order = sorted(names, key=lambda n: sum(1 for p in bones[n].parent_recursive if p.name in sel))
        old = chains_of(arm)
        chains = [c for c in old if not set(c['bones']) & set(order)]
        replaced = [c for c in old if set(c['bones']) & set(order)]
        prefix = re.sub(r'_?\d+$', '', order[0]) or order[0]
        prefix = re.sub(r'_?Proxy$', '', prefix) or prefix
        if replaced and replaced[0].get('source') == 'Cars 3':     # keep the model's own settings
            keys = ('global_damping', 'iteration_rate', 'frequency', 'damping', 'drag', 'inverse_mass')
            chain = dict({k: replaced[0][k] for k in keys if k in replaced[0]}, name=replaced[0]['name'],
                         bones=order, source='Cars 3 settings')
        else:
            chain = dict(c3toc2.MATER_CHAIN, name=prefix, bones=order, source='Mater settings')
        why = check_chain(arm, chain)
        if why:
            self.report({'ERROR'}, 'Not a chain - %s' % why)
            return {'CANCELLED'}
        set_chains(arm, chains + [chain])
        self.report({'INFO'}, 'Dynamic chain %s: %s' % (prefix, ' > '.join(order)))
        return {'FINISHED'}


class CARS2TK_OT_c3_remove_chain(bpy.types.Operator):
    """Don't make this a dynamic chain (its bones are still exported, riding along without swinging)"""
    bl_idname = 'cars2tk.c3_remove_chain'
    bl_label = 'Remove Chain'
    bl_options = {'REGISTER', 'UNDO'}
    name: StringProperty()

    def execute(self, context):
        arm = _model(context)
        if arm is not None:
            set_chains(arm, [c for c in chains_of(arm) if c['name'] != self.name])
        return {'FINISHED'}


class CARS2TK_PT_cars3(bpy.types.Panel):
    bl_label = 'Cars 3 to Cars 2'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Cars 2 Character'

    @classmethod
    def poll(cls, context):
        return cars3_armature(context) is not None

    def draw(self, context):
        arm = cars3_armature(context)
        col = self.layout.column(align=True)
        col.label(text='Puts the Cars 3 meshes on a Cars 2 character.')
        col.label(text='Delete the meshes you do not want first.')
        col.operator(CARS2TK_OT_c3_add_bones.bl_idname, icon='BONE_DATA')
        marked = new_bones_of(arm) if arm else []
        if marked:
            box = self.layout.box()
            box.label(text='New bones (%d)' % len(marked))
            for n in marked:
                row = box.row(align=True)
                why = check_new_bone(arm, n)
                row.label(text=n, icon='ERROR' if why else 'BONE_DATA')
                row.operator(CARS2TK_OT_c3_remove_bone.bl_idname, text='', icon='X').name = n
        col = self.layout.column(align=True)
        col.operator(CARS2TK_OT_c3_make_chain.bl_idname, icon='LINKED')
        col.operator(CARS2TK_OT_c3_connect_controls.bl_idname, icon='CONSTRAINT_BONE')
        chains = chains_of(arm) if arm else []
        if chains:
            box = self.layout.box()
            box.label(text='Dynamic chains (%d)' % len(chains))
            for c in chains:
                row = box.row(align=True)
                why = check_chain(arm, c)
                row.label(text='%s: %d links (%s)' % (c['name'], len(c['bones']), c.get('source', '')),
                          icon='ERROR' if why else 'LINKED')
                row.operator(CARS2TK_OT_c3_remove_chain.bl_idname, text='', icon='X').name = c['name']
        self.layout.operator('cars2tk.export_cars2', text='Export Cars 2 Character', icon='EXPORT')


classes = (CARS2TK_OT_c3_add_bones, CARS2TK_OT_c3_remove_bone, CARS2TK_OT_c3_connect_controls,
           CARS2TK_OT_c3_make_chain,
           CARS2TK_OT_c3_remove_chain, CARS2TK_OT_export_cars3_as_cars2, CARS2TK_PT_cars3)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
