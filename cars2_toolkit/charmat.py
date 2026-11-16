"""Cars 2 character materials, the Cars 2 Scene Editor's way (v2.0).

A character's materials are game materials of the shared material system
(look/, or the Scene Editor's own copy when it is enabled: see look/looklink):

* mat.c2 holds the MaterialPool entry: effect, parameters, texture slots.
  mat.c2.owner is the character's armature, whose Object.c2tex lists the
  character's own TexturePool (slot indices point into it).
* Game Look: the Cars 2 Game render engine (Rendered view) draws every
  material with its effect's own shader. Material Preview / Solid use the
  Blender materials, switched with the same Viewport setting as the Scene
  Editor's (Game Look unlit / Blender lights / textures only).
* Editing: the Scene Editor's Material panel (Pick Effect asks before
  dropping texture slots and parameters the new shader doesn't use, keeps
  them for switching back, and adds the new shader's slots empty; Pick
  Texture; Load Image; parameters with reset; blending / culling).
* Eyes: car_eyes_* / car_double_eyes_* materials take their eye parameters
  from the rig (the eye sliders and the look interpolator over the Eyes
  control, the network the game evaluates), live; the renderer draws the
  game's renderedEyeTexture from them.
* Export: mat.c2 back into the MaterialPool entry (only what changed: an
  untouched character exports byte-identical), dropped slots / parameters
  left out, new and replaced textures into the TexturePool, the .tfx of a
  newly picked effect into effects/. Refused while a texture slot the effect
  samples has no texture.
"""
import json
import os

import bpy
import numpy as np

from . import look

EYE_EFFECTS = ('car_eyes', 'car_double_eyes')
PROP_EYE_PLAN = 'cars2_eye_plan'     # on the armature: the rig plan's eye links and look interpolator (JSON)
_eyes = {}                            # armature pointer -> (plan text, compiled look graph roots)


def api():
    """The running copy of the material system (None: neither registered)."""
    L = look.api()
    if L is not None:
        _alpha_hook(L)
        try:                    # v2.0.65: lid shadow edits in Game Look
            from . import eyelook
            eyelook.hook(L)
        except Exception as e:
            print('Cars 2 Toolkit: eye lid shadow hook not set (%s)' % e)
        try:
            _missing_hook(L)
        except Exception as e:
            print('Cars 2 Toolkit: missing-texture hook not set (%s)' % e)
    return L


# ------------------------------------------------------------------------------------------------ missing textures
# v2.0.68 USER: mcqueendtw's eye reflection never changed in Blender. Its eyeshader (car_eyes_dtmmsmqdrqceism_skin)
# samples specularMapA1 / environmentReflectivityMapA1 but its material lists neither; the material system stood in
# WHITE for a missing texture, and reflection = Amount x (1 - map) -> always 0. The game binds NULL for every sampler
# a material doesn't fill (sdaemon.exe 0x832653 clears samplers with SetTexture(n, NULL)), and a D3D9
# NULL sampler reads (0, 0, 0, 1). So for CHARACTER materials a missing texture reads black. Kept: the flat normal
# stand-in (not verified in game) and the existing lightmap / glow stand-ins. Runtime hook on look/ (the Scene
# Editor's code is not edited); world materials keep the material system's own stand-ins.
CHAR_MISSING_DEFAULT = (0.0, 0.0, 0.0, 1.0)


def _char_missing(base):
    out = dict(base)
    out[''] = CHAR_MISSING_DEFAULT          # '' matches every usage: the D3D9 NULL read, after the special ones
    return out


def _missing_hook(L):
    fx, gr = getattr(L, 'fxnodes', None), getattr(L, 'gamerender', None)
    if fx is None or gr is None or getattr(fx.attach, '_c2tk_missing', False):
        return
    renderer = getattr(gr, 'Renderer', None)
    orig_attach = fx.attach
    orig_bind = getattr(renderer, '_bindings', None)

    def _swapped(mat, fn):
        if mat is None or not _is_character_material(mat):
            return fn()
        keep = fx.MISSING
        fx.MISSING = _char_missing(keep)
        try:
            return fn()
        finally:
            fx.MISSING = keep

    def attach(mat, plan, textures):
        return _swapped(mat, lambda: orig_attach(mat, plan, textures))

    attach._c2tk_missing = True
    fx.attach = attach
    if orig_bind is not None:
        def _bindings(self, mat, eff, textures, gtime):
            return _swapped(mat, lambda: orig_bind(self, mat, eff, textures, gtime))
        renderer._bindings = _bindings
    try:                    # draws made before the hook: bind again
        gr.RENDERER.rebuild()
    except Exception:
        pass


# ------------------------------------------------------------------------------------------------ alpha preview
# v2.0.29: Alpha Modifier (g_AlphaModifier) below 1 makes a character material see-through in game, but
# the preview (Game Look and Eevee) only blends when the EFFECT's render state says AlphaBlendEnable - the
# a_1_... character effects say 0 (opaque), so lowering it showed nothing (USER: McQueen_Headlight_Glass).
# Every character effect reads g_AlphaModifier into its output alpha and already has SrcBlend SRCALPHA /
# DestBlend INVSRCALPHA; only the switch is off. A runtime hook on the material system (look/ is the Scene
# Editor's code - not edited): a character material with Alpha Modifier < 1 counts as alpha-blended, and
# crossing 1 rebuilds it (opaque <-> blended changes its node setup / draw pass). v2.0.30: the shaders'
# output alpha is g_FinalAlpha, not g_AlphaModifier (the game multiplies them on the CPU): Game Look's
# per-draw g_FinalAlpha is now that product - before, the part only moved to the blended pass at alpha 1.
_ALPHA_EPS = 0.999


def _is_character_material(mat):
    c = getattr(mat, 'c2', None)
    if c is None:
        return False
    base = c.variant_of or mat
    return getattr(base.c2, 'owner', None) is not None


def _alpha_of(mat):
    """The material's Alpha Modifier (its game material's, for a lightmap variant)."""
    base = mat.c2.variant_of or mat
    p = next((p for p in base.c2.props if p.name == 'g_AlphaModifier'), None)
    return float(p.fvalue[0]) if p is not None else 1.0


def apply_alpha_nodes(mat):
    """Eevee / Material Preview: the Blender material (C2_Switch -> Material Output) mixed with a
    Transparent BSDF by Alpha Modifier - none of it when the value is 1 (the material as built)."""
    nt = mat.node_tree
    if nt is None:
        return
    a = _alpha_of(mat)
    mix = nt.nodes.get('C2TK_AlphaMix')
    sw = nt.nodes.get('C2_Switch')
    out = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
    if a >= _ALPHA_EPS:
        if mix is not None:
            src = next((l.from_socket for l in nt.links if l.to_node == mix and l.to_socket == mix.inputs[2]), None)
            for n in ('C2TK_AlphaMix', 'C2TK_AlphaClear'):
                if nt.nodes.get(n) is not None:
                    nt.nodes.remove(nt.nodes[n])
            if src is not None and out is not None:
                nt.links.new(src, out.inputs['Surface'])
        return
    if mix is None:
        if sw is None or out is None:
            return
        tr = nt.nodes.new('ShaderNodeBsdfTransparent')
        tr.name = tr.label = 'C2TK_AlphaClear'
        tr.location = (sw.location.x + 200, sw.location.y - 160)
        mix = nt.nodes.new('ShaderNodeMixShader')
        mix.name = 'C2TK_AlphaMix'
        mix.label = 'Alpha Modifier'
        mix.location = (sw.location.x + 400, sw.location.y)
        nt.links.new(tr.outputs[0], mix.inputs[1])
        nt.links.new(sw.outputs[0], mix.inputs[2])
        nt.links.new(mix.outputs[0], out.inputs['Surface'])
    mix.inputs[0].default_value = max(0.0, a)        # 1 = the material, 0 = clear
    try:
        if getattr(mat, 'surface_render_method', 'DITHERED') != 'BLENDED':
            mat.surface_render_method = 'DITHERED'   # Eevee Next: alpha needs dithered / blended
    except (AttributeError, TypeError):
        pass
    try:
        if mat.blend_method == 'OPAQUE':
            mat.blend_method = 'HASHED'              # older Eevee
    except (AttributeError, TypeError):
        pass


def _alpha_hook(L):
    fx = getattr(L, 'fxnodes', None)
    if fx is None or getattr(fx._state, '_c2tk_alpha', False):
        return
    orig_state, orig_changed, orig_value = fx._state, fx.on_param_changed, fx.param_value
    mn = getattr(L, 'matnodes', None)

    def param_value(mat, plan, name, element, comp):
        # every character shader writes its output alpha from g_FinalAlpha (a per-draw value the game
        # computes on the CPU from the material's Alpha Modifier); the preview left it at 1
        v = orig_value(mat, plan, name, element, comp)
        if name == 'g_FinalAlpha' and comp == 0 and mat is not None and _is_character_material(mat):
            try:
                v = float(v) * max(0.0, min(1.0, orig_value(mat, plan, 'g_AlphaModifier', 0, 0)))
            except Exception:
                pass
        return v
    orig_build = getattr(mn, 'build', None)

    def build(mat, *args, **kw):
        r = orig_build(mat, *args, **kw)
        try:
            if _is_character_material(mat):
                apply_alpha_nodes(mat)
        except Exception as e:
            print('Cars 2 Toolkit: alpha preview not set on %s (%s)' % (mat.name, e))
        return r

    def _state(plan, mat, key, default=None):
        v = orig_state(plan, mat, key, default)
        if key == 'AlphaBlendEnable' and not v and mat is not None and _is_character_material(mat):
            try:
                if fx.param_value(mat, plan, 'g_AlphaModifier', 0, 0) < _ALPHA_EPS:
                    return 1
            except Exception:
                pass
        return v

    def on_param_changed(scene, mat, prop):
        orig_changed(scene, mat, prop)
        if mat is None or scene is None or getattr(prop, 'name', '') != 'g_AlphaModifier' or fx.SUSPEND[0]:
            return
        base = mat.c2.variant_of or mat
        if not _is_character_material(base):
            return
        want = float(prop.fvalue[0]) < _ALPHA_EPS
        if base.get('c2tk_alpha_blend') is None or bool(base['c2tk_alpha_blend']) != want:
            base['c2tk_alpha_blend'] = want
            fx.rebuild_material(scene, base)          # opaque <-> blended: new node setup / draw pass
        else:
            for m in [base] + list(fx.variants_of(base)):
                apply_alpha_nodes(m)                  # Eevee: just the mix value

    _state._c2tk_alpha = True
    fx._state = _state
    fx.on_param_changed = on_param_changed
    fx.param_value = param_value
    if orig_build is not None:
        mn.build = build


