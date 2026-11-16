"""v2.0.59: a Cars 2 character's eyes in Eevee and Cycles, not only in the Cars 2 Game render engine.

The game draws a car's eyes into a texture each frame (renderedEyeTexture: irises, sclera, outline, lids) and the
car_eyes_* / car_double_eyes_* effect samples it. The Cars 2 Game render engine does that on the GPU (look/gameeyes);
the node materials the material system builds for Material Preview / Eevee / Cycles (look/matnodes, and the game
shaders as nodes, look/fxnodes) have nothing to draw it with, so the eyes showed only their lid texture there.

graft() adds the toolkit's node-built eye (c2_model.build_eye_nodes: the same eye graph the game draws, driven by
the rig's eyelid sliders and Eyes control like the import's own eye material) into that rebuilt material:
  - simplified material (matnodes): the eye with its lids replaces the lid texture x tint (C2_Tint), so lighting,
    the Lit/Unlit switch and alpha stay the material system's;
  - game shaders as nodes (Scene > Eevee Game Shaders): it feeds the renderedEyeTexture reads, rgb + alpha exactly
    as the game's eye texture (lids = their texture or black, alpha 0).
Every rebuild clears the tree; charmat's depsgraph handler grafts again (eyelook.ensure). Preview only: the material's
game values, the export and the Cars 2 Game render engine are unchanged.
"""
import json
import re

import bpy

GRAFT = 'C2_EyeGraft'             # name of the eye group node the graft adds
EYE_EFFECTS = ('car_eyes', 'car_double_eyes')
PROP_EYE_PLAN = 'cars2_eye_plan'  # charmat.PROP_EYE_PLAN (on the armature)
_X0, _Y0 = -2600, 1800            # where the grafted nodes go (left of / above the material system's)


def _base(mat):
    return getattr(mat.c2, 'variant_of', None) or mat


def is_eye(mat):
    c2 = getattr(mat, 'c2', None)
    return c2 is not None and (_base(mat).c2.effect or '').lower().startswith(EYE_EFFECTS)


def has_graft(mat):
    return mat.node_tree is not None and mat.node_tree.nodes.get(GRAFT) is not None


def _props(mat):
    """{parameter: value or [values]} of the material's game values."""
    out = {}
    for p in _base(mat).c2.props:
        if p.ptype == 'Float':
            vals = [float(p.fvalue[i]) for i in range(max(1, min(p.size, len(p.fvalue))))]
            out[p.name] = vals if len(vals) > 1 else vals[0]
        else:
            out[p.name] = float(p.ivalue)
    return out


def _image(mat, usage, images, stored):
    """The bpy image in the material's texture slot `usage`: from the material system's images
    (TexturePool index -> image), else the one the import loaded (mat[stored])."""
    base = _base(mat)
    idx = next((t.texture for t in base.c2.textures if t.usage == usage), None)
    if idx is None:
        return None
    img = images.get(idx) if images else None
    if img is None:
        img = bpy.data.images.get(base.get(stored) or mat.get(stored) or '')
    return img


def _plan(arm):
    try:
        return json.loads(arm.get(PROP_EYE_PLAN)) if arm is not None and arm.get(PROP_EYE_PLAN) else None
    except (TypeError, ValueError):
        return None


def _driven_params(plan):
    """Eye parameters the rig sets (drivers), which the material's stored values must not overwrite."""
    if not plan:
        return set()
    out = {param for _label, param, _rest in plan.get('eye_links', [])}
    rbf = plan.get('eye_rbf') or {}
    out.update(name for name, _weights in (rbf.get('outputs') or []))     # [(parameter, weights), ...]
    return out


def _drop_dead_drivers(mat):
    """Drivers of nodes a rebuild removed (the import's own eye material) - Blender reports them as invalid."""
    ad = mat.node_tree.animation_data
    if ad is None:
        return
    names = {n.name for n in mat.node_tree.nodes}
    for fc in list(ad.drivers):
        m = re.match(r'nodes\["(.+?)"\]', fc.data_path)
        if m and m.group(1) not in names:
            ad.drivers.remove(fc)


