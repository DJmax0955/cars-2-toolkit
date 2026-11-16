"""Export Cars 2 Character - one export for every kind of character (public 2.0.10).

USER: three buttons wrote the same thing (a Cars 2 character in cars2_export) and only one of them had the
pack / texture folder / JSON options. One operator now, in File > Export and in the panels; it looks at the
selection and runs the export that fits:

    an imported Cars 2 character   rig.CARS2TK_OT_export_character    you pick the output folder (cars2_export)
    a custom model on a skeleton   template.CARS2TK_OT_export_template you pick the Cars 2 character to build on
    an imported Cars 3 character   cars3_export.CARS2TK_OT_export_cars3_as_cars2   (the same)

and then, for all of them, the extras: the texture folder / JSON (offsetting.exe oct decode) and the game .zip
(offsetting.exe zip-c2) - pack.py. The three operators stay registered (scripts call them) without their panel
buttons.
"""
import os

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from . import character as ch


# ------------------------------------------------------------------------------------------------ offsetting
def find_offsetting(context=None, hint_path=None):
    """offsetting.exe, or None: the toolkit's preference, the Cars 2 World Importer's, else searched in the game's
    assets folder (the scene's Game Assets folder, or the one above hint_path / the active character)."""
    from . import pack
    context = context or bpy.context
    prefs_path = ''
    try:
        prefs_path = context.preferences.addons[__package__].preferences.offsetting_path
    except (KeyError, AttributeError):
        pass
    found = pack.find_offsetting(prefs_path) if prefs_path else None
    if found:
        return found
    try:
        for a in context.preferences.addons.values():
            p = getattr(getattr(a, 'preferences', None), 'offsetting', None)
            if p and os.path.isfile(bpy.path.abspath(p)):
                return bpy.path.abspath(p)
    except Exception:
        pass
    dirs = []
    c2 = getattr(getattr(context, 'scene', None), 'c2', None)
    if c2 is not None and getattr(c2, 'assets_dir', ''):
        dirs.append(c2.assets_dir)
    for p in (hint_path, ):
        if p:
            dirs.append(pack.assets_folder_of(p))
    arm = ch.active_character(context) if getattr(context, 'scene', None) is not None else None
    if arm is not None and arm.get(ch.PROP_SCENE):
        dirs.append(pack.assets_folder_of(arm[ch.PROP_SCENE]))
    return pack.find_offsetting('', dirs)


# ------------------------------------------------------------------------------------------------ extras
def after_export(op, context, out_dir, oct_name, source_oct, pack_zip, keep_json, text=None):
    """The extras of every character export. oct_name: the written <car>.oct in out_dir; source_oct: the Cars 2
    character's own .oct (or .oct.json) the export came from / was built on - its unpacked package is what gets
    packed. -> (extra paths, packed zip or None)."""
    from . import pack
    extras, packed = [], None
    if not (pack_zip or keep_json):
        return extras, packed
    exe = find_offsetting(context, source_oct)
    if exe is None:
        op.report({'ERROR'}, 'Exported, but offsetting.exe was not found for the %s (set its path in '
                             'Preferences > Add-ons > Cars 2 Toolkit)' % _what(pack_zip, keep_json))
        return extras, packed
    log = []
    if keep_json:
        try:
            extras = pack.decode_oct(os.path.join(out_dir, oct_name), exe, True, True, log)
        except (RuntimeError, OSError) as e:
            op.report({'WARNING'}, 'Exported without the JSON + textures: %s' % e)
    if pack_zip:
        src = source_oct[:-5] if source_oct.lower().endswith('.oct.json') else source_oct
        try:
            packed = pack.pack_export(src, out_dir, exe, log)
        except (RuntimeError, OSError) as e:
            op.report({'ERROR'}, 'Exported, but not packed: %s' % e)
    if text is not None and log:
        text.write('\nEXTRAS\n' + '\n'.join('  ' + x for x in log) + '\n')
    return extras, packed


def _what(pack_zip, keep_json):
    parts = (['game .zip'] if pack_zip else []) + (['JSON + textures'] if keep_json else [])
    return ' / '.join(parts)


# ------------------------------------------------------------------------------------------------ the operator
def export_kind(context):
    """('cars2' | 'custom' | 'cars3', armature) for the selection, or (None, why)."""
    from . import cars3, template
    from .cars3_export import cars3_armature
    obj = context.active_object
    cands = []
    if obj is not None:
        cands.append(obj)
        if obj.parent is not None:
            cands.append(obj.parent)
        cands += [m.object for m in obj.modifiers if m.type == 'ARMATURE' and m.object is not None]
    for o in cands:
        if o is None or o.type != 'ARMATURE':
            continue
        if cars3.PROP_SOURCE in o.keys():
            return 'cars3', o
        if template.is_template(o):
            return 'custom', o
        if ch.PROP_SCENE in o.keys():
            return 'cars2', o
        if cars3.PROP_PROP_SOURCE not in o.keys():
            return 'custom', o
    arm = ch.active_character(context)
    if arm is not None:
        return 'cars2', arm
    arm = cars3_armature(context)
    if arm is not None:
        return 'cars3', arm
    arm = template.template_armature(context)
    if arm is not None:
        return 'custom', arm
    return None, ('Select the character to export: an imported Cars 2 or Cars 3 character, or your model '
                  '(Custom Model panel)')