def _pool(p):
    def num(k):
        tail = k.rsplit('#', 1)[-1]
        return int(tail) if tail.isdigit() else 0
    return [v for _k, v in sorted((p or {}).items(), key=lambda kv: num(kv[0]))] if isinstance(p, dict) \
        else list(p or [])


_chars = {'key': None, 'arms': [], 'meshes': {}}    # found once per object count (worlds hold 5,000+)


def _scan():
    key = len(bpy.data.objects)
    if _chars['key'] != key:
        arms = [o for o in bpy.data.objects if o.type == 'ARMATURE' and o.get('c2_source')]
        ptrs = {a.as_pointer(): a for a in arms}
        meshes = {}
        for o in bpy.data.objects:
            if o.type != 'MESH':
                continue
            owner = o.parent if o.parent is not None and o.parent.as_pointer() in ptrs else next(
                (m.object for m in o.modifiers if m.type == 'ARMATURE' and m.object is not None and
                 m.object.as_pointer() in ptrs), None)
            if owner is not None:
                meshes.setdefault(owner.as_pointer(), []).append(o)
        _chars.update(key=key, arms=arms, meshes=meshes)


def characters(scene=None):
    """Armatures of characters imported with this toolkit (they own a texture table)."""
    try:
        _scan()
        arms = [a for a in _chars['arms'] if a.name]
    except ReferenceError:
        _chars['key'] = None
        _scan()
        arms = _chars['arms']
    # (scene.objects.get scans every object: a character of another scene is
    # simply lit for nothing)
    return list(arms)


def meshes_of(arm):
    """The character's meshes (parented to it or deformed by it)."""
    _scan()
    return list(_chars['meshes'].get(arm.as_pointer(), ()))


def materials_of(arm):
    """The character's game materials (mat.c2.owner == arm)."""
    return [m for m in bpy.data.materials if getattr(m, 'c2', None) is not None and m.c2.owner == arm and
            m.c2.variant_of is None]


# ------------------------------------------------------------------------------------------------ import
def drop_texture_cache(arm):
    """v2.0.13: forget the material system's cached images of this armature
    (fxnodes._owner_contexts, keyed by pointer + name + texture count): after
    a template was removed, or a character deleted and another made with the
    same name, it handed back images that no longer exist ('StructRNA of type
    Image has been removed'). fxnodes is the Scene Editor's file - not edited."""
    L = api()
    cache = getattr(getattr(L, 'fxnodes', None), '_owner_contexts', None) if L is not None else None
    if isinstance(cache, dict) and arm is not None:
        cache.pop(arm.as_pointer(), None)
    if arm is not None:
        arm['c2tex_version'] = arm.get('c2tex_version', 0) + 1


# ------------------------------------------------------------------------------------------------ light role
# v2.0.24: a material can glow WITH the headlights / brake lights. The game links a material to a light by a
# 'Material' association named after the light's network node (Anchor|Headlights -> MaterialRef): the rig
# drives that node's emissiveMultiplier (Main_Ctrl/Glow_Headlights ...) and every material linked to it
# follows. Stock: Headlights / BrakeLights materials only; Boost_Neon links its Body and Tires to
# Anchor|Headlights too (the neon glows with the lights). Tokyo Mater's tires glow all the time instead
# (emissiveMultiplier 1.0, no link) - that is Light: None with a glow map.
LIGHT_NODES = {'HEADLIGHT': 'Headlights', 'BRAKELIGHT': 'BrakeLights'}
LIGHT_ITEMS = (('NONE', 'None', "Not linked to a light (a glow map glows at the material's own "
                                "emissiveMultiplier, like Tokyo Mater's tires)"),
               ('HEADLIGHT', 'Behave as Headlight', "Glows when the headlights do (linked to Anchor|Headlights, "
                                                    "like Boost_Neon's body and tires)"),
               ('BRAKELIGHT', 'Behave as Brakelight', 'Glows when the brake lights do (linked to '
                                                      'Anchor|BrakeLights)'))


def light_roles_of(tree):
    """{MaterialPool index: 'HEADLIGHT' / 'BRAKELIGHT'} from the tree's Material associations."""
    by_node = {v: k for k, v in LIGHT_NODES.items()}
    out = {}
    for a in _pool(tree.get('AssociationPool')):
        if a.get('Type') == 'Material' and isinstance(a.get('MaterialRef'), int):
            role = by_node.get(str(a.get('NodeName', '')).split('|')[-1])
            if role:
                out.setdefault(a['MaterialRef'], role)
    return out


def write_light_roles(tree, roles, names, rep):
    """Make the tree's Material associations match roles {MaterialPool index: role}
    (only the materials listed; the rest untouched). A link to a light the base's
    rig doesn't have is not written (warning). Changes tree in place."""
    pool = tree.get('AssociationPool')
    if not isinstance(pool, dict):
        return
    items = list(pool.values())
    mats = [a for a in items if a.get('Type') == 'Material']
    root = str(mats[0]['NodeName']).split('|')[0] if mats else 'Anchor'
    sub = mats[0].get('SubNetworkRef', 0) if mats else 0
    has = None
    try:
        from c2scene import SceneNetwork
        net = SceneNetwork.from_subnetwork(tree['SubNetworkPool']['SubNetwork#0'])
        has = lambda node: net.has('%s/%s/emissiveMultiplier' % (root, node))
    except Exception:
        pass
    light_names = set(LIGHT_NODES.values())
    changed = False
    for idx, role in sorted(roles.items()):
        want = LIGHT_NODES.get(role)
        if want and has is not None and not has(want):
            rep.warn("%s: Behave as %s not written - this character's rig has no %s light (%s/%s/emissiveMultiplier)"
                     % (names.get(idx, idx), want[:-1], want, root, want))
            want = None
        cur = [a for a in items if a.get('Type') == 'Material' and a.get('MaterialRef') == idx
               and str(a.get('NodeName', '')).split('|')[-1] in light_names]
        for a in cur:
            if want is None or str(a['NodeName']).split('|')[-1] != want:
                items.remove(a)
                changed = True
                rep.info('%s: no longer linked to %s' % (names.get(idx, idx), a['NodeName']))
        if want and not any(str(a['NodeName']).split('|')[-1] == want for a in cur):
            new = {'NodeName': '%s|%s' % (root, want), 'Type': 'Material', 'MaterialRef': idx,
                   'SubNetworkRef': sub}
            items.append(new)                           # at the end of the pool, where Boost_Neon has its links
            changed = True
            rep.info('%s: linked to %s (behaves as %s)' % (names.get(idx, idx), new['NodeName'], want[:-1]))
    if changed:
        prefix = next(iter(pool)).split('#')[0] if pool else 'Association'
        tree['AssociationPool'] = {'%s#%d' % (prefix, i): a for i, a in enumerate(items)}


def setup(context, arm, tree, materials, tex_folder, use_tex, model_path):
    """Make an imported character's materials game materials: its textures in
    arm.c2tex, each material's MaterialPool entry in mat.c2 (with every
    parameter and slot its effect has), its Blender material built. -> count."""
    L = api()
    if L is None or arm is None:
        return 0
    drop_texture_cache(arm)
    scene = context.scene
    stem = os.path.splitext(os.path.basename(model_path))[0]
    arm['c2_source'] = os.path.abspath(model_path)
    arm['c2_stem'] = stem
    arm['c2_effects_dir'] = package_effects(model_path)
    # the game's assets folder (effects): kept on the character, and set for the scene when it has none,
    # so Game Look finds the engine effects
    assets = find_assets(scene, model_path)
    arm['c2_assets'] = assets or ''
    if assets:
        remember_assets(assets)
        if not scene.c2.assets_dir:
            scene.c2.assets_dir = assets
    if scene.c2.root is None and not scene.get(GAME_SHADERS_OFF):
        scene.c2.eevee_game_shaders = True      # v2.0.67: Material Preview / Rendered = the game's shaders
        scene[ENV_NEUTRAL_DONE] = True            # public 2.0.6: built with the solid view display node
    L.fxnodes.SUSPEND[0] += 1
    try:
        images = L.matio.fill_texture_table(arm.c2tex, _pool(tree.get('TexturePool')), tex_folder) if use_tex \
            else {}
        game = L.fxnodes.owner_context(scene, arm)
        mats = _pool(tree.get('MaterialPool'))
        n_tex = len(_pool(tree.get('TexturePool')))
        roles = light_roles_of(tree)
        n = 0
        for i, md in enumerate(mats):
            if i >= len(materials) or materials[i] is None:
                continue
            mat = materials[i]
            mat.cars2_light_role = roles.get(i, 'NONE')
            mat['c2_world'] = stem
            mat['c2_platform'] = 'ARCADE'
            L.matio.fill_material_props(mat, md, i)
            mat[EMPTY_SLOTS] = _empty_slots(md, n_tex)
            if REMOVED_SLOTS in mat:            # (re)built from the file: its slots as the file has them
                del mat[REMOVED_SLOTS]
            mat.c2.owner = arm
            if game is not None:
                meta = L.gamerender.meta_for(game.assets, mat.c2.effect, local=game.local_effects)
                if meta is not None:
                    L.fxnodes.ensure_effect_entries(mat, meta)
            L.matnodes.build(mat, game.textures.images if game is not None else images, game)
            try:            # v2.0.59: the eyes in Eevee / Cycles too (eyelook.py)
                from . import eyelook
                eyelook.graft(L, scene, mat, game.textures.images if game is not None else images)
            except Exception as e:
                print('Cars 2 eyes: %s not grafted: %s' % (mat.name, e))
            try:
                solid_display(mat, L, scene)
            except Exception as e:
                print('Cars 2 Toolkit: solid view texture of %s not set: %s' % (mat.name, e))
            n += 1
    finally:
        L.fxnodes.SUSPEND[0] -= 1
    # Rendered view = Game Look (the Scene Editor's world sets this itself)
    if scene.c2.root is None and L.fxnodes.owner_assets(scene, arm):
        scene.render.engine = L.gamerender.ENGINE_ID
        L.fxnodes.sync_engine(scene)
    if scene.c2.root is None and scene.c2.eevee_game_shaders and scene.c2.viewport_lighting != 'GAME':
        scene.c2.viewport_lighting = 'GAME'     # the game shaders show in the 'Game' materials mode
    return n


