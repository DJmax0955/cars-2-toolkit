"""Characters in a Cars 2 Scene Editor map (v1.97; v2.0: lighting only).

A character's materials are game materials of the shared material system
(charmat.py), so the Scene Editor's Cars 2 Game renderer draws them with their
effects' own shaders and the map's sun, light rig, shadows, fog and exposure
by itself. What this adds is the light the game gives a car where it is
(sdaemon.exe; see memory cars2-light-probes):

* objectSH = the Volume light probes around the car (inside: the probe's
  DirectSH grid, trilinear corner to corner; fade margin: blended with the
  default rig; several probes: averaged), else the PrimarySceneRig. The Scene
  Editor keeps objectSH in its one engine block, so a small hook gives the
  character's draws their own copy of the block with the character's objectSH.
* The reflection cube (envMapInstance) = the first Volume probe the car is
  inside that has a cube, else the scene probe: gamerender.ENV_OVERRIDE, which
  the renderer samples from the world's textures. The material keeps its own
  slot (export is unchanged).

Nothing here writes into the world, the character's materials or its source
files; the Scene Editor's exporter skips the character.
"""
import math

import bpy
import numpy as np

PROP_INFO = 'cars2_se_light'      # on the armature: which probes light it now (shown in the panel)
PROP_INFO_ENV = 'cars2_se_refl'
_state = {'busy': False, 'hooked': False, 'sh': {}, 'probes': (None, [])}


# ------------------------------------------------------------------------------------------------ Scene Editor
def se():
    """The running material system's modules when the Scene Editor's copy runs, else None."""
    from .look import looklink
    o = looklink.owner()
    if o is None or o['name'] != 'scene_editor':
        return None
    return looklink.modules()


def world(scene):
    """(SE modules, GameContext) of the scene's Arcade world, or None."""
    m = se()
    if m is None or scene is None or not hasattr(scene, 'c2') or scene.c2.root is None:
        return None
    try:
        ctx = m.fxnodes.game_context(scene)
    except Exception:
        return None
    return (m, ctx) if ctx is not None else None


def active(scene=None):
    sc = scene or bpy.context.scene
    return world(sc) is not None


def characters(scene):
    from . import charmat
    return charmat.characters(scene)


def meshes_of(arm):
    from . import charmat
    return charmat.meshes_of(arm)


def _probes(scene):
    """The map's light probes (found once per object count)."""
    key = (scene.name, len(bpy.data.objects))
    if _state['probes'][0] != key:
        _state['probes'] = (key, [o for o in scene.objects if getattr(o, 'c2', None) is not None and
                                  o.c2.kind == 'PROBE'])
    return _state['probes'][1]


# ------------------------------------------------------------------------------------------------ light probes
def _game_pos(scene, arm):
    """Where the game asks for the car's lighting: its body centre, in the probes' object space (Blender)."""
    from mathutils import Vector
    ms = meshes_of(arm)
    pts = [o.matrix_world @ Vector(c) for o in ms for c in o.bound_box]
    if not pts:
        return arm.matrix_world.translation.copy()
    return sum(pts, Vector()) / len(pts)


def _probe_state(o, p):
    """(state 0 out / 1 fade margin / 2 inside, weight, unit-cube position) of a Volume probe for world point p."""
    mw = o.matrix_world
    q = mw.inverted_safe() @ p
    c = o.c2
    ext = [max(mw.col[i].xyz.length, 1e-6) for i in range(3)]
    if c.probe_fade == 'Inside' and c.probe_has_inside:
        inner = max(min(float(c.probe_inside), 0.999), 0.0)
        r = max(abs(q[i]) for i in range(3))
        if r <= inner:
            return 2, 1.0, q
        if r <= 1.0:
            return 1, (1.0 - r) / (1.0 - inner), q
        return 0, 0.0, q
    out = [max(0.0, abs(q[i]) - 1.0) * ext[i] for i in range(3)]
    if max(out) <= 0.0:
        return 2, 1.0, q
    margin = list(c.probe_outside) if any(c.probe_outside) else [20.0, 20.0, 20.0]
    f = max(out[i] / max(margin[i], 1e-6) for i in range(3))
    if f < 1.0:
        return 1, 1.0 - f, q
    return 0, 0.0, q