def _renderedeye_samples(L, scene, mat):
    """Indices i of the effect's samples that read renderedEyeTexture (the game-shader node groups take them as
    inputs T<i> / T<i>.a), or [] when the material has no game-shader nodes."""
    nt = mat.node_tree
    if not any(n.name.startswith(L.fxnodes.GAME_TAG) for n in nt.nodes):
        return []
    try:
        game = L.fxnodes.material_context(scene, mat)
        plan = L.fxnodes.plan_for(game.assets, _base(mat).c2.effect, game.local_effects) if game else None
    except Exception:
        plan = None
    if plan is None:
        return []
    return [i for i, s in enumerate(plan.samples) if plan.sampler_usage.get(s[0]) == 'renderedEyeTexture']


def graft(L, scene, mat, images=None):
    """Add the node-built eye to a rebuilt eye material (see the module doc). -> True when added."""
    from . import c2_model
    if not is_eye(mat) or mat.node_tree is None or has_graft(mat):
        return False
    nt = mat.node_tree
    tint = nt.nodes.get('C2_Tint')
    samples = _renderedeye_samples(L, scene, mat) if L is not None else []
    if tint is None and not samples:
        return False                      # a tree this doesn't know: leave it
    if images is None and L is not None:
        try:
            game = L.fxnodes.material_context(scene, mat)
            images = game.textures.images if game is not None else L.fxnodes.scene_images(scene)
        except Exception:
            images = {}
    base = _base(mat)
    arm = base.c2.owner
    plan = _plan(arm)
    double = base.c2.effect.lower().startswith('car_double_eyes')
    built = c2_model.build_eye_nodes(nt, _props(mat), double,
                                     _image(mat, 'eyeBallTexture', images, 'cars2_eye_iris'),
                                     _image(mat, 'diffuseMapA1', images, 'cars2_eye_lid'),
                                     x0=_X0, y0=_Y0, bsdf=False)
    built['group'].name = built['group'].label = GRAFT
    _drop_dead_drivers(mat)
    c2_model.drive_eye_node(nt, built['group'], arm, plan, x0=_X0, y0=_Y0)
    if tint is not None:
        # simplified material: lid texture x lid colour -> the whole eye (irises, outline, lids)
        for link in [l for l in nt.links if l.from_node == tint]:
            nt.links.new(built['color'], link.to_socket)
    for i in samples:
        # game shaders as nodes: renderedEyeTexture as the game draws it
        for n in nt.nodes:
            if n.type == 'GROUP' and n.name.startswith(L.fxnodes.GAME_TAG) and ('T%d' % i) in n.inputs:
                nt.links.new(built['rgb'], n.inputs['T%d' % i])
                if ('T%d.a' % i) in n.inputs:
                    nt.links.new(built['alpha'], n.inputs['T%d.a' % i])
    try:
        apply_lid_shadow(mat)             # v2.0.65: the user's lid shadow edits survive rebuilds
    except Exception as e:
        print('Cars 2 eyes: lid shadow not applied to %s: %s' % (mat.name, e))
    return True


def sync_values(mat):
    """An eye value edited in the material panel (changed in place, no rebuild): the graft follows, except the
    values the rig drives."""
    from . import c2_model
    g = mat.node_tree.nodes.get(GRAFT) if mat.node_tree is not None else None
    if g is None:
        return
    driven = _driven_params(_plan(_base(mat).c2.owner))
    props = _props(mat)
    for name in c2_model.EYE_PARAMS:
        if name in driven or name not in g.inputs or g.inputs[name].is_linked:
            continue
        v = props.get(name)
        if isinstance(v, list):
            v = v[0]
        v = float(v) if isinstance(v, (int, float)) else 0.0
        if abs(g.inputs[name].default_value - v) > 1e-7:
            g.inputs[name].default_value = v


_pending = set()


def ensure(mat):
    """A material was updated: graft its eye again after a rebuild (deferred - not from inside the depsgraph
    update), or follow an edited value."""
    if not is_eye(mat):
        return
    if has_graft(mat):
        sync_values(mat)
        return
    name = mat.name
    if name in _pending:
        return
    _pending.add(name)

    def run():
        _pending.discard(name)
        from . import charmat
        try:
            m = bpy.data.materials.get(name)
            if m is not None:
                graft(charmat.api(), bpy.context.scene, m)
        except Exception as e:
            print('Cars 2 eyes: graft of %s skipped: %s' % (name, e))
        return None
    bpy.app.timers.register(run, first_interval=0.0)