def package_effects(model_path):
    """The effects/ folder the character's package ships (its zip has it at the root:
    effects/*.tfx next to characters/<car>/<car>.oct), or ''."""
    d = os.path.dirname(os.path.abspath(model_path))
    for _ in range(4):
        fx = os.path.join(d, 'effects')
        if os.path.isdir(fx) and any(f.lower().endswith('.tfx') for f in os.listdir(fx)):
            return fx
        d = os.path.dirname(d)
    return ''


def _assets_ok(p):
    import arcade
    return bool(p) and os.path.isdir(p) and bool(arcade.effects_dir(p))


def _remembered_path():
    return os.path.join(bpy.utils.user_resource('CONFIG', path='cars2_scene_editor', create=True), 'assets_dir.txt')


def remember_assets(path):
    """Keep the last game assets folder that worked (for characters kept outside the game folder)."""
    try:
        with open(_remembered_path(), 'w', encoding='utf-8') as f:
            f.write(path)
    except OSError:
        pass


def find_assets(scene, model_path):
    """The game's assets folder for a character: the scene's Game Assets Folder,
    else a folder above the character's file that holds effects/effects
    (itself or its assets/), else the last one that worked, else the standard
    Cars 2 Arcade Essentials install. None when none of them is there."""
    p = bpy.path.abspath(scene.c2.assets_dir) if scene.c2.assets_dir else ''
    if _assets_ok(p):
        return p
    d = os.path.dirname(os.path.abspath(model_path))
    while True:
        for c in (d, os.path.join(d, 'assets')):
            if _assets_ok(c):
                return c
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    try:
        with open(_remembered_path(), encoding='utf-8') as f:
            p = f.read().strip()
        if _assets_ok(p):
            return p
    except OSError:
        pass
    p = os.path.join(os.path.expanduser('~'), 'Documents', 'My Games', 'Cars 2 Arcade Essentials', 'assets')
    return p if _assets_ok(p) else None


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    return v


def setup_eyes(arm, plan):
    """Keep the rig plan's eye network on the armature, and start its eye
    sliders at their rest values (clips key them; Remove Clip resets to them)."""
    if arm is None or not plan:
        return
    links = plan.get('eye_links') or []
    rbf = plan.get('eye_rbf')
    if not links and not rbf:
        return
    arm[PROP_EYE_PLAN] = json.dumps(_jsonable({'eye_links': links, 'eye_rbf': rbf}))
    from . import c2_model
    try:
        rests = json.loads(arm.get(c2_model.SLIDER_REST_PROP, '{}'))
    except (TypeError, ValueError):
        rests = {}
    for label, _param, rest in links:
        if label not in arm.keys():
            arm[label] = rest
            if not rest:
                try:
                    from . import anim
                    arm[label] = anim.scalar_rest(label)
                except ImportError:
                    pass
        rests.setdefault(label, float(arm[label]))
    arm[c2_model.SLIDER_REST_PROP] = json.dumps(rests)


def eye_values(arm):
    """{eye material parameter: value} for the rig's current pose."""
    text = arm.get(PROP_EYE_PLAN)
    if not text:
        return {}
    from . import c2_model
    hit = _eyes.get(arm.as_pointer())
    if hit is None or hit[0] != text:
        plan = json.loads(text)
        rbf = plan.get('eye_rbf')
        roots = c2_model.build_look_graph(rbf['keys'], rbf['sds'], rbf['outputs']) if rbf else {}
        hit = _eyes[arm.as_pointer()] = (text, plan, roots)
    _text, plan, roots = hit
    out = {}
    for label, param, rest in plan.get('eye_links', []):
        try:
            out[param] = float(arm.get(label, rest))
        except (TypeError, ValueError):
            pass
    rbf = plan.get('eye_rbf')
    if rbf and rbf.get('bone') in arm.pose.bones:
        loc = arm.pose.bones[rbf['bone']].location
        scale = getattr(c2_model, 'EYE_LOOK_SCALE', None) or rbf.get('frame_scale') or (1.0, 1.0)
        env = {'look_x': np.array([loc[0] * scale[0]]), 'look_y': np.array([loc[1] * scale[1]])}
        for name, node in roots.items():
            out[name] = float(np.asarray(c2_model.eval_numpy(node, env), np.float64).ravel()[0])
    return out


def update_eyes(arm):
    """Give the renderer the rig's eye values for the character's eye materials
    (gamerender.EYE_VALUES: the game evaluates the eye rig every frame and never
    stores the result, so the materials keep their own values and export
    unchanged). Only a change redraws the eye texture."""
    L = api()
    if L is None:
        return
    vals = eye_values(arm)
    if not vals:
        return
    for mat in materials_of(arm):
        if not (mat.c2.effect or '').lower().startswith(EYE_EFFECTS):
            continue
        old = L.gamerender.EYE_VALUES.get(mat.name)
        if old is not None and all(abs(old.get(k, 1e30) - v) < 1e-6 for k, v in vals.items()):
            continue
        L.gamerender.EYE_VALUES[mat.name] = dict(vals)
        L.gamerender.touch_material(mat)


_rigs = {'key': None, 'arms': []}     # armatures with an eye plan, found once per object count


def _eye_rigs(scene):
    key = (scene.name, len(bpy.data.objects))
    if _rigs['key'] != key:
        _rigs['arms'] = [o for o in scene.objects if o.type == 'ARMATURE' and o.get(PROP_EYE_PLAN)]
        _rigs['key'] = key
    return _rigs['arms']


# ------------------------------------------------------------------------------------------------ solid view
# public 2.0.6 USER: Solid view with Color: Texture showed the errorsky panorama raw (its RGBM alpha ignored) on the
# eyes, glass and headlights. The reflections themselves keep using errorsky (USER).
# Blender shows a material's ACTIVE image node there, else the first image node it finds - for materials with no
# diffuse texture that was the reflection panorama of the game-shader nodes. Every character material gets an
# unconnected display node C2_SolidView, made active: its diffuse texture, or no image (Blender then shows the
# material's Viewport Display colour, set from diffuseColorA1). Rendering doesn't use the node.
SOLID_NODE = 'C2_SolidView'


def _diffuse_image(L, scene, mat):
    base = mat.c2.variant_of or mat
    slot = next((t for t in base.c2.textures if t.usage == 'diffuseMapA1' and t.texture >= 0), None)
    if slot is None:
        return None
    try:
        game = L.fxnodes.material_context(scene, base)
        img = game.textures.images.get(slot.texture) if game is not None else None
        if img is not None:
            return img
        table = L.fxnodes.texture_table(scene, base)
        tex = next((t for t in table if t.index == slot.texture), None) if table is not None else None
        return _image_by_path(tex.replace or tex.file) if tex is not None else None
    except Exception:
        return None


def solid_display(mat, L=None, scene=None):
    """Give a character material its Solid view display node (see above). -> True when something changed."""
    if mat is None or mat.node_tree is None or not _is_character_material(mat):
        return False
    L = L or api()
    if L is None:
        return False
    scene = scene or bpy.context.scene
    nt = mat.node_tree
    img = _diffuse_image(L, scene, mat)
    node = nt.nodes.get(SOLID_NODE)
    changed = False
    if node is None:
        node = nt.nodes.new('ShaderNodeTexImage')
        node.name = SOLID_NODE
        node.label = 'Solid View Texture (display only)'
        node.location = (-1600, 1200)
        node.hide = True
        changed = True
    if node.image != img:
        node.image = img
        changed = True
    if nt.nodes.active != node:
        nt.nodes.active = node
        changed = True
    if img is None:
        base = mat.c2.variant_of or mat
        p = next((p for p in base.c2.props if p.name == 'diffuseColorA1' and p.ptype == 'Float'), None)
        if p is not None:
            col = (p.fvalue[0], p.fvalue[1], p.fvalue[2], 1.0)
            if any(abs(a - b) > 1e-4 for a, b in zip(mat.diffuse_color, col)):
                mat.diffuse_color = col
                changed = True
    return changed


_solid_pending = set()


def _ensure_solid(mat):
    """After a rebuild (which clears the tree): the display node again - deferred, not from inside the depsgraph
    update."""
    if mat is None or mat.node_tree is None or not _is_character_material(mat):
        return
    node = mat.node_tree.nodes.get(SOLID_NODE)
    if node is not None and mat.node_tree.nodes.active == node:
        return
    name = mat.name
    if name in _solid_pending:
        return
    _solid_pending.add(name)

    def run():
        _solid_pending.discard(name)
        m = bpy.data.materials.get(name)
        try:
            if m is not None:
                solid_display(m)
        except Exception as e:
            print('Cars 2 Toolkit: solid view texture of %s not set: %s' % (name, e))
        return None
    bpy.app.timers.register(run, first_interval=0.0)


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph):
    """A posed / edited rig: its eye materials follow (only the updated armatures are looked at)."""
    glow = set()
    for u in depsgraph.updates:
        if isinstance(u.id, bpy.types.Material):
            # a material edit (shader switch, a texture picked for a glow slot ...): the
            # Emissions preview follows it, whenever the toggle was turned on
            mat = u.id.original
            try:            # v2.0.59: a rebuilt eye material gets its Eevee / Cycles eye back (eyelook.py)
                from . import eyelook
                eyelook.ensure(mat)
                _ensure_solid(mat)
            except ReferenceError:
                pass
            owner = getattr(getattr(mat, 'c2', None), 'owner', None)
            if owner is not None and getattr(owner, 'cars2_emissions', False):
                glow.add(owner)
            continue
        o = u.id.original if isinstance(u.id, bpy.types.Object) else None
        if o is not None and o.type == 'ARMATURE' and o.get(PROP_EYE_PLAN):
            try:
                update_eyes(o)
            except ReferenceError:
                pass
    for arm in glow:
        try:
            apply_emissions(scene, arm)
        except ReferenceError:
            pass


@bpy.app.handlers.persistent
def _on_frame(scene, depsgraph=None):
    """Playback / scrubbing: the clips key the eye sliders."""
    for arm in _eye_rigs(scene):
        try:
            update_eyes(arm)
        except ReferenceError:
            _rigs['key'] = None


@bpy.app.handlers.persistent
def _on_file_load(*_a):
    _on_load()
    try:
        api()               # installs the runtime hooks before Game Look draws the file
    except Exception:
        pass
    bpy.app.timers.register(_upgrade_old_scene, first_interval=0.3)


def _on_load(*_a):
    _eyes.clear()
    _rigs['key'] = None
    _chars['key'] = None
    # preview values live in memory: the toggles saved with the file show them again
    bpy.app.timers.register(lambda: _reapply_emissions() and None, first_interval=0.1)