KIND_TEXT = {'cars2': 'Cars 2 character - pick the output folder',
             'custom': 'custom model - pick the Cars 2 character to build on',
             'cars3': 'Cars 3 character - pick the Cars 2 character to build on'}


class CARS2TK_OT_export_cars2(bpy.types.Operator, ImportHelper):
    """Export the selected character as a Cars 2 character (.oct, .vbuf/.ibuf): an imported Cars 2 character with
    your edits, your own model on a Cars 2 skeleton, or an imported Cars 3 character - optionally packed as the
    game's .zip"""
    bl_idname = 'cars2tk.export_cars2'
    bl_label = 'Export Cars 2 Character'
    bl_options = {'REGISTER'}
    filename_ext = ''
    filter_glob: StringProperty(default='*.oct;*.json', options={'HIDDEN'})
    filepath: StringProperty(subtype='FILE_PATH')
    # Cars 2 character only
    include_geometry: BoolProperty(name='Include meshes', default=True,
                                   description='Rebuild the geometry from the meshes in the scene (needed for new or '
                                               'changed meshes and new skin bones)')
    # Cars 3 character only
    retarget: BoolProperty(name='Move skeleton to Cars 3', default=True,
                           description="Move the Cars 2 character's bones, controls and FX markers to where the "
                                       'Cars 3 model has them (off: fit the Cars 3 meshes onto the Cars 2 skeleton '
                                       'as it is)')
    # every kind
    pack_zip: BoolProperty(name='Pack as game .zip', default=False,
                           description='Also pack the whole character package (the game archive of the Cars 2 '
                                       'character, with the exported files in place of the originals) into a .zip the '
                                       'game loads, with offsetting.exe zip-c2')
    keep_json: BoolProperty(name='Keep JSON + Textures', default=False,
                            description='Also write <car>.oct.json and its <car>.oct.textures folder next to the '
                                        'export (offsetting.exe oct decode -t), as in the unpacked game folders')

    def invoke(self, context, event):
        kind, arm = export_kind(context)
        if kind is None:
            self.report({'ERROR'}, arm)
            return {'CANCELLED'}
        if kind == 'cars2':
            folder = os.path.join(os.path.dirname(arm[ch.PROP_SCENE]), ch.EXPORT_FOLDER)
            try:
                os.makedirs(folder, exist_ok=True)     # the file browser can't open a folder that isn't there
            except OSError:
                pass
            self.filepath = folder + os.sep
        else:
            from . import template
            from .cars3_export import PROP_TEMPLATE
            last = arm.get(PROP_TEMPLATE if kind == 'cars3' else template.PROP_BASE)
            if last and os.path.isfile(last):
                self.filepath = last
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        kind, arm = export_kind(context)
        if kind is None:
            layout.label(text=arm, icon='ERROR')
            return
        box = layout.box()
        box.label(text=arm.name, icon='ARMATURE_DATA')
        box.label(text=KIND_TEXT[kind])
        if kind == 'cars2':
            layout.prop(self, 'include_geometry')
        elif kind == 'cars3':
            layout.prop(self, 'retarget')
        layout.prop(self, 'pack_zip')
        layout.prop(self, 'keep_json')
        if (self.pack_zip or self.keep_json) and find_offsetting(context) is None:
            col = layout.column()
            col.alert = True
            col.label(text='offsetting.exe not found:', icon='ERROR')
            col.label(text='set it in the add-on preferences')

    def execute(self, context):
        kind, arm = export_kind(context)
        if kind is None:
            self.report({'ERROR'}, arm)
            return {'CANCELLED'}
        if kind == 'cars2':
            # the Cars 2 export runs the extras itself (its report text lists them)
            r = bpy.ops.cars2tk.export_character('EXEC_DEFAULT', filepath=self.filepath,
                                                 include_geometry=self.include_geometry, pack_zip=self.pack_zip,
                                                 keep_json=self.keep_json)
            return r
        base = bpy.path.abspath(self.filepath) if self.filepath else ''
        if kind == 'custom':
            r = bpy.ops.cars2tk.export_template('EXEC_DEFAULT', filepath=base)
            report = 'CustomRigReport'
        else:
            r = bpy.ops.cars2tk.export_cars3_as_cars2('EXEC_DEFAULT', filepath=base, retarget=self.retarget)
            report = 'Cars3ToCars2Report'
        if 'FINISHED' not in r:
            return r
        out = os.path.join(os.path.dirname(base), ch.EXPORT_FOLDER)
        name = os.path.basename(base)
        if name.lower().endswith('.json'):
            name = name[:-5]
        text = bpy.data.texts.get(report)
        extras, packed = after_export(self, context, out, name, base, self.pack_zip, self.keep_json, text)
        if extras or packed:
            self.report({'INFO'}, 'Exported to %s%s%s' % (
                out, (' with %s' % ', '.join(os.path.basename(x) for x in extras)) if extras else '',
                (', packed: %s' % os.path.basename(packed)) if packed else ''))
        return {'FINISHED'}


def menu_export(self, context):
    self.layout.operator(CARS2TK_OT_export_cars2.bl_idname, text='Cars 2 Character (.oct)')


def register():
    bpy.utils.register_class(CARS2TK_OT_export_cars2)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    bpy.utils.unregister_class(CARS2TK_OT_export_cars2)