def _grid_sh(o, q):
    """27 SH floats of a Volume probe's DirectSH grid at unit-cube position q (trilinear, grid corner to corner)."""
    sh = np.asarray(o.get('c2_direct_sh', []), np.float64)
    d = [max(1, int(round(x))) for x in o.c2.probe_density]
    n = d[0] * d[1] * d[2]
    if sh.size < n * 27:
        return None
    sh = sh[:n * 27].reshape(d[2], d[1], d[0], 27)        # cells x fastest
    idx, frac = [], []
    for a in range(3):
        if d[a] == 1:
            idx.append((0, 0)); frac.append(0.0)
            continue
        t = (min(max(q[a], -1.0), 1.0) + 1.0) * 0.5 * (d[a] - 1)
        i0 = min(int(math.floor(t)), d[a] - 2)
        idx.append((i0, i0 + 1)); frac.append(t - i0)
    out = np.zeros(27)
    for bx in (0, 1):
        for by in (0, 1):
            for bz in (0, 1):
                w = (frac[0] if bx else 1 - frac[0]) * (frac[1] if by else 1 - frac[1]) * \
                    (frac[2] if bz else 1 - frac[2])
                if w:
                    out += w * sh[idx[2][bz], idx[1][by], idx[0][bx]]
    return out


def _rig_sh(scene):
    rigs = scene.c2.light_rigs
    rig = next((r for r in rigs if r.type_name == 'PrimarySceneRig'), rigs[0] if len(rigs) else None)
    return np.asarray(list(rig.sh), np.float64) if rig is not None else None


def car_light(scene, arm):
    """(27 SH floats or None, TexturePool index of the reflection cube or -1) for a character, as the game picks them."""
    sh, env, _info = car_light_info(scene, arm)
    return sh, env


def car_light_info(scene, arm):
    """car_light + (light text, reflection text) for the panel."""
    p = _game_pos(scene, arm)
    rig = _rig_sh(scene)
    probes = _probes(scene)
    acc, count, env, used, env_name = None, 0, -1, [], ''
    for o in probes:
        if o.c2.probe_type != 'Volume':
            continue
        st, w, q = _probe_state(o, p)
        if st == 0:
            continue
        if st == 2 and env < 0 and o.c2.probe_texture >= 0:
            env = int(o.c2.probe_texture)
            env_name = o.c2.probe_name or o.name
        g = _grid_sh(o, q)
        if g is None:
            continue
        if st == 1 and rig is not None:
            g = w * g + (1.0 - w) * rig
        acc = g if acc is None else acc + g
        count += 1
        used.append('%s%s' % (o.c2.probe_name or o.name, '' if st == 2 else ' (edge %d%%)' % round(w * 100)))
    sh = acc / count if count else rig
    if env < 0:
        sp = next((o for o in probes if o.c2.probe_scene and o.c2.probe_texture >= 0), None)
        if sp is not None:
            env, env_name = int(sp.c2.probe_texture), '%s (scene probe)' % (sp.c2.probe_name or sp.name)
    light = ', '.join(used) if used else 'map light rig (no volume probe here)'
    return sh, env, (light, env_name or 'none')

def update_light(scene, arm, force=False):
    """The car's probe light + reflection cube for where it is now."""
    w = world(scene)
    if w is None:
        return
    m, _ctx = w
    sh, env, info = car_light_info(scene, arm)
    if arm.get(PROP_INFO) != info[0]:
        arm[PROP_INFO] = info[0]
    if arm.get(PROP_INFO_ENV) != info[1]:
        arm[PROP_INFO_ENV] = info[1]
    rows = m.fxnodes.sh_constants(list(sh)) if sh is not None else None
    changed = False
    if rows is not None:
        flat = [float(x) for r in rows for x in r]
        for o in meshes_of(arm):
            if force or _state['sh'].get(o.name) != flat:
                _state['sh'][o.name] = flat
                changed = True
    if force or m.gamerender.ENV_OVERRIDE.get(arm.name, -1) != env:
        m.gamerender.ENV_OVERRIDE[arm.name] = env
        from . import charmat
        for mat in charmat.materials_of(arm):
            m.gamerender.touch_material(mat)
        changed = True
    if changed:
        _redraw()


def unlink(scene, arm):
    """No map any more: the character's own light and reflection."""
    m = se()
    for o in meshes_of(arm):
        _state['sh'].pop(o.name, None)
    if m is not None and m.gamerender.ENV_OVERRIDE.pop(arm.name, None) is not None:
        from . import charmat
        for mat in charmat.materials_of(arm):
            m.gamerender.touch_material(mat)
    for k in (PROP_INFO, PROP_INFO_ENV):
        if k in arm:
            del arm[k]


