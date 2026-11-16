"""Cars 3 dynamic chains (Fritter's side chains with their license plates) in Blender.

A Cars 3 model describes each swinging part as a DynamicChain processor in its rig network: a list of
particles with their settings (ConfigFlags, InverseMass, NaturalFrequency, DampingFactor, DragCoefficient,
ConstraintLength, RelativeTarget, bind offset), the chain's GlobalDamping and IterationRate, and optionally a
container box (DynamicChainExtentBox). Particle 0 is fixed to the anchor (chain<Side>_Start, riding on Body);
particle 1 swings - a damped spring pulls it towards its rest place relative to the previous particle, turned
with the anchor, the link length is kept and it stays inside the container box. The proxies that carry the
chain links and the plate ride on the swinging particle's frame; the other links turn to aim at it.

The motion here is this add-on's own solver, built from standard published methods, using the settings stored
in the model:
  * T. Jakobsen, "Advanced Character Physics", GDC 2001: position-based particles, distance constraints
    relaxed iteratively and weighted by inverse mass;
  * M. Mueller et al., "Position Based Dynamics", 2007: velocities taken from the corrected positions;
  * a damped harmonic spring (natural frequency w, damping ratio z) stepped with implicit Euler, the usual
    unconditionally stable choice for stiff springs;
  * quadratic air drag, and the container as a box clamp in the anchor's frame.
It runs over the clip's frames after a clip import and bakes onto a target bone (<chain>_SimTarget). The rig
follows it with constraints:
  <chain>_Swing  (anchor -> particle, Damped Track to the target) carries the riding proxies,
  <link>_Aim     (each aiming link, Damped Track to the target) turns the others.
"""
import json
import math

import bpy
import mathutils
from mathutils import Matrix, Vector

PROP = 'cars2tk_c3_chain_sim'          # JSON list of chain specs on the armature
TARGET = '_SimTarget'
SWING = '_Swing'
AIM = '_Aim'
FOLLOW = '_Follow'
FOLLOW_CONSTRAINT = 'Cars2 Follow'

# ConfigFlags bits as seen in the shipped Cars 3 chains: the fixed particle has 1, the swinging one 2;
# bit 8 asks for air drag. (Bit 4, gravity, is ignored: these chains have no gravity input.)
FIXED, SPRUNG, USES_DRAG = 0x1, 0x2, 0x8


def _pool(x):
    if x is None:
        return []
    if isinstance(x, dict):
        return list(x.values())
    return list(x)