# ------------------------------------------------------------------------------------- lid shadow (v2.0.65)
# The grey outline band under the top lid (the game draws it at run time in a fixed grey; not in the files).
# USER: adjustable in Blender for renders - darkness, colour, thickness. Stored on the base material
# (mat['c2tk_lid_shadow'], only the edited keys), never exported. Band colour = mix(sclera, colour, darkness):
# black + 0.45 is the toolkit's calibrated game grey. Eevee / Cycles: applied to the graft after every rebuild.
# Game Look: runtime hook on the material system's eye texture (look/ is the Scene Editor's code - not edited);
# its eye shader takes one grey, so a colour shows there as its brightness.
SHADOW_PROP = 'c2tk_lid_shadow'
SHADOW_DEFAULTS = {'darkness': 0.45, 'thickness': 0.03, 'colour': (0.0, 0.0, 0.0)}


def lid_shadow(mat, with_defaults=True):
    base = _base(mat)
    stored = base.get(SHADOW_PROP)
    out = dict(SHADOW_DEFAULTS) if with_defaults else {}
    if stored is not None:
        for k in SHADOW_DEFAULTS:
            if k in stored:
                v = stored[k]
                out[k] = tuple(float(x) for x in v) if k == 'colour' else float(v)
    return out


def _band_colour(s, sclera):
    d = max(0.0, min(1.0, s['darkness']))
    return [sclera * (1.0 - d) + c * d for c in s['colour']]


def _band_mixes(nt, g):
    """The colour mixes the graft's band goes through (factor = the group's 'line')."""
    out = []
    for link in nt.links:
        if link.from_node == g and link.from_socket.name == 'line' and link.to_node.type in ('MIX', 'MIX_RGB'):
            rgba = [x for x in link.to_node.inputs if x.type == 'RGBA']
            if len(rgba) >= 2:
                out.append(rgba[1])
    return out


def apply_lid_shadow(mat):
    """Eevee / Cycles: the graft's band from the edits, or back to the calibrated grey without any."""
    nt = mat.node_tree
    g = nt.nodes.get(GRAFT) if nt is not None else None
    if g is None:
        return
    edited = _base(mat).get(SHADOW_PROP) is not None
    s = lid_shadow(mat)
    for name, v in (('lid_line_darkness', s['darkness']), ('lid_line_width', s['thickness'])):
        if name in g.inputs and not g.inputs[name].is_linked:
            g.inputs[name].default_value = v
    sclera = g.inputs['sclera'].default_value if 'sclera' in g.inputs else 0.95
    for b in _band_mixes(nt, g):
        if edited:
            for l2 in [l2 for l2 in nt.links if l2.to_socket == b]:
                nt.links.remove(l2)                 # was the group's grey line_color
            b.default_value = _band_colour(s, sclera) + [1.0]
        elif not b.is_linked and 'line_color' in g.outputs:
            nt.links.new(g.outputs['line_color'], b)


def _refresh(mat):
    from . import charmat
    base = _base(mat)
    L = charmat.api()
    mats = [base]
    if L is not None:
        try:
            mats += list(L.fxnodes.variants_of(base))
        except Exception:
            pass
    for m in mats:
        try:
            apply_lid_shadow(m)
            if L is not None:
                L.gamerender.touch_material(m)
        except Exception as e:
            print('Cars 2 eyes: lid shadow not applied to %s: %s' % (m.name, e))
    wm = getattr(bpy.context, 'window_manager', None)
    for win in (wm.windows if wm is not None else []):
        for area in win.screen.areas:
            area.tag_redraw()


def _store(mat, key, value):
    base = _base(mat)
    d = dict(base.get(SHADOW_PROP) or {})
    d[key] = value
    base[SHADOW_PROP] = d
    _refresh(base)


def _get_dark(self):
    return lid_shadow(self)['darkness']


def _set_dark(self, v):
    _store(self, 'darkness', float(v))


def _get_thick(self):
    return lid_shadow(self)['thickness']


def _set_thick(self, v):
    _store(self, 'thickness', float(v))


def _get_colour(self):
    return lid_shadow(self)['colour']