def _redraw():
    wm = bpy.context.window_manager
    for win in (wm.windows if wm else []):
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ------------------------------------------------------------------------------------------------ renderer hook
def _hook():
    """Per-character objectSH in the Cars 2 Game renderer: its draws get a copy of the engine block whose
    objectSH rows are the character's (the game sets objectSH per object; the Scene Editor keeps one)."""
    m = se()
    if m is None or _state['hooked']:
        return
    R = m.gamerender.Renderer
    if not (hasattr(R, 'engine_block') and hasattr(R, '_run_cmds')) or hasattr(R, '_cars2tk_orig'):
        _state['hooked'] = hasattr(R, '_cars2tk_orig')
        return
    orig_eb, orig_run = R.engine_block, R._run_cmds
    layout = m.gamerender.LAYOUT
    snaps, ubos = {}, {}

    def engine_block(self, *a, **kw):
        ubo = orig_eb(self, *a, **kw)
        try:
            snaps[id(ubo)] = np.array(self.engine_data, np.float32, copy=True)
        except Exception:
            pass
        return ubo

    def run_cmds(self, cmds, eng, *a, **kw):
        sh = _state['sh']
        base = snaps.get(id(eng))
        if not sh or base is None:
            return orig_run(self, cmds, eng, *a, **kw)
        try:
            import gpu
            regs = [r for n, r in layout.regions() if n == 'objectSH']
            runs, cur, cur_key = [], [], None
            for c in cmds:
                nm = c.key[0].split('|')[0] if getattr(c, 'key', None) else None
                key = nm if nm in sh else None
                if cur and key != cur_key:
                    runs.append((cur_key, cur))
                    cur = []
                cur_key = key
                cur.append(c)
            if cur:
                runs.append((cur_key, cur))
            if not regs or all(k is None for k, _p in runs):
                return orig_run(self, cmds, eng, *a, **kw)
            for key, part in runs:
                if key is None:
                    orig_run(self, part, eng, *a, **kw)
                    continue
                d = base.copy()
                rows = np.asarray(sh[key], np.float32).reshape(7, 4)
                for b, count, _cls in regs:
                    d[b:b + min(count, 7)] = rows[:min(count, 7)]
                buf = gpu.types.Buffer('FLOAT', d.size, np.ascontiguousarray(d).ravel())
                u = ubos.get((id(eng), key))
                if u is None:
                    u = ubos[(id(eng), key)] = gpu.types.GPUUniformBuf(buf)
                else:
                    u.update(buf)
                orig_run(self, part, u, *a, **kw)
        except Exception as e:      # never break the Scene Editor's frame
            print('Cars 2 Toolkit: per-character light skipped (%s)' % e)
            return orig_run(self, cmds, eng, *a, **kw)

    R.engine_block, R._run_cmds = engine_block, run_cmds
    R._cars2tk_orig = (orig_eb, orig_run)
    _state['hooked'] = True


def _unhook():
    m = se()
    if m is None:
        return
    R = m.gamerender.Renderer
    if hasattr(R, '_cars2tk_orig'):
        R.engine_block, R._run_cmds = R._cars2tk_orig
        del R._cars2tk_orig
    _state['hooked'] = False


# ------------------------------------------------------------------------------------------------ keeping in step
def sync(scene, moved=None):
    """Follow moves (probe light). moved: character names, or None = all."""
    if _state['busy'] or scene is None:
        return
    _state['busy'] = True
    try:
        if not active(scene):
            for arm in characters(scene):
                if PROP_INFO in arm:
                    unlink(scene, arm)
            return
        _hook()
        for arm in characters(scene):
            if moved is None or arm.name in moved or PROP_INFO not in arm:
                update_light(scene, arm)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print('Cars 2 Toolkit: Scene Editor light: %s' % e)
    finally:
        _state['busy'] = False


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph):
    if _state['busy'] or se() is None or not hasattr(scene, 'c2'):
        return
    moved, probes = set(), False
    for u in depsgraph.updates:
        idb = u.id
        if not isinstance(idb, bpy.types.Object) or not u.is_updated_transform:
            continue
        o = idb.original
        if o.type == 'ARMATURE' and o.get('c2_source'):
            moved.add(o.name)
        elif o.type == 'MESH' and o.parent is not None and o.parent.get('c2_source'):
            moved.add(o.parent.name)
        elif getattr(o, 'c2', None) is not None and o.c2.kind == 'PROBE':
            probes = True
    if probes:
        sync(scene)
    elif moved:
        sync(scene, moved)


@bpy.app.handlers.persistent
def _on_frame(scene, depsgraph=None):
    if se() is None or not hasattr(scene, 'c2') or scene.c2.root is None:
        return
    sync(scene)      # a new frame: where the cars are


@bpy.app.handlers.persistent
def _on_load(*_a):
    _state['sh'].clear()
    _state['probes'] = (None, [])

    def later():
        sync(bpy.context.scene)
        return None
    bpy.app.timers.register(later, first_interval=0.5)


@bpy.app.handlers.persistent
def _on_undo(*_a):
    _state['probes'] = (None, [])


_HANDLERS = ((bpy.app.handlers.depsgraph_update_post, _on_depsgraph),
             (bpy.app.handlers.frame_change_post, _on_frame),
             (bpy.app.handlers.load_post, _on_load),
             (bpy.app.handlers.undo_post, _on_undo),
             (bpy.app.handlers.redo_post, _on_undo))


def register():
    for h, f in _HANDLERS:
        if f not in h:
            h.append(f)


def unregister():
    for h, f in _HANDLERS:
        if f in h:
            h.remove(f)
    _unhook()