def read_specs(data):
    """The model's DynamicChain processors -> [spec] (game space, anchor-local offsets)."""
    sn = next((x for x in _pool(data.get('SubNetworkPool')) if x.get('DataNodePool')), None)
    if sn is None:
        return []
    HS, HI, HL = sn['HeaderStrings'], sn['HeaderStringIndices'], sn['HeaderLayout']
    dn = _pool(sn['DataNodePool'])
    n_h = len(HI)
    parent_h, nxt = [None] * (n_h + 1), 1
    for k in range(0, len(HL) - 1, 2):
        for _c in range(HL[k + 1]):
            if nxt <= n_h:
                parent_h[nxt] = HL[k]
            nxt += 1

    def owner(h):
        """The rig frame a node belongs to: Anchor/<frame>/... (v2.0.55)."""
        out, k = [], 0
        while h and 0 < h <= n_h and k < 32:
            out.append(HS[HI[h - 1]])
            h, k = parent_h[h], k + 1
        out.reverse()
        return out[1] if len(out) > 1 and out[0] == 'Anchor' else (out[0] if out else None)
    procs = _pool(sn.get('ProcessorNodePool'))

    def writer(r):
        for p in procs:
            refs = list(zip(p.get('DataNodeAttributes') or [], p.get('DataNodeRefs') or []))
            if any(rr == r and a == 'Output' for a, rr in refs):
                return p.get('Type'), dict(refs)
        return None, {}

    def vec(v, d=(0.0, 0.0, 0.0)):
        return [float(x) for x in (v if isinstance(v, (list, tuple)) and len(v) >= 3 else d)[:3]]
    out = []
    for p in procs:
        if p.get('Type') != 'DynamicChain' or 'DataNodeRefs' not in p:
            continue
        refs = dict(zip(p.get('DataNodeAttributes') or [], p['DataNodeRefs']))
        if 'AnchorToWorld' not in refs:
            continue
        anchor_ref = refs['AnchorToWorld']
        anchor = owner(dn[anchor_ref].get('Header', 0))
        cd = dn[refs['ChainData']] if 'ChainData' in refs else {}
        parts = []
        k = 0
        while 'Particle[%d]' % k in refs:
            d = dn[refs['Particle[%d]' % k]]
            bind = d.get('BindTransform#Dilation') or {}
            attach = None
            cp = refs.get('ConstraintPoint[%d]' % k)
            if cp is not None:                       # TransformPositionWithDilation(local point, anchor)
                t, wr = writer(cp)
                if t == 'TransformPositionWithDilation' and wr.get('Converter') == anchor_ref and 'Input' in wr:
                    attach = vec(dn[wr['Input']].get('Data'))
            parts.append({'flags': int(d.get('ConfigFlags', 0)), 'inv_mass': float(d.get('InverseMass', 1.0)),
                          'damping': float(d.get('DampingFactor', 0.0)),
                          'frequency': float(d.get('NaturalFrequency', 0.0)),
                          'drag': float(d.get('DragCoefficient', 0.0)),
                          'length': float(d.get('ConstraintLength', 0.0)),
                          'relative_target': vec(d.get('RelativeTarget')),
                          'bind': vec(bind.get('Translation')), 'attach': attach})
            k += 1
        container = None
        if 'Container' in refs:                      # TransformDilationWithDilation(offset, (local, anchor))
            t, wr = writer(refs['Container'])
            if t == 'TransformDilationWithDilation' and 'Input' in wr and 'Converter' in wr:
                t2, wr2 = writer(wr['Converter'])
                if wr2.get('Input[1]') == anchor_ref:
                    off = (dn[wr['Input']].get('Data') or {}).get('Translation')
                    container = {'offset': vec(off), 'extent': vec(cd.get('ContainerExtent'))}
        if len(parts) < 2 or not anchor:
            continue
        pos_refs = [refs['Position[%d]' % i] for i in range(len(parts)) if 'Position[%d]' % i in refs]
        out.append({'anchor': anchor, 'particles': parts, 'container': container, '_pos': pos_refs,
                    'global_damping': float(cd.get('GlobalDamping', 1.0)),
                    'iteration_rate': float(cd.get('IterationRate', 300.0)),
                    'gravity': 'Gravity' in refs})
    _trace_roles(out, dn, procs, owner)
    return out


def _trace_roles(specs, dn, procs, owner):
    """v2.0.54: which rig nodes each chain drives, traced through the network (Arvy's PanelEnd /
    MoldingLeftEnd / MufflerEnd sit on particle 0 and aim at particle 1). A bone reached without an aim on
    the way rides on the swing; reached only through aims, it turns towards it."""
    def outs(p, refs):
        t = str(p.get('Type'))
        if t.endswith('Decompose'):
            return {a for a, _ in refs if a != 'Aggregate'}
        if t.endswith('Compose'):
            return {'Aggregate'}
        return {'Output'}
    writer = {}
    for p in procs:
        refs = list(zip(p.get('DataNodeAttributes') or [], p.get('DataNodeRefs') or []))
        o = outs(p, refs)
        for a, r in refs:
            if a in o:
                writer[r] = (str(p.get('Type')), [rr for aa, rr in refs if aa not in o])
    chain_of = {}
    for k, s in enumerate(specs):
        for r in s.pop('_pos', []):
            chain_of[r] = k
    memo = {}

    def reach(r, depth=0, stack=()):
        """{chain index: True if reached with no aim on the way, else False}."""
        if r in chain_of:
            return {chain_of[r]: True}
        if r in memo:
            return memo[r]
        if r in stack or depth > 24:
            return {}
        t, ins = writer.get(r, ('', []))
        res = {}
        for rr in ins:
            for k, direct in reach(rr, depth + 1, stack + (r,)).items():
                # an aim, or a DilationOrientationConnection (passes on the orientation only - Fritter's chain
                # links keep their place and take chainLeft_Start's turn), means 'turned', not 'placed'
                res[k] = res.get(k, False) or (direct and 'Aim' not in t and t != 'DilationOrientationConnection')
        memo[r] = res
        return res
    nodes = {}
    for i, d in enumerate(dn):
        if d.get('Type') in ('Transform', 'Dilation', 'Matrix4x4'):
            nodes.setdefault(owner(d.get('Header', 0)), []).append(i)
    for s in specs:
        s['traced_riders'], s['traced_followers'] = [], []
    for nm, rs in nodes.items():
        if not nm or ' ' in nm:
            continue
        hit = {}
        for r in rs:
            for k, direct in reach(r).items():
                hit[k] = hit.get(k, False) or direct
        for k, direct in hit.items():
            if nm != specs[k]['anchor']:
                specs[k]['traced_riders' if direct else 'traced_followers'].append(nm)