def _set_colour(self, v):
    _store(self, 'colour', [float(x) for x in v])


def hook(L):
    """Game Look: the eye texture's band from the material's lid shadow edits."""
    gr = getattr(L, 'gamerender', None)
    ge = getattr(gr, 'gameeyes', None)
    if gr is None or ge is None or getattr(ge.block_values, '_c2tk_shadow', False):
        return
    renderer = getattr(gr, 'Renderer', None)
    if renderer is None or not hasattr(renderer, '_eye_texture'):
        return
    orig_tex, orig_bv = renderer._eye_texture, ge.block_values
    current = [None]

    def _eye_texture(self, mat, eff, textures):
        current[0] = mat
        try:
            return orig_tex(self, mat, eff, textures)
        finally:
            current[0] = None

    def block_values(P, *args, **kw):
        rows = orig_bv(P, *args, **kw)
        mat = current[0]
        try:
            if mat is not None and _base(mat).get(SHADOW_PROP) is not None:
                s = lid_shadow(mat, with_defaults=False)
                sclera = rows[-3][2]
                if 'thickness' in s:
                    r = rows[-3]
                    rows[-3] = (r[0], r[1], r[2], s['thickness'])
                c = _band_colour(lid_shadow(mat), sclera)
                r = rows[-1]
                rows[-1] = (r[0], 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2], r[2], r[3])
        except Exception as e:
            print('Cars 2 eyes: lid shadow (Game Look) skipped: %s' % e)
        return rows

    block_values._c2tk_shadow = True
    renderer._eye_texture = _eye_texture
    ge.block_values = block_values


class CARS2TK_OT_eye_shadow_reset(bpy.types.Operator):
    """Put the lid shadow back to the toolkit's game values"""
    bl_idname = 'cars2tk.eye_shadow_reset'
    bl_label = 'Reset to Game Values'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base = _base(context.material)
        if base.get(SHADOW_PROP) is not None:
            del base[SHADOW_PROP]
        _refresh(base)
        return {'FINISHED'}


class CARS2TK_PT_eye_shadow(bpy.types.Panel):
    """The grey line under the top eyelid (Blender only, not exported)"""
    bl_label = 'Eye Lid Shadow'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'material'

    @classmethod
    def poll(cls, context):
        mat = getattr(context, 'material', None)
        return mat is not None and is_eye(mat)

    def draw(self, context):
        layout = self.layout
        mat = context.material
        col = layout.column(align=True)
        col.prop(mat, 'c2tk_lid_shadow_darkness', text='Darkness', slider=True)
        col.prop(mat, 'c2tk_lid_shadow_colour', text='Colour')
        col.prop(mat, 'c2tk_lid_shadow_thickness', text='Thickness')
        layout.operator(CARS2TK_OT_eye_shadow_reset.bl_idname, icon='LOOP_BACK')
        layout.label(text='Blender only - not exported', icon='INFO')


_classes = (CARS2TK_OT_eye_shadow_reset, CARS2TK_PT_eye_shadow)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    M = bpy.types.Material
    M.c2tk_lid_shadow_darkness = bpy.props.FloatProperty(
        name='Lid Shadow Darkness', min=0.0, max=1.0, get=_get_dark, set=_set_dark,
        description='How strong the line under the top eyelid is (0 = gone, 1 = solid colour). Blender only')
    M.c2tk_lid_shadow_thickness = bpy.props.FloatProperty(
        name='Lid Shadow Thickness', min=0.0, soft_max=0.2, step=0.1, precision=3, get=_get_thick, set=_set_thick,
        description='How far the line reaches below the top eyelid, in eye heights. Blender only')
    M.c2tk_lid_shadow_colour = bpy.props.FloatVectorProperty(
        name='Lid Shadow Colour', subtype='COLOR', size=3, min=0.0, max=1.0, get=_get_colour, set=_set_colour,
        description='Colour of the line under the top eyelid (Game Look shows its brightness). Blender only')


def unregister():
    M = bpy.types.Material
    for p in ('c2tk_lid_shadow_darkness', 'c2tk_lid_shadow_thickness', 'c2tk_lid_shadow_colour'):
        if hasattr(M, p):
            delattr(M, p)
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