# ------------------------------------------------------------------------------------------------ emissions
# The game switches a character's glow maps on at run time (brake lights when
# braking ...): 85 of the 86 character materials with an emissiveMapA1 store
# emissiveMultiplier 0, so they show nothing in a preview. The Emissions toggle
# (Look panel) shows them at 1, for this preview only: the value goes to
# fxnodes.PREVIEW_VALUES, never into the material or an export.

def emissive_materials(arm):
    """The character's materials with a glow map."""
    return [m for m in materials_of(arm)
            if any(t.usage == 'emissiveMapA1' and t.texture >= 0 for t in m.c2.textures)]


def apply_emissions(scene, arm):
    """Show (or hide again) the character's glow maps in Game Look and the Blender materials."""
    L = api()
    if L is None:
        return
    on = bool(getattr(arm, 'cars2_emissions', False))
    strength = float(getattr(arm, 'cars2_emission_strength', 1.0))
    glowing = emissive_materials(arm)
    for mat in materials_of(arm):
        if mat not in glowing and L.fxnodes.PREVIEW_VALUES.pop(mat.name, None) is not None:
            L.gamerender.touch_material(mat)       # its glow map was switched away / cleared
            L.fxnodes.rebuild_material(scene, mat)
    for mat in glowing:
        stored = next((p.fvalue[0] for p in mat.c2.props if p.name == 'emissiveMultiplier'), 0.0)
        before = L.fxnodes.PREVIEW_VALUES.get(mat.name)
        if on:
            # a glow the game leaves off (0) shows at Strength; one stored lit is scaled by it
            L.fxnodes.PREVIEW_VALUES[mat.name] = {'emissiveMultiplier': strength * (stored if stored > 0.0 else 1.0)}
        else:
            L.fxnodes.PREVIEW_VALUES.pop(mat.name, None)
        if before != L.fxnodes.PREVIEW_VALUES.get(mat.name):
            L.gamerender.touch_material(mat)
            L.fxnodes.rebuild_material(scene, mat)


def _emissions_update(self, context):
    apply_emissions(context.scene, self)


def _reapply_emissions():
    scene = bpy.context.scene
    for arm in characters():
        if getattr(arm, 'cars2_emissions', False):
            try:
                apply_emissions(scene, arm)
            except ReferenceError:
                pass


# ------------------------------------------------------------------------------------------------ export
def own_slot(mat, usage):
    """v2.0.20: the material's OWN MaterialPool entry lists this texture slot.
    Slots the effect can sample but the entry doesn't list (marked extra -
    mcqueendtw's eyeshader on car_eyes_dtmmsmqdrqceism_skin has no
    specularMapA1 / environmentReflectivityMapA1) ship empty in the game:
    never required (USER RULE: a material that doesn't use a texture must
    not be asked for one). The exporter writes an extra slot only when it got
    a texture."""
    t = next((t for t in mat.c2.textures if t.usage == usage), None)
    return t is not None and not t.extra and usage not in empty_slots(mat)


# v2.0.62: slots the character's own file lists but leaves without a texture (bobby_Body's normalMapA1 ->
# TextureReference 100 of a 13-texture pool): the game draws them with no texture there, so they are never
# required (USER RULE: a character without a normal map imports and exports as is). Written back unchanged.
EMPTY_SLOTS = 'c2tk_empty_slots'


def _empty_slots(md, n_tex):
    out = []
    for te in (md.get('TextureEntries') or {}).values():
        if not isinstance(te, dict):
            continue
        ref = te.get('TextureReference')
        if not isinstance(ref, int) or not 0 <= ref < n_tex:
            out.append(te.get('Usage', ''))
    return out


def empty_slots(mat):
    v = mat.get(EMPTY_SLOTS)
    if v is None:                   # imported before 2.0.62: read the source .oct once and tag
        v = []
        owner = getattr(mat.c2, 'owner', None)
        src = owner.get('c2_source') if owner is not None else None
        idx = mat.c2.index
        if src and os.path.isfile(src) and idx >= 0:
            try:
                import c2oct
                if src not in _SRC_CACHE:
                    _SRC_CACHE[src] = c2oct.load(src)
                tree = _SRC_CACHE[src]
                mats = _pool(tree.get('MaterialPool'))
                if idx < len(mats):
                    v = _empty_slots(mats[idx], len(_pool(tree.get('TexturePool'))))
            except Exception as e:
                print('Cars 2 Toolkit: empty slots of %s not read: %s' % (mat.name, e))
        try:
            mat[EMPTY_SLOTS] = v
        except Exception:           # drawing (read-only context): keep it for this call only
            pass
    return set(v)


_SRC_CACHE = {}


def used_materials(arm):
    """v2.0.21: the materials that have faces on the character's meshes (what
    gets drawn). A base can carry materials no mesh uses - King, a McQueen
    port, keeps Headlights / BrakeLights / McQueen_Body pointing at texture
    indices past its 9-texture pool; the game never draws them."""
    from . import geometry
    used = set()
    for o in geometry.character_meshes(arm):
        slots = o.material_slots
        if not slots:
            continue
        idx = np.zeros(len(o.data.polygons), dtype=np.int32)
        o.data.polygons.foreach_get('material_index', idx)
        for i in set(idx.tolist()):
            if 0 <= i < len(slots) and slots[i].material is not None:
                m = slots[i].material
                used.add(m.c2.variant_of or m if getattr(m, 'c2', None) is not None else m)
    return used


def missing(scene, arm):
    """['material: slot'] of the used materials' (used_materials) own texture
    slots (own_slot) that have no texture."""
    L = api()
    if L is None:
        return []
    used = used_materials(arm)
    mats = [m for m in materials_of(arm) if m in used]
    return ['%s: %s' % (m.name, L.fxnodes.usage_label(u)) for m, u in L.fxnodes.missing_in(scene, mats)
            if own_slot(m, u)]


class _Report:
    def __init__(self):
        self.lines, self.warnings = [], []

    def info(self, msg):
        self.lines.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)
        self.lines.append('WARNING: ' + msg)


def exporter(scene, arm):
    """The export's material step: apply(tree) -> (log lines, errors, {effect: .tfx bytes})
    writes the character's materials and textures into the tree it exports."""
    L = api()

    def apply(tree):
        if L is None:
            return ['materials: the material system is not running - left as they were'], [], {}
        import c2fx
        rep = _Report()
        mkeys = list(tree.get('MaterialPool', {}).keys())
        tkeys = list(tree.get('TexturePool', {}).keys())
        mats = [tree['MaterialPool'][k] for k in mkeys]
        texs = [tree['TexturePool'][k] for k in tkeys]
        n_orig = len(texs)
        L.matio.add_new_textures(texs, arm.c2tex, rep, crc=c2fx.crc)
        L.matio.apply_replacements(texs, arm.c2tex, n_orig, rep, crc=c2fx.crc)
        tfx = {}
        game = L.fxnodes.owner_context(scene, arm)
        for mat in materials_of(arm):
            c = mat.c2
            if not 0 <= c.index < len(mats):
                rep.warn('%s: not one of the character\'s materials (pool index %d)' % (mat.name, c.index))
                continue
            m = mats[c.index]
            old = m.get('Effect', '')
            m['Effect'] = c.effect or old
            if 'RenderCaps' in m:
                m['RenderCaps'] = c.render_caps
            L.matio.write_props(m, c, len(texs))
            k = _drop_removed(m, mat)
            if k:
                rep.info('%s: %d texture slot(s) removed (%s)' % (mat.name, k, ', '.join(sorted(removed_slots(mat)))))
            if m['Effect'] != old:
                rep.info('%s: effect %s -> %s' % (mat.name, old, m['Effect']))
                raw = _effect_bytes(game, m['Effect'])
                if raw is not None:
                    tfx[m['Effect']] = raw          # the character's package ships its effects
                else:
                    rep.warn('%s: %s.tfx not found to ship with it' % (mat.name, m['Effect']))
        roles, names = {}, {}
        for mat in materials_of(arm):
            if 0 <= mat.c2.index < len(mats):
                roles[mat.c2.index] = getattr(mat, 'cars2_light_role', 'NONE')
                names[mat.c2.index] = mat.name
        write_light_roles(tree, roles, names, rep)
        texs, changed = compact_textures(texs, mats, rep)
        prefix = tkeys[0].split('#')[0] if tkeys else 'Texture'
        if changed:                                   # renumbered: every key follows the new order
            tree['TexturePool'] = {'%s#%d' % (prefix, i): t for i, t in enumerate(texs)}
        else:
            tree['TexturePool'] = {('%s#%d' % (prefix, i)) if i >= len(tkeys) else tkeys[i]: t
                                   for i, t in enumerate(texs)}
        return rep.lines, [], tfx
    return apply


def compact_textures(texs, mats, rep):
    """v2.0.32: one TexturePool entry per texture name, and no entry nothing uses.

    * Entries with the same Name (case ignored: McQueenRookie_body_d / mcqueenrookie_body_d) become one: the
      first (lowest) slot is kept, with its name and source path, and takes the image of the newest entry of
      that name a material uses (the one picked last). Before v2.0.32 Load Image As Texture always added a new
      entry, so loading a texture again stacked copies in the pool.
    * Entries no material's TextureEntries reference are removed, and every TextureReference is renumbered.

    TextureEntries are the only references into a character's TexturePool, and no shipped character has an
    unused or a duplicate texture (all 171 character .octs of the Arcade build), so a vanilla character comes
    out unchanged. -> (texture list, changed?)"""
    refs = [te for m in mats for te in (m.get('TextureEntries') or {}).values()
            if isinstance(te.get('TextureReference'), int) and 0 <= te['TextureReference'] < len(texs)]
    used = {te['TextureReference'] for te in refs}
    groups = {}
    for i, t in enumerate(texs):
        groups.setdefault(str(t.get('Name', '')).lower(), []).append(i)
    target = list(range(len(texs)))
    for name, idx in groups.items():
        if len(idx) < 2 or not name:
            continue
        keep = idx[0]
        live = [i for i in idx if i in used]
        src = live[-1] if live else keep
        if src != keep:
            for k in ('Data', 'DataCRC'):
                if k in texs[src]:
                    texs[keep][k] = texs[src][k]
                elif k in texs[keep]:
                    del texs[keep][k]
        for i in idx[1:]:
            target[i] = keep
        rep.info('texture "%s": %d entries of this name merged into slot %d%s'
                 % (texs[keep].get('Name'), len(idx), keep, ' (image of slot %d)' % src if src != keep else ''))
    for te in refs:
        te['TextureReference'] = target[te['TextureReference']]
    used = sorted({te['TextureReference'] for te in refs})
    if used == list(range(len(texs))):
        return texs, False
    gone = [str(texs[i].get('Name')) for i in range(len(texs)) if i not in set(used)]
    new_index = {old: new for new, old in enumerate(used)}
    for te in refs:
        te['TextureReference'] = new_index[te['TextureReference']]
    rep.info('TexturePool: %d unused entr%s removed (%s), %d kept'
             % (len(gone), 'y' if len(gone) == 1 else 'ies', ', '.join(gone[:12]) + (' ...' if len(gone) > 12 else ''),
                len(used)))
    return [texs[i] for i in used], True