def setup(context, arm, data, rig):
    """Build the swing rig for every chain; returns the number of chains."""
    import c3toc2
    from . import cars3
    specs = read_specs(data)
    if not specs:
        return 0
    cars3.add_chain_anchors(context, arm, rig, [{'anchor': s['anchor']} for s in specs])
    specs = [s for s in specs if s['anchor'] in arm.data.bones]
    if not specs:
        return 0
    for s in specs:
        s['name'] = s['anchor']
        bones = arm.data.bones
        s['riders'], s['followers'] = [], []
        s['riders'] = [b for b in s.pop('traced_riders', []) if b in bones and bones[b].use_deform]
        s['followers'] = [b for b in s.pop('traced_followers', []) if b in bones and bones[b].use_deform
                          and b not in s['riders']]
    prev = context.view_layer.objects.active
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.data.edit_bones
    internals = arm.data.collections.get('Rig Internals') if hasattr(arm.data, 'collections') else None

    def helper(name, head, tail, roll, parent):
        h = eb.get(name) or eb.new(name)
        h.head, h.tail, h.roll = head, tail, roll
        h.parent = eb[parent] if parent else None
        h.use_deform = False
        if internals is not None:
            internals.assign(h)
        return h
    for s in specs:
        am = eb[s['anchor']].matrix.copy()
        p0, p1 = rest_points(am, s)
        s['rest'] = [list(p0), list(p1)]
        helper(s['name'] + TARGET, p1, p1 + Vector((0.0, 0.05, 0.0)), 0.0, None)
        sw = helper(s['name'] + SWING, p0, p1, 0.0, s['anchor'])
        for b in s['riders']:
            src = eb[b]
            helper(b + FOLLOW, src.head.copy(), src.tail.copy(), src.roll, sw.name)
        for b in s['followers']:
            src = eb[b]
            helper(b + FOLLOW, src.head.copy(), src.tail.copy(), src.roll, sw.name)
    bpy.ops.object.mode_set(mode='OBJECT')
    pbs = arm.pose.bones
    for s in specs:
        k = pbs[s['name'] + SWING].constraints.new('DAMPED_TRACK')
        k.target, k.subtarget, k.track_axis = arm, s['name'] + TARGET, 'TRACK_Y'
        for b in s['riders'] + s['followers']:
            for c in [c for c in pbs[b].constraints if c.name == FOLLOW_CONSTRAINT]:
                pbs[b].constraints.remove(c)
            k = pbs[b].constraints.new('COPY_TRANSFORMS' if b in s['riders'] else 'COPY_ROTATION')
            k.name = FOLLOW_CONSTRAINT
            k.target, k.subtarget = arm, b + FOLLOW
    arm[PROP] = json.dumps(specs)
    context.view_layer.objects.active = prev
    return len(specs)


def rest_points(am, s):
    """Particle 0 (attach point) and particle 1 (bind chain) at the anchor matrix am."""
    pa = s['particles']
    p0 = am @ Vector(pa[0]['attach'] or pa[0]['bind'])
    p1 = p0 + am.to_3x3() @ Vector(pa[1]['bind'])
    return p0, p1


def _is_fixed(p):
    return p['flags'] & 3 == FIXED