def _effect_bytes(game, effect):
    """The .tfx bytes of an effect: the character's own copy, else the game's."""
    if game is None:
        return None
    if game.local_effects and effect.lower() in game.local_effects:
        return game.local_effects[effect.lower()]
    import arcade
    d = arcade.effects_dir(game.assets)
    p = os.path.join(d, effect.lower() + '.tfx') if d else ''
    if p and os.path.exists(p):
        with open(p, 'rb') as f:
            return f.read()
    return None


# ------------------------------------------------------------------------------------------------ operators
class CARS2TK_OT_rebuild_materials(bpy.types.Operator):
    """Make this character's materials game materials again from its source .oct (also brings characters
    imported before v2.0 to the new materials). Edits made to its materials are lost"""
    bl_idname = 'cars2tk.rebuild_materials'
    bl_label = 'Rebuild Character Materials'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        from . import character
        arm = character.active_character(context)
        return arm is not None and bool(arm.get(character.PROP_SCENE)) and api() is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        from . import character, geometry
        import c2oct
        arm = character.active_character(context)
        src = arm[character.PROP_SCENE]
        try:
            tree = c2oct.load(src)
        except OSError as e:
            self.report({'ERROR'}, 'Cannot read %s: %s' % (src, e))
            return {'CANCELLED'}
        mats = {}
        for o in bpy.data.objects:
            if o.type == 'MESH' and (o.parent == arm or any(m.type == 'ARMATURE' and m.object == arm
                                                            for m in o.modifiers)):
                for s in o.material_slots:
                    if s.material is not None and geometry.PROP_MAT in s.material:
                        mats[int(s.material[geometry.PROP_MAT])] = s.material
        n_pool = len(_pool(tree.get('MaterialPool')))
        lst = [mats.get(i) for i in range(n_pool)]
        from . import c2_model
        folder = c2_model.guess_textures_folder(src + '.json')      # (missing .dds files are written again)
        for m in lst:
            if m is not None and 'c2_dropped' in m:
                del m['c2_dropped']
        n = setup(context, arm, tree, lst, folder, True, src)
        self.report({'INFO'}, '%d material(s) rebuilt' % n)
        return {'FINISHED'}


# ------------------------------------------------------------------------------------------------ panels
def _draw_look(layout, context):
    """Viewport: Game Look in Rendered view; the Blender materials' lighting mode; the game render options."""
    L = api()
    sc = context.scene.c2
    col = layout.column()
    if L is None:
        col.label(text='Material system not running', icon='ERROR')
        return
    engine = context.scene.render.engine == L.gamerender.ENGINE_ID
    row = col.row(align=True)
    row.label(text='Rendered view:', icon='SHADING_RENDERED')
    op = row.operator('cars2tk.set_engine', text='Game Look', depress=engine)
    op.game = True
    op = row.operator('cars2tk.set_engine', text='Blender', depress=not engine)
    op.game = False
    col.prop(sc, 'viewport_lighting', text='Materials')
    row = col.row(align=True)
    on = bool(sc.eevee_game_shaders)
    op = row.operator('cars2tk.eevee_game_shaders', text='Game Shaders (Preview / Rendered)',
                      icon='SHADING_TEXTURE', depress=on)
    op.on = not on
    if on and sc.viewport_lighting != 'GAME':
        col.label(text="Game shaders show with Materials: Game", icon='INFO')
    if engine:
        g = col.column(align=True)
        r = g.row(align=True)
        r.prop(sc, 'game_shadows', toggle=True)
        r.prop(sc, 'game_reflections', toggle=True)
        r = g.row(align=True)
        r.prop(sc, 'game_bloom', toggle=True)
        r.prop(sc, 'game_auto_exposure', toggle=True)
        if not sc.game_auto_exposure:
            g.prop(sc, 'exposure_luminance')
    from . import character
    arm = character.active_character(context)
    if arm is not None and hasattr(arm, 'cars2_emissions'):
        n = len(emissive_materials(arm))
        row = col.row(align=True)
        row.enabled = n > 0
        row.prop(arm, 'cars2_emissions', toggle=True, icon='LIGHT_SUN',
                 text='Emissions (%d glow map%s)' % (n, '' if n == 1 else 's') if n else 'Emissions (no glow maps)')
        if arm.cars2_emissions:
            row.prop(arm, 'cars2_emission_strength', text='Strength')
    col.prop(sc, 'assets_dir', text='Game Assets')
    if sc.root is not None:
        col.label(text='Lit by the Scene Editor world %s' % sc.world_name, icon='WORLD')


def _material_of(context):
    o = context.active_object
    mat = o.active_material if o is not None else None
    if mat is not None and getattr(mat, 'c2', None) is not None and mat.c2.variant_of is not None:
        mat = mat.c2.variant_of
    return mat


def _is_eye_shape(name):
    return name.startswith(('eyeBall', 'eyeLid'))


def _draw_param_row(L, layout, mat, p, gamma, reset):
    """One parameter row, as lookui.draw_mat_props draws it (* = changed from the effect default)."""
    row = layout.row(align=True)
    changed = p.extra and any(abs(a - b) > 1e-6 for a, b in zip(p.fvalue, p.fdefault)) if p.ptype == 'Float' else False
    row.label(text=L.lookui.nice_name(p.name) + (' *' if changed else ''))
    if p.ptype == 'Float':
        if p.size >= 3 and ('olor' in p.name or 'Light' in p.name or 'Tint' in p.name):
            row.prop(p, 'color_gamma' if p.name in gamma else 'color', text='')
            if p.size == 4:
                row.prop(p, 'fvalue', index=3, text='')
        else:
            for k in range(p.size):
                row.prop(p, 'fvalue', index=k, text='')
    else:
        row.prop(p, 'ivalue', text='')
    if reset:
        op = row.operator('c2.reset_param', text='', icon='LOOP_BACK')
        op.material, op.name = mat.name, p.name


def _draw_eye_props(L, layout, mat, reset=False):
    """Parameters of an eye material: shading (specular, reflection, colours) first, eye shape below."""
    c = mat.c2
    gamma = set(L.fxnodes.SHARED_GAMMA) | set(mat.get('c2_gamma_params', ()))
    params = [p for p in c.props if p.name not in L.lookui.STATE_PARAMS and p.name != 'g_BoneCount']
    shading = sorted((p for p in params if not _is_eye_shape(p.name)), key=lambda p: p.name)
    shape = [p for p in params if _is_eye_shape(p.name)]
    layout.label(text='Eye Shader', icon='SHADING_RENDERED')
    for p in shading:
        _draw_param_row(L, layout, mat, p, gamma, reset)
    if shape:
        box = layout.box()
        box.label(text='Eye Shape (eyeballs, eyelids)', icon='HIDE_OFF')
        box.label(text='Values the rig animates follow the eye sliders', icon='INFO')
        for p in shape:
            _draw_param_row(L, box, mat, p, gamma, reset)


def _mark(fn, orig):
    """public 2.0.4: tag a temporary panel hook with the function it stands in for."""
    fn._c2tk_orig = orig
    return fn


def _unwrapped(module, name):
    """module.name without any toolkit hook still on it (left by an older loaded copy of the add-on, e.g. after
    the files were replaced while Blender ran): hooks chained onto stale hooks blanked the panel."""
    fn = getattr(module, name)
    seen = 0
    while getattr(fn, '_c2tk_orig', None) is not None and seen < 16:
        fn, seen = fn._c2tk_orig, seen + 1
    # a 2.0.2 / 2.0.3 slot hook (unmarked) from an older loaded copy: its own module knows the original
    while getattr(fn, '__name__', '') == '_draw_slot' and (fn.__module__ or '').endswith('charmat')             and seen < 16:
        prev = (getattr(fn, '__globals__', {}).get('_ORIG_SLOT') or [None])[0]
        if prev is None or prev is fn:
            break
        fn, seen = prev, seen + 1
    setattr(module, name, fn)
    return fn


def _draw_material(layout, context, mat):
    L = api()
    if L is None:
        layout.label(text='Material system not running', icon='ERROR')
        return
    if mat is None or not mat.c2.effect:
        layout.label(text='Not a Cars 2 game material', icon='INFO')
        return
    layout.label(text=mat.name, icon='MATERIAL')
    # v2.0.20: the panel (the Scene Editor's draw code - not edited) marks every slot the effect samples as
    # 'needs a texture before export'; for this draw only, required = this material's own slots (own_slot)
    fx = L.fxnodes
    req = fx.required_textures
    # v2.0.21: and nothing at all for a material no mesh of its character uses (not drawn, not checked)
    owner = mat.c2.owner
    drawn = owner is None or mat in used_materials(owner)
    fx.required_textures = lambda plan: [u for u in req(plan) if drawn and own_slot(mat, u)]
    # v2.0.62: the X of an empty slot removes the slot (_draw_slot); removed slots are not listed
    orig_slot = _unwrapped(L.lookui, 'draw_texture_slot')
    _ORIG_SLOT[0] = orig_slot
    _draw_slot._c2tk_orig = orig_slot
    L.lookui.draw_texture_slot = _draw_slot
    # public 2.0.1: an eye material's shading values (specular, reflection, colours) first, the ~35 eye shape
    # values (eyeBall* / eyeLid*) in their own section under them
    orig_props = _unwrapped(L.lookui, 'draw_mat_props')
    if (mat.c2.effect or '').lower().startswith(EYE_EFFECTS):
        L.lookui.draw_mat_props = _mark(lambda lay, m, reset=False: _draw_eye_props(L, lay, m, reset), orig_props)
    try:
        L.lookui.draw_arcade(context, layout, mat)
    finally:
        fx.required_textures = req
        L.lookui.draw_texture_slot = orig_slot
        L.lookui.draw_mat_props = orig_props
    gone = removed_slots(mat)
    if gone:
        box = layout.box()
        box.label(text='Removed texture slots (left out of the export):', icon='TRASH')
        for u in sorted(gone):
            r = box.row()
            r.label(text=L.lookui.nice_name(u), icon='BLANK1')
            op = r.operator('cars2tk.restore_texture_slot', text='Restore', icon='LOOP_BACK')
            op.material, op.usage = mat.name, u
    _draw_light_role(layout, mat)
    layout.separator()
    L.lookui.draw_texture_list(layout, context, mat)


# public 2.0.2 USER: every texture slot shows its thumbnail. The panel finds a slot's image only by the exact file
# path of the texture table entry (matio.image_at); when no loaded image has that path (the importer loaded the
# diffuse from another spelling of the path, the file was re-exported, the image was dropped on save) the slot
# showed no thumbnail. Fallback while drawing (no data changes in draw): the image the material's shader uses for
# that texture, else a loaded image of the same file name; and the file is loaded once from a timer so the next
# redraw finds it by path.
_THUMB_PENDING = set()


def _queue_image_load(path):
    p = bpy.path.abspath(path)
    if not p or p in _THUMB_PENDING or not os.path.isfile(p):
        return
    _THUMB_PENDING.add(p)

    def run():
        try:
            bpy.data.images.load(p, check_existing=True)
        except Exception as e:
            print('Cars 2 Toolkit: texture thumbnail %s not loaded: %s' % (p, e))
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                a.tag_redraw()
        return None
    bpy.app.timers.register(run, first_interval=0.0)


def _fallback_image(L, scene, mat, slot, path):
    try:
        game = L.fxnodes.material_context(scene, mat)
        img = game.textures.images.get(slot.texture) if game is not None else None
        if img is not None:
            return img
    except Exception:
        pass
    want = os.path.basename(path.replace('\\', '/')).lower()
    stem = os.path.splitext(want)[0]
    for img in bpy.data.images:
        if img.source != 'FILE':
            continue
        if os.path.basename(img.filepath.replace('\\', '/')).lower() == want or img.name.lower() in (want, stem):
            return img
    return None


# public 2.0.3 USER: cube map slots (envMapInstance errorsky, environmentMap chrome ...) show a thumbnail too,
# written once per file version as a small PNG in the temp folder by a timer (never while drawing), then loaded.
# 2.0.6: RGBM decoded, as a panorama (_write_cube_thumb).


def _cube_thumb_path(path):
    import hashlib
    import tempfile
    p = bpy.path.abspath(path)
    try:
        stamp = '%s|%d|%d' % (os.path.normcase(os.path.abspath(p)), os.path.getmtime(p), os.path.getsize(p))
    except OSError:
        return None
    key = hashlib.md5(stamp.encode('utf-8')).hexdigest()[:12]
    name = os.path.splitext(os.path.basename(p))[0]
    return os.path.join(tempfile.gettempdir(), 'cars2_toolkit_thumbs', '%s_cube2_%s.png' % (name, key))


def _write_cube_thumb(src, dst):
    """public 2.0.6: the cube as the game shows it - RGBM decoded like the character shaders do
    (light = (rgb * a * 6)^2, every character cube is RGBM: errorsky / bluesky / chrome store alpha <= 42/255),
    shown as the panorama the preview samples (sky on top, ground below), written as sRGB."""
    import cubemap
    with open(src, 'rb') as f:
        faces = cubemap.cube_faces(f.read())       # +X -X +Y -Y +Z -Z, (s, s, 4) floats
    pano = np.asarray(cubemap.equirect(faces, width=128), dtype=np.float32)
    light = (pano[..., :3] * pano[..., 3:4] * 6.0) ** 2
    out = np.ones(pano.shape, dtype=np.float32)
    out[..., :3] = np.clip(light, 0.0, 1.0) ** (1.0 / 2.2)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    cubemap.write_png(dst, out)


def _queue_cube_thumb(src, dst):
    if dst in _THUMB_PENDING:
        return
    _THUMB_PENDING.add(dst)

    def run():
        try:
            if not os.path.isfile(dst):
                _write_cube_thumb(bpy.path.abspath(src), dst)
            bpy.data.images.load(dst, check_existing=True)
        except Exception as e:
            print('Cars 2 Toolkit: cube map thumbnail of %s not made: %s' % (src, e))
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                a.tag_redraw()
        return None
    bpy.app.timers.register(run, first_interval=0.0)


def _norm_path(path):
    return os.path.normcase(os.path.abspath(bpy.path.abspath(path))) if path else ''


def _image_by_path(path):
    """public 2.0.5: the loaded bpy image of a file, by its path - the toolkit's own lookup. The material system
    that runs may be the Scene Editor's copy, whose panel finds images only by NAME = file name ('x.dds'), so the
    diffuse the character importer names 'x' (no .dds) never showed; older copies have no matio.image_at."""
    if not path:
        return None
    p = _norm_path(path)
    for img in bpy.data.images:
        if img.source == 'FILE' and img.filepath and _norm_path(img.filepath) == p:
            return img
    return None


def _cube_thumb(L, path):
    """The thumbnail image of a cube map file, or None (then it is being made)."""
    dst = _cube_thumb_path(path) if path else None
    if dst is None:
        return None
    img = _image_by_path(dst)
    if img is None:
        _queue_cube_thumb(path, dst)
    return img


def _slot_image(L, scene, mat, slot, tex):
    """The thumbnail image of a texture slot's texture, or None (then it is being loaded / made)."""
    path = tex.replace or tex.file
    kind = (tex.ttype or '').lower()
    if kind == 'cube':
        return _cube_thumb(L, path)
    if kind != '2d':
        return None
    img = _image_by_path(path)
    if img is None and path:
        _queue_image_load(path)
        img = _fallback_image(L, scene, mat, slot, path)
    return img


# public 1.0 (Blender 4.1): previews of images the panel draws stay empty there (icon size 0 x 0 - newer
# Blender renders them when template_icon draws them). A preview still empty a moment after it was first drawn
# is filled once from the image's pixels (downscaled), from a timer - never while drawing.
_PREVIEW_PENDING = set()
_PREVIEW_SIZE = 128