class ChainSim:
    """Position-based simulation of one chain (see the module notes for the methods used)."""

    def __init__(self, spec):
        self.spec = spec
        self.parts = spec['particles']
        n = len(self.parts)
        self.pos = [Vector() for _ in range(n)]
        self.vel = [Vector() for _ in range(n)]
        self.goal = [None] * n                   # each sprung particle's goal on the previous step

    def _fixed_point(self, am, p):
        return am @ Vector(p['attach'] or p['bind'])

    def start(self, am):
        """Put the chain at rest at anchor matrix `am` (the first frame of the clip)."""
        place = am.to_translation()
        turn = am.to_3x3()
        for i, p in enumerate(self.parts):
            place = self._fixed_point(am, p) if _is_fixed(p) else place + turn @ Vector(p['bind'])
            self.pos[i] = place.copy()
            self.vel[i] = Vector()
            self.goal[i] = None
        self._satisfy_links(am, 3)

    def advance(self, am, dt):
        """One time step of dt seconds with the anchor now at `am`."""
        if dt <= 0.0:
            return
        before = [q.copy() for q in self.pos]
        turn = am.to_3x3()
        for i, p in enumerate(self.parts):
            if _is_fixed(p):
                self.pos[i] = self._fixed_point(am, p)
                continue
            v = self.vel[i]
            accel = Vector()
            if p['flags'] & USES_DRAG and p['drag'] > 0.0:
                accel -= p['drag'] * v.length * v * p['inv_mass']          # quadratic drag
            if p['flags'] & SPRUNG and i > 0:
                goal = self.pos[i - 1] + turn @ Vector(p['relative_target'])
                last = self.goal[i] if self.goal[i] is not None else goal
                goal_vel = (goal - last) / dt
                w, z = p['frequency'], p['damping']
                # implicit Euler for x'' = w^2 (goal - x) + 2 z w (goal_vel - x') + accel
                k, c = w * w, 2.0 * z * w
                v = (v + dt * (k * (goal - self.pos[i]) + c * goal_vel + accel)) / (1.0 + dt * c + dt * dt * k)
                self.goal[i] = goal
            else:
                v = v + dt * accel
            self.pos[i] = self.pos[i] + dt * v
        self._satisfy_links(am, max(1, int(round(self.spec['iteration_rate'] * dt))))
        keep = self.spec['global_damping']
        for i in range(len(self.parts)):
            self.vel[i] = (self.pos[i] - before[i]) * (keep / dt)

    def _satisfy_links(self, am, rounds):
        """Relax the link lengths (inverse-mass weighted), then keep particles in the container box."""
        scale = am.to_scale()[0]
        box = self.spec.get('container')
        frame = am @ Matrix.Translation(Vector(box['offset'])) if box else None
        to_box = frame.inverted() if box else None
        for _ in range(rounds):
            for i in range(1, len(self.parts)):
                pa, pb = self.parts[i - 1], self.parts[i]
                wa = 0.0 if _is_fixed(pa) else pa['inv_mass']
                wb = 0.0 if _is_fixed(pb) else pb['inv_mass']
                if wa + wb <= 0.0:
                    continue
                d = self.pos[i] - self.pos[i - 1]
                dist = d.length
                if dist < 1e-9:
                    continue
                corr = d * ((dist - pb['length'] * scale) / (dist * (wa + wb)))
                self.pos[i - 1] += wa * corr
                self.pos[i] -= wb * corr
            if box:
                ext = box['extent']
                for i, p in enumerate(self.parts):
                    if not _is_fixed(p):
                        local = to_box @ self.pos[i]
                        self.pos[i] = frame @ Vector([min(max(local[k], -ext[k]), ext[k]) for k in range(3)])


def bake(context, arm):
    """Run the chains over the scene's frame range and key their targets. Returns a short note."""
    raw = arm.get(PROP)
    if not raw:
        return ''
    specs = json.loads(raw)
    scene = context.scene
    f0, f1 = scene.frame_start, scene.frame_end
    dt = scene.render.fps_base / float(scene.render.fps or 30)
    if arm.animation_data is None:
        arm.animation_data_create()
    if arm.animation_data.action is None:
        arm.animation_data.action = bpy.data.actions.new(arm.name + '_chains')
    act = arm.animation_data.action
    pbs = arm.pose.bones
    targets = [pbs[s['name'] + TARGET] for s in specs]
    _clear_keys(arm, act, [t.name for t in targets])
    for t in targets:
        t.location = (0.0, 0.0, 0.0)
    sims = [ChainSim(s) for s in specs]
    keep = scene.frame_current
    for f in range(f0, f1 + 1):
        scene.frame_set(f)
        for s, sim, t in zip(specs, sims, targets):
            am = pbs[s['anchor']].matrix.copy()
            if f == f0:
                sim.start(am)
            else:
                sim.advance(am, dt)
            t.location = sim.pos[-1] - Vector(s['rest'][1])
            t.keyframe_insert('location', frame=f, group=t.name)
    scene.frame_set(keep)
    return '  [Cars 3 chains simulated: %s]' % ', '.join(s['name'] for s in specs)


def _clear_keys(arm, act, bones):
    paths = {'pose.bones["%s"].location' % b for b in bones}
    try:
        from .anim import action_curves, action_container
        cont = action_container(act, arm)
        for fc in [fc for fc in action_curves(act) if fc.data_path in paths]:
            cont.fcurves.remove(fc)
    except Exception:
        for fc in [fc for fc in getattr(act, 'fcurves', []) if fc.data_path in paths]:
            act.fcurves.remove(fc)