def _fill_preview(img):
    w, h = img.size
    if not w or not h:
        return False
    px = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    px = px.reshape(h, w, 4)
    n = _PREVIEW_SIZE
    big = np.ascontiguousarray(px[(np.arange(n) * h) // n][:, (np.arange(n) * w) // n])
    pv = img.preview
    pv.image_size = (n, n)
    pv.image_pixels_float.foreach_set(big.ravel())
    small = np.ascontiguousarray(big[::4, ::4])
    pv.icon_size = (n // 4, n // 4)
    pv.icon_pixels_float.foreach_set(small.ravel())
    return True


def _ensure_preview(img):
    """Call when a preview is drawn: a preview Blender leaves empty gets filled (see above)."""
    try:
        if img.preview is None or img.preview.icon_size[0] > 0:
            return
    except (AttributeError, ReferenceError):
        return
    name = img.name
    if name in _PREVIEW_PENDING:
        return
    _PREVIEW_PENDING.add(name)

    def run():
        im = bpy.data.images.get(name)
        try:
            if im is not None and im.preview is not None and im.preview.icon_size[0] == 0:
                if _fill_preview(im):
                    for w in bpy.context.window_manager.windows:
                        for a in w.screen.areas:
                            a.tag_redraw()
        except Exception as e:
            print('Cars 2 Toolkit: thumbnail of %s not made: %s' % (name, e))
        _PREVIEW_PENDING.discard(name)      # (filled now: icon size > 0, not queued again)
        return None
    bpy.app.timers.register(run, first_interval=1.0)    # newer Blender fills it itself meanwhile


def _draw_tex_slot(L, layout, scene, mat, slot, tex, required):
    """lookui.draw_texture_slot's row (thumbnail, name / info, Pick and Load Image) drawn by the toolkit, so it
    works the same with every copy of the material system (Scene Editor, World Importer, the toolkit's own)."""
    fx = L.fxnodes
    row = layout.row()
    col = row.column()
    img = _slot_image(L, scene, mat, slot, tex)
    if img is not None:
        img.preview_ensure()
        _ensure_preview(img)
        col.template_icon(icon_value=img.preview.icon_id, scale=3.0)
    else:
        col.label(text='', icon='IMAGE_DATA')
    col = row.column(align=True)
    col.label(text=L.lookui.nice_name(slot.usage))
    col.label(text='%s  (%s)' % (tex.name, tex.info))
    r = col.row(align=True)
    op = r.operator('cars2tk.pick_texture', text='Pick', icon='VIEWZOOM')
    op.material, op.usage = mat.name, slot.usage
    try:
        cube = fx.slot_is_cube(scene, mat, slot.usage)
    except Exception:
        cube = None
    op.cube = cube if cube is not None else (tex.ttype or '').lower() == 'cube'
    op = r.operator('cars2tk.load_texture', text='Load Image', icon='FILEBROWSER')
    op.material, op.usage = mat.name, slot.usage


def _pick_items(self, context):
    """Pick Texture's search list (as the material system's, with the toolkit's own image lookup): the character's
    textures, those of the slot's kind (2D / cube) first, a thumbnail where the file loads."""
    L = api()
    items = [('-1', '(none)', 'No texture: the slot uses a neutral constant', 'X', 0)]
    if L is None:
        _pick_items.cache = items
        return items
    scene = context.scene
    mat = _mat_named(self.material) if self.material else None
    table = L.fxnodes.texture_table(scene, mat) if mat is not None else None
    want_cube = self.cube
    for t in sorted(table or (), key=lambda t: ((t.ttype.lower() == 'cube') != want_cube, t.name.lower())):
        is_cube = t.ttype.lower() == 'cube'
        other = is_cube != want_cube
        icon = 0
        path = bpy.path.abspath(t.replace or t.file)
        if is_cube:
            img = _cube_thumb(L, t.replace or t.file)
        else:
            img = _image_by_path(path)
            if img is None and path and os.path.exists(path):
                try:
                    img = bpy.data.images.load(path, check_existing=True)
                except RuntimeError:
                    img = None
        if img is not None:
            img.preview_ensure()
            _ensure_preview(img)
            icon = img.preview.icon_id
        label = t.name + ((' (cube map)' if is_cube else ' (2D)') if other else '')
        tip = '%s  %s' % (t.info, t.source_path) + (
            '  -  not the kind this slot samples (%s)' % ('a cube map' if want_cube else '2D') if other else '')
        items.append((str(t.index), label, tip, icon, t.index + 1))
    _pick_items.cache = items       # Blender needs the list kept alive
    return items


class CARS2TK_OT_pick_texture(bpy.types.Operator):
    """Choose one of the character's textures for this slot"""
    # public 2.0.5: the toolkit's own Pick for character materials (thumbnails found the same way with or without
    # the Scene Editor; its older copy looked them up by name and missed the importer's diffuse images)
    bl_idname = 'cars2tk.pick_texture'
    bl_label = 'Pick Texture'
    bl_options = {'REGISTER', 'UNDO'}
    bl_property = 'texture'
    material: bpy.props.StringProperty()
    usage: bpy.props.StringProperty()
    cube: bpy.props.BoolProperty(default=False)
    texture: bpy.props.EnumProperty(items=_pick_items)

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        mat = _mat_named(self.material)
        if mat is None:
            return {'CANCELLED'}
        slot = next((t for t in mat.c2.textures if t.usage == self.usage), None)
        if slot is None:
            slot = mat.c2.textures.add()
            slot.usage, slot.extra = self.usage, True
        slot.texture = int(self.texture)        # the live update rebuilds the material
        return {'FINISHED'}


class CARS2TK_OT_load_texture(bpy.types.Operator):
    """Use an image file (.dds kept as is, others written as uncompressed DDS) in this texture slot. A file named
    like one of the character's textures replaces that texture everywhere it is used; any other name is added as
    a new texture"""
    # public 2.0.5: the toolkit's own Load Image for character materials. The material system's c2.load_texture
    # comes from whichever copy runs: with an older Scene Editor enabled (0.29.6) a file named like an existing
    # texture was ADDED a second time (two TexturePool entries of one name in the export) instead of replacing it,
    # and an image edited on disk kept its old pixels. Same behaviour now with or without the Scene Editor.
    bl_idname = 'cars2tk.load_texture'
    bl_label = 'Load Image As Texture'
    bl_options = {'REGISTER', 'UNDO'}
    material: bpy.props.StringProperty()
    usage: bpy.props.StringProperty()
    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default='*.dds;*.png;*.tga;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff;*.exr',
                                          options={'HIDDEN'})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        from .look import matio as tk_matio        # the toolkit's own helpers (plain functions)
        L = api()
        if L is None:
            return {'CANCELLED'}
        fx = L.fxnodes
        scene = context.scene
        mat = _mat_named(self.material) if self.material else (
            context.active_object.active_material if context.active_object else None)
        if mat is not None and getattr(mat.c2, 'variant_of', None) is not None:
            mat = mat.c2.variant_of
        if mat is None or not self.filepath:
            return {'CANCELLED'}
        img = _image_by_path(self.filepath)
        try:
            if img is not None:
                img.reload()                    # edited and saved over the same file: show the new pixels
            else:
                img = bpy.data.images.load(self.filepath, check_existing=True)
            img.size[0]
        except RuntimeError as e:
            self.report({'ERROR'}, 'Cannot read %s: %s' % (self.filepath, e))
            return {'CANCELLED'}
        try:
            if img.preview is not None:
                img.preview.reload()
        except (AttributeError, RuntimeError):
            pass
        table = fx.texture_table(scene, mat)
        if table is None:
            self.report({'ERROR'}, "A choreography preview's material: textures can't be added to it")
            return {'CANCELLED'}
        owner = mat.c2.owner
        name = bpy.path.clean_name(os.path.splitext(os.path.basename(self.filepath))[0])
        same = sorted((x for x in table if x.name.lower() == name.lower()), key=lambda x: x.index)
        existing = same[0] if same else None
        if existing is not None:
            t = existing
        else:
            t = table.add()
            t.index = max((x.index for x in table), default=-1) + 1
            t.name = name
            t.ttype = '2d'
            t.source_path = ('characters\\%s\\maps\\%s.png' % (owner.get('c2_stem', owner.name), name) if owner
                             else 'materiallibrary\\custom\\%s.png' % name)
            t.file = self.filepath
        fx.SUSPEND[0] += 1
        try:
            if existing is None:
                t.srgb = not self.usage.lower().startswith(('normalmap', 'lightmap', 'envmap', 'environmentmap',
                                                            'fallbackmap'))
            t.replace = self.filepath
        finally:
            fx.SUSPEND[0] -= 1
        try:
            tk_matio.refresh_info(t)
        except (OSError, RuntimeError):
            pass
        if owner is not None:
            owner['c2tex_version'] = owner.get('c2tex_version', 0) + 1
        for k in list(fx._contexts):
            if k[0] == scene.name:
                del fx._contexts[k]
        if existing is not None:
            if owner is not None and hasattr(fx, 'on_owner_texture_replaced'):
                fx.on_owner_texture_replaced(scene, owner, t.index)
            elif hasattr(fx, 'on_texture_replaced'):
                fx.on_texture_replaced(scene, t.index)
        slot = next((s for s in mat.c2.textures if s.usage == self.usage), None)
        if slot is None:
            slot = mat.c2.textures.add()
            slot.usage, slot.extra = self.usage, True
        slot.texture = t.index
        if existing is not None:
            self.report({'INFO'}, 'Texture %d "%s" replaced with %s (%s; written into its own TexturePool slot '
                                  'on export)' % (t.index, t.name, os.path.basename(self.filepath), t.info))
        else:
            self.report({'INFO'}, 'New texture %d "%s" (%s; added to the .oct on export)' % (t.index, name, t.info))
        for win in context.window_manager.windows:
            for area in win.screen.areas:
                area.tag_redraw()
        return {'FINISHED'}


def _draw_slot_with_image(L, orig, layout, scene, mat, slot, required):
    table = L.fxnodes.texture_table(scene, mat)
    tex = next((t for t in table if t.index == slot.texture), None) if table is not None else None
    if tex is None:             # a preview package's texture: the material system's own row
        return orig(layout, scene, mat, slot, required)
    return _draw_tex_slot(L, layout, scene, mat, slot, tex, required)


_SLOT_ERRORS = set()


def _draw_slot(layout, scene, mat, slot, required=False):
    """public 2.0.4: a slot that fails to draw (thumbnail / hook trouble) never blanks the rest of the panel:
    it is drawn plainly instead, and the error goes to the console once."""
    try:
        return _draw_slot_inner(layout, scene, mat, slot, required)
    except Exception as e:
        key = (mat.name, slot.usage, str(e))
        if key not in _SLOT_ERRORS:
            _SLOT_ERRORS.add(key)
            import traceback
            print('Cars 2 Toolkit: texture slot %s of %s drawn plainly:' % (slot.usage, mat.name))
            traceback.print_exc()
        orig = _ORIG_SLOT[0]
        try:
            if orig is not None and orig is not _draw_slot:
                return orig(layout, scene, mat, slot, required)
        except Exception:
            pass
        api_ = api()
        name = api_.lookui.nice_name(slot.usage) if api_ is not None else slot.usage
        layout.label(text='%s (could not draw: %s)' % (name, e), icon='ERROR')


def _draw_slot_inner(layout, scene, mat, slot, required=False):
    """lookui.draw_texture_slot of whichever copy runs the material system (Scene Editor / World Importer /
    toolkit - not edited): a slot with a texture is drawn by it unchanged; an EMPTY slot is drawn here with
    its X as a button that removes the slot; removed slots are skipped."""
    L = api()
    orig = _ORIG_SLOT[0]
    if slot.usage in removed_slots(mat):
        return
    try:
        fx = L.fxnodes
        table = fx.texture_table(scene, mat)
        has = table is not None and any(t.index == slot.texture for t in table)
        if table is None and slot.texture >= 0:
            game = fx.material_context(scene, mat)
            has = bool(game is not None and game.textures.tex_files.get(slot.texture))
    except Exception:
        has = True
    if has and orig is not None:
        return _draw_slot_with_image(L, orig, layout, scene, mat, slot, required)
    if has or orig is None:
        return orig(layout, scene, mat, slot, required) if orig is not None else None
    row = layout.row()
    row.alert = required
    col = row.column()
    op = col.operator('cars2tk.remove_texture_slot', text='', icon='X', emboss=False)
    op.material, op.usage = mat.name, slot.usage
    col = row.column(align=True)
    col.label(text=L.lookui.nice_name(slot.usage))
    col.label(text='(none) - needs a texture before export' if required else
              "(none) - the game binds its own" if slot.usage in fx.ENGINE_TEXTURES else '(none)',
              icon='ERROR' if required else 'NONE')
    r = col.row(align=True)
    op = r.operator('cars2tk.pick_texture', text='Pick', icon='VIEWZOOM')
    op.material, op.usage = mat.name, slot.usage
    try:
        cube = fx.slot_is_cube(scene, mat, slot.usage)
        op.cube = bool(cube)
    except Exception:
        pass
    op = r.operator('cars2tk.load_texture', text='Load Image', icon='FILEBROWSER')
    op.material, op.usage = mat.name, slot.usage


_ORIG_SLOT = [None]


# v2.0.62: texture slots removed with the X (USER: the X of an empty slot removes it). Kept as a list on the
# material; the slot stays off the panel (also when the effect's slots are added again as extras), is never
# required, and export leaves its TextureEntry out of the MaterialPool entry. Restore brings it back.
REMOVED_SLOTS = 'c2tk_removed_slots'


def removed_slots(mat):
    return set(mat.get(REMOVED_SLOTS) or ())


def _mat_named(name):
    m = bpy.data.materials.get(name)
    if m is not None and getattr(m, 'c2', None) is not None and m.c2.variant_of is not None:
        m = m.c2.variant_of
    return m


def _rebuild(mat):
    L = api()
    try:
        game = L.fxnodes.material_context(bpy.context.scene, mat)
        L.matnodes.build(mat, game.textures.images if game is not None else {}, game)
    except Exception as e:
        print('Cars 2 Toolkit: %s not rebuilt: %s' % (mat.name, e))
    for w in bpy.context.window_manager.windows:
        for a in w.screen.areas:
            a.tag_redraw()


class CARS2TK_OT_remove_texture_slot(bpy.types.Operator):
    """Remove this empty texture slot from the material (it is left out of the export)"""
    bl_idname = 'cars2tk.remove_texture_slot'
    bl_label = 'Remove Texture Slot'
    bl_options = {'REGISTER', 'UNDO'}
    material: bpy.props.StringProperty()
    usage: bpy.props.StringProperty()

    def execute(self, context):
        mat = _mat_named(self.material)
        if mat is None:
            return {'CANCELLED'}
        c = mat.c2
        for i in reversed(range(len(c.textures))):
            if c.textures[i].usage == self.usage:
                c.textures.remove(i)
        mat[REMOVED_SLOTS] = sorted(removed_slots(mat) | {self.usage})
        _rebuild(mat)
        return {'FINISHED'}


class CARS2TK_OT_restore_texture_slot(bpy.types.Operator):
    """Put this texture slot back on the material, as the character's file has it"""
    bl_idname = 'cars2tk.restore_texture_slot'
    bl_label = 'Restore Texture Slot'
    bl_options = {'REGISTER', 'UNDO'}
    material: bpy.props.StringProperty()
    usage: bpy.props.StringProperty()

    def execute(self, context):
        mat = _mat_named(self.material)
        if mat is None:
            return {'CANCELLED'}
        mat[REMOVED_SLOTS] = sorted(removed_slots(mat) - {self.usage})
        c = mat.c2
        t = next((t for t in c.textures if t.usage == self.usage), None)
        if t is None:
            t = c.textures.add()
            t.usage, t.texture, t.extra = self.usage, -1, True
        orig = _original_entry(mat, self.usage)
        if orig is not None:                   # the file's own slot: its reference, not an extra
            t.texture, t.extra = orig, False
        _rebuild(mat)
        return {'FINISHED'}


def _original_entry(mat, usage):
    owner = getattr(mat.c2, 'owner', None)
    src = owner.get('c2_source') if owner is not None else None
    if not src or not os.path.isfile(src) or mat.c2.index < 0:
        return None
    try:
        import c2oct
        if src not in _SRC_CACHE:
            _SRC_CACHE[src] = c2oct.load(src)
        mats = _pool(_SRC_CACHE[src].get('MaterialPool'))
        md = mats[mat.c2.index] if mat.c2.index < len(mats) else {}
        for te in (md.get('TextureEntries') or {}).values():
            if isinstance(te, dict) and te.get('Usage') == usage:
                return int(te.get('TextureReference', -1))
    except Exception as e:
        print('Cars 2 Toolkit: %s of %s not read: %s' % (usage, mat.name, e))
    return None


def _drop_removed(m, mat):
    """Export: leave the removed slots' TextureEntries out of MaterialPool entry m."""
    gone = removed_slots(mat)
    tes = m.get('TextureEntries')
    if not gone or not isinstance(tes, dict):
        return 0
    kept = [e for e in tes.values() if not (isinstance(e, dict) and e.get('Usage') in gone)]
    if len(kept) == len(tes):
        return 0
    m['TextureEntries'] = {'TextureEntry#%d' % i: e for i, e in enumerate(kept)}
    return len(tes) - len(kept)


def _draw_light_role(layout, mat):
    """v2.0.25: Emissive Property dropdown (was Light, v2.0.24) - None / Behave as Headlight / Behave as Brakelight."""
    box = layout.box()
    box.prop(mat, 'cars2_light_role', text='Emissive Property', icon='LIGHT_SUN')
    if mat.cars2_light_role != 'NONE' and not any(t.usage == 'emissiveMapA1' for t in mat.c2.textures):
        box.label(text='No glow map (emissiveMapA1) on this effect: nothing lights up', icon='ERROR')
        box.label(text='Pick an effect with a glow map (Add Property) and give it one')


class CARS2TK_OT_set_engine(bpy.types.Operator):
    """Rendered view with the game's own shaders (Cars 2 Game render engine), or with Blender's (Eevee)"""
    bl_idname = 'cars2tk.set_engine'
    bl_label = 'Rendered View'
    bl_options = {'REGISTER', 'UNDO'}
    game: bpy.props.BoolProperty()

    def execute(self, context):
        L = api()
        if L is None:
            return {'CANCELLED'}
        if self.game:
            context.scene.render.engine = L.gamerender.ENGINE_ID
            L.fxnodes.sync_engine(context.scene)
        else:
            context.scene.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in \
                bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items.keys() else 'BLENDER_EEVEE'
        return {'FINISHED'}


GAME_SHADERS_OFF = 'c2tk_game_shaders_off'     # scene: the user switched the game shaders off (v2.0.67)


def set_game_shaders(scene, on):
    """Every character material with its own game shader as nodes (on) or the simple Blender material (off).
    -> number of materials rebuilt. v2.0.66/67 USER: Material Preview and Rendered view (Eevee / Cycles) 1:1 with
    the game - the material system's game-shader nodes (fxnodes), whose own operator only covers Scene Editor
    worlds; they show in the 'Game' materials mode."""
    L = api()
    if L is None:
        return 0
    scene.c2.eevee_game_shaders = on
    L.fxnodes._contexts.clear()
    from . import eyelook
    n = 0
    for arm in characters(scene):
        for mat in materials_of(arm):
            if on:
                try:
                    game = L.fxnodes.material_context(scene, mat)
                    meta = L.gamerender.meta_for(game.assets, mat.c2.effect, local=game.local_effects) \
                        if game is not None else None
                    if meta is not None:
                        L.fxnodes.SUSPEND[0] += 1
                        try:
                            L.fxnodes.ensure_effect_entries(mat, meta)
                        finally:
                            L.fxnodes.SUSPEND[0] -= 1
                except Exception as e:
                    print('Cars 2 Toolkit: %s effect entries: %s' % (mat.name, e))
            _rebuild(mat)
            try:
                eyelook.graft(L, scene, mat)
            except Exception as e:
                print('Cars 2 eyes: %s not grafted: %s' % (mat.name, e))
            try:
                solid_display(mat, L, scene)
            except Exception as e:
                print('Cars 2 Toolkit: solid view texture of %s not set: %s' % (mat.name, e))
            n += 1
    scene.c2.viewport_lighting = 'GAME' if on else 'BAKED'
    return n


ENV_NEUTRAL_DONE = 'c2tk_solid_view_207'     # scene: character materials rebuilt with the Solid view display node (2.0.7)


def _upgrade_old_scene():
    """A file saved before v2.0.67: its characters get the game shaders once (unless switched off there). A file
    saved before public 2.0.6: its character materials are rebuilt once (Solid view display node)."""
    try:
        scene = bpy.context.scene
        if scene is not None and hasattr(scene, 'c2') and scene.c2.root is None and not scene.get(ENV_NEUTRAL_DONE) \
                and characters(scene) and (scene.c2.eevee_game_shaders or scene.get(GAME_SHADERS_OFF)):
            scene[ENV_NEUTRAL_DONE] = True
            n = set_game_shaders(scene, bool(scene.c2.eevee_game_shaders))
            print('Cars 2 Toolkit: %d character materials rebuilt (solid view)' % n)
            return None
        if scene is None or not hasattr(scene, 'c2') or scene.c2.root is not None or scene.c2.eevee_game_shaders \
                or scene.get(GAME_SHADERS_OFF) or not characters(scene):
            return None
        n = set_game_shaders(scene, True)
        print('Cars 2 Toolkit: %d character materials now use their game shaders' % n)
    except Exception as e:
        print('Cars 2 Toolkit: game shaders not set up (%s)' % e)
    return None


class CARS2TK_OT_eevee_game_shaders(bpy.types.Operator):
    """Material Preview and Rendered view (Eevee / Cycles) show every character material with its own game shader,
    exactly like the game and Game Look (specular, reflections, eyes ...). Off: simple Blender materials"""
    bl_idname = 'cars2tk.eevee_game_shaders'
    bl_label = 'Game Shaders'
    bl_options = {'REGISTER', 'UNDO'}
    on: bpy.props.BoolProperty()

    def execute(self, context):
        if api() is None:
            return {'CANCELLED'}
        context.scene[GAME_SHADERS_OFF] = not self.on
        n = set_game_shaders(context.scene, self.on)
        self.report({'INFO'}, '%d character material%s rebuilt' % (n, '' if n == 1 else 's'))
        return {'FINISHED'}


class CARS2TK_PT_look(bpy.types.Panel):
    """How characters look: Game Look or Blender materials"""
    bl_label = 'Look'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Cars 2 Character'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return hasattr(context.scene, 'c2')

    def draw(self, context):
        _draw_look(self.layout, context)
        from . import character
        arm = character.active_character(context)
        if arm is not None:
            self.layout.operator('cars2tk.rebuild_materials', icon='FILE_REFRESH')


class CARS2TK_PT_material_sidebar(bpy.types.Panel):
    """The active material's game settings"""
    bl_label = 'Material'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Cars 2 Character'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        mat = _material_of(context)
        return mat is not None and getattr(mat, 'c2', None) is not None and mat.c2.owner is not None

    def draw(self, context):
        o = context.active_object
        if o is not None and o.type == 'MESH' and len(o.material_slots) > 1:
            self.layout.template_list('MATERIAL_UL_matslots', '', o, 'material_slots', o, 'active_material_index',
                                      rows=2)
        _draw_material(self.layout, context, _material_of(context))


class CARS2TK_PT_material(bpy.types.Panel):
    """Material properties: the game material (same panel as the Cars 2 Scene Editor's)"""
    bl_label = 'Cars 2 Material'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'material'

    @classmethod
    def poll(cls, context):
        mat = _material_of(context)
        return mat is not None and getattr(mat, 'c2', None) is not None and bool(mat.c2.effect)

    def draw(self, context):
        _draw_material(self.layout, context, _material_of(context))


_classes = (CARS2TK_OT_rebuild_materials, CARS2TK_OT_set_engine, CARS2TK_OT_eevee_game_shaders,
            CARS2TK_OT_load_texture, CARS2TK_OT_pick_texture,CARS2TK_OT_remove_texture_slot,
            CARS2TK_OT_restore_texture_slot, CARS2TK_PT_look, CARS2TK_PT_material_sidebar,
            CARS2TK_PT_material)
_HANDLERS = (('depsgraph_update_post', _on_depsgraph), ('frame_change_post', _on_frame), ('load_post', _on_file_load),
             ('undo_post', _on_load), ('redo_post', _on_load))     # undo re-creates objects: find them again


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.types.Material.cars2_light_role = bpy.props.EnumProperty(
        name='Emissive Property', items=LIGHT_ITEMS, default='NONE',
        description='Make this material glow with the headlights or the brake lights in game (written into the '
                    'export as a link to the light; None removes the link)')
    bpy.types.Object.cars2_emissions = bpy.props.BoolProperty(
        name='Emissions', default=False, update=_emissions_update,
        description="Show the character's glow maps (emissiveMapA1) lit, as the game does when it switches them on "
                    "(brake lights, headlights ...). Preview only: the materials and exports keep their values")
    bpy.types.Object.cars2_emission_strength = bpy.props.FloatProperty(
        name='Glow Strength', default=1.0, min=0.0, soft_max=10.0, update=_emissions_update,
        description='emissiveMultiplier the preview shows the glow maps at (the game sets its own at run time). '
                    'Preview only')
    for h, f in _HANDLERS:
        getattr(bpy.app.handlers, h).append(f)


def unregister():
    for h, f in _HANDLERS:
        lst = getattr(bpy.app.handlers, h)
        if f in lst:
            lst.remove(f)
    del bpy.types.Object.cars2_emission_strength
    del bpy.types.Object.cars2_emissions
    del bpy.types.Material.cars2_light_role
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
