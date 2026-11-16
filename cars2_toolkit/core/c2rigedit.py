"""Apply a list of recorded rig edits to a character's .oct scene and .bent.

Blender-independent, so it can be tested and scripted on its own. The add-on
records edits as plain dicts (stored on the armature) and calls build() on
export. Edit kinds:

  {"op": "add_node",   "path": "Anchor/Spoiler/Transform", "parent": "<node path>",
                       "translation": [x, y, z], "rotor": [x, y, z, w]}
  {"op": "add_float",  "path": "Anchor/Main_Ctrl/MyDial", "value": 0.0}
  {"op": "channel",    "path": "<node path>", "templates": ["car_body", ...]}
  {"op": "constraint", "owner": "<node path>", "targets": ["<node path>", ...],
                       "ops": [{"op": "orient", "input": 0}, ...]}
  {"op": "driven_key", "driver": "<float path>", "outputs": [["<node path>", "rot_x"]],
                       "points": [[x, y], ...]}
  {"op": "node_transform", "path": "<node path>", "translation": [x, y, z],
                       "rotor": [x, y, z, w] | "llp_rotor": [x, y, z, w], "scale": [x, y, z]}
                       (any subset: a rig node moved/rotated/scaled in Blender,
                       e.g. an FX marker; values are final, in the node's own fields)

Nodes are added in parent-first order with insert_data, so shipping
conventions (parents before children) hold.
"""
from c2oct import FloatList
import os

import c2oct as octio
import c2channels as channels
import c2clipblock as clipblock
from c2scene import SceneNetwork, NONE16
from c2validate import validate_subnetwork, validate_associations

TRANSFORM_TYPES = ('Transform', 'Dilation')


def label_of(path):
    """Animation add-on label for a node path (verified rule: 535/535 labels):
    'Anchor/BackEnd_Ctrl/Transform' -> 'BackEnd'."""
    parts = path.split('/')[1:]
    if parts and parts[-1] == 'Transform':
        parts = parts[:-1]
    if len(parts) == 1 and parts[0].endswith('_Ctrl'):
        parts[0] = parts[0][:-5]
    return '/'.join(parts)


def find_bent(scene_path):
    """The .bent whose SceneFilename names this scene (searches up 3 levels)."""
    stem = os.path.basename(scene_path)
    for suffix in ('.oct.json', '_oct.json', '.json', '.oct'):   # sheriff.oct.json -> sheriff
        if stem.lower().endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    d = os.path.dirname(os.path.abspath(scene_path))
    for _ in range(4):
        for f in sorted(os.listdir(d)):
            if f.endswith('.bent'):
                try:
                    t = octio.load(os.path.join(d, f))
                except Exception:
                    continue
                if os.path.basename(t.get('SceneFilename', '')) == stem:
                    return os.path.join(d, f)
        d = os.path.dirname(d)
    return None


def _insert_position(net, parent):
    """Right after the parent and its existing subtree start - keeps parents first."""
    return net.index(parent) + 1


def _order_new_processors(net, first_new):
    """Pool order is evaluation order. Re-sort the processors the edits added
    so each runs after whatever writes its inputs (stable otherwise). Nothing
    references processors by index, so moving them is safe."""
    from c2scene import _inputs, _outputs
    new = net.procs[first_new:]
    writer = {}
    for k, p in enumerate(new):
        for o in _outputs(p):
            writer[o] = k
    done, order = set(), []

    def visit(k, stack=()):
        if k in done:
            return
        if k in stack:
            raise ValueError('new processors form a cycle: %s' % [new[i]['Header'] for i in stack])
        for d in _inputs(new[k]):
            if d in writer and writer[d] != k:
                visit(writer[d], stack + (k,))
        done.add(k)
        order.append(k)
    for k in range(len(new)):
        visit(k)
    net.procs[first_new:] = [new[k] for k in order]


# Slots tried first when a custom part borrows a clip slot (parts few
# characters have), then any other free transform slot of the template.
BORROW_FIRST = ('TowCraneBase_Ctrl', 'TowCrane_Ctrl', 'TCable_Pulley_Ctrl', 'ControlArmLeft',
                'ControlArmRight', 'ControlHandLeft', 'ControlHandRight', 'SideMirrorMountLeft_Ctrl',
                'SideMirrorMountRight_Ctrl', 'ControlStacheLeft', 'ControlStacheRight')


def _template_slots(net, bent, tm):
    """(link set, [channel name per slot]) of one template - from the .bent
    when it has the interface, else the template catalog."""
    ls = next((x for x in net.links if x['ct'].endswith('/%s.ct' % tm)), None)
    if ls is None:
        return None, []
    names = None
    if bent is not None:
        try:
            names = bent.channels(tm)
        except KeyError:
            names = None
    if names is None:
        e = net._ct.get(ls['ct']) or {}
        names = [sl and sl.get('channel') for sl in e.get('slots', [])]
    return ls, names


def borrow_slots(net, bent, templates, taken):
    """A transform channel name that is free (the scene leaves its slot
    empty and no node has the name) in every one of `templates`.
    -> (channel path, {template: slot}) or (None, {})"""
    free = {}
    for tm in templates:
        ls, names = _template_slots(net, bent, tm)
        if ls is None:
            return None, {}
        free[tm] = {nm: j for j, nm in enumerate(names)
                    if nm and nm.endswith('/Transform') and j < len(ls['nodes']) and ls['nodes'][j] < 0
                    and not net.has(nm) and nm not in taken}
    common = set.intersection(*[set(v) for v in free.values()]) if free else set()
    first = ['Anchor/%s/Transform' % b for b in BORROW_FIRST]
    order = [n for n in first if n in common] + sorted(n for n in common if n not in first)
    if not order:
        return None, {}
    return order[0], {tm: free[tm][order[0]] for tm in templates}


def _driver_of(net, path):
    """What moves a skeleton node: its constraint's first input, else itself."""
    ci = net.constraint_of(path) if net.has(path) else None
    if ci is not None:
        pr = net.procs[ci]
        ins = pr['Input'] if isinstance(pr['Input'], list) else [pr['Input']]
        if ins and ins[0] is not None and ins[0] >= 0:
            return net.path_of(ins[0])
    return path


def _world(net, i):
    import c3toc2
    return c3toc2._rig_world(net, i)


def apply(net, bent, edits):
    """Apply edits in order. Returns a list of human-readable log lines."""
    import c3toc2
    log = []
    first_new = len(net.procs)
    net._borrowed = getattr(net, '_borrowed', {})          # {template: {slot: animated node}}
    net._chains = getattr(net, '_chains', [])
    for e in edits:
        op = e['op']
        if op == 'add_node' and e['path'].split('/')[1].startswith('FX_Marker_'):
            # a new FX marker (a bone FX_Marker_X_Ctrl): a Transform named
            # FX_Marker_X, connected to its clip-template slot when Cars 2
            # has one - that is how the game finds a marker
            nm = e['path'].split('/')[1]
            nm = nm[:-len('_Ctrl')] if nm.endswith('_Ctrl') else nm
            path = 'Anchor/%s/Transform' % nm
            if net.has(path):
                raise ValueError('%s already exists in the rig' % path)
            data = {'Translation': [float(x) for x in e.get('translation', (0, 0, 0))]}
            r = [float(x) for x in e.get('rotor', (0, 0, 0, 1))]
            if any(abs(a - b) > 1e-7 for a, b in zip(r, (0, 0, 0, 1))):
                data['Orientation#Rotor'] = r
            net.insert_data(_insert_position(net, e['parent']), path, 'Transform', data=data, basis=e['parent'])
            hit = []
            for ls in net.links:
                ent = net._ct.get(ls['ct'])
                if ent and path in [x and x['channel'] for x in ent['slots']]:
                    net.link(ls['ct'], path, path)
                    hit.append(ls['ct'].split('/')[-1])
            if hit:
                net.rest[net.index(path)] = (NONE16, NONE16)       # as shipping markers
            log.append('added FX marker %s%s' % (nm, (' (template slot in %s)' % ', '.join(hit)) if hit
                                                else ' (no Cars 2 template slot - nothing in the game uses it)'))
            continue
        if op == 'add_node':
            if net.has(e['path']):
                raise ValueError('%s already exists in the rig' % e['path'])
            data = {}
            t = [float(x) for x in e.get('translation', (0, 0, 0))]
            r = [float(x) for x in e.get('rotor', (0, 0, 0, 1))]
            if any(abs(x) > 1e-7 for x in t):
                data['Translation'] = t
            if any(abs(a - b) > 1e-7 for a, b in zip(r, (0, 0, 0, 1))):
                data['Orientation#Rotor'] = r
            net.insert_data(_insert_position(net, e['parent']), e['path'], 'Dilation',
                            data=data, basis=e['parent'])
            log.append('added node %s under %s' % (e['path'], e['parent']))
        elif op == 'add_float':
            net.add_data(e['path'], 'Float', data=float(e.get('value', 0.0)))
            log.append('added float %s = %g' % (e['path'], e.get('value', 0.0)))
        elif op == 'channel':
            node = net.data[net.index(e['path'])]
            kind = 'float' if node['Type'] == 'Float' else 'transform'
            if kind == 'transform':
                # The game only animates the slots its templates define (a slot
                # appended past them is never animated - tested in-game), so the
                # part borrows an empty slot and is built like the face: the bone
                # stays in the skeleton, a group at its place -> a control named
                # after the borrowed channel, and the bone copies the control
                taken = {n for tb in net._borrowed.values() for n in tb.values()}
                ch_path, slots = borrow_slots(net, bent, e['templates'], taken)
                if ch_path is None:
                    raise ValueError('%s: no free clip slot in %s for it to borrow'
                                     % (label_of(e['path']), ', '.join(e['templates'])))
                xi = net.index(e['path'])
                par = net.data[xi].get('BasisDataNodeRef')
                cp = _driver_of(net, net.path_of(par)) if par is not None else None
                grp = 'Anchor/%s_Clip_Grp/Transform' % label_of(e['path'])
                local = c3toc2._mul(c3toc2._inv4(_world(net, net.index(cp))), _world(net, xi)) if cp else _world(net, xi)
                qx, qy, qz, qw = c3toc2._quat([row[:3] + [0.0] for row in local[:3]] + [[0, 0, 0, 1]])
                gdata = {'Translation': [local[0][3], local[1][3], local[2][3]]}
                if abs(abs(qw) - 1.0) > 1e-7:
                    gdata['Orientation#Rotor'] = [-qx, -qy, -qz, qw]
                net.insert_data(_insert_position(net, cp), grp, 'Dilation', data=gdata, basis=cp)
                net.insert_data(_insert_position(net, grp), ch_path, 'Dilation', data={}, basis=grp)
                for tm, slot in slots.items():
                    ls, _n = _template_slots(net, bent, tm)
                    net.link(ls['ct'], ch_path, ch_path)
                    net._borrowed.setdefault(tm, {})[slot] = e['path']
                net.add_constraint(e['path'], [ch_path], [{'op': 'orient', 'input': 0}, {'op': 'point', 'input': 0}])
                log.append('channel %s: borrows %s (%s), the bone copies it'
                           % (label_of(e['path']), label_of(ch_path),
                              ', '.join('%s slot %d' % (t, sl) for t, sl in slots.items())))
            else:
                if bent is None:
                    raise ValueError('animation channels need the character .bent')
                slots = bent.add_channel(e['path'], kind, e['templates'])
                for tm, s in slots.items():
                    channels.extend_scene(net, 'characters/car/%s.ct' % tm, s, e['path'])
                log.append('channel %s -> %s (NOTE: float channels past the template are not animated '
                           'in-game yet)' % (label_of(e['path']), slots))
        elif op == 'chain':
            # a dynamic chain over rig nodes in a parent-to-child line, root
            # first (Make Dynamic Chain) - built like Mater's tow cable
            log.append(c3toc2.add_dynamic_chain(net, e))
            net._chains.append(e)
        elif op == 'constraint':
            if net.constraint_of(e['owner']) is not None:
                net.set_constraint_ops(e['owner'], e['ops'], e['targets'])
                log.append('changed constraint on %s' % e['owner'])
            else:
                net.add_constraint(e['owner'], e['targets'], e['ops'])
                log.append('added constraint on %s' % e['owner'])
        elif op == 'driven_key':
            pts = [tuple(p) for p in e['points']]
            curve = clipblock.make_curve(pts, linear=e.get('linear'))
            outs = [tuple(o) for o in e['outputs']]
            try:
                net.add_driven_key(e['driver'], outs, curve)
                how = ''
            except ValueError as ex:
                if 'too steep' not in str(ex) or e.get('linear'):
                    raise
                # A smooth curve through few points (a zig-zag) overshoots
                # what 16-bit keys can store; straight segments between the
                # same points always fit.
                net.add_driven_key(e['driver'], outs, clipblock.make_curve(pts, linear=True))
                how = ' (straight segments: the smooth curve was too steep for the game format)'
            log.append('driven key %s -> %s%s' % (e['driver'], [o[0] for o in outs], how))
        elif op == 'node_transform':
            # A rig node moved / rotated (Edit Mode) or scaled (Pose Mode) in
            # Blender, e.g. an FX marker. Values are final, in the node's own
            # fields: translation, rotor (Orientation#Rotor), llp_rotor (the
            # LocalToLocalProxy part of an Orientation#OrientProxy), scale.
            n = net.data[net.index(e['path'])]
            d = n.get('Data') if isinstance(n.get('Data'), dict) else {}
            n['Data'] = d
            what = []
            if 'translation' in e:
                d['Translation'] = FloatList(float(x) for x in e['translation'])
                what.append('position')
            if 'rotor' in e:
                d['Orientation#Rotor'] = FloatList(float(x) for x in e['rotor'])
                what.append('rotation')
            if 'llp_rotor' in e and isinstance(d.get('Orientation#OrientProxy'), dict):
                d['Orientation#OrientProxy']['LocalToLocalProxy#Rotor'] = \
                    FloatList(float(x) for x in e['llp_rotor'])
                what.append('rotation')
            if 'scale' in e:
                d['Scale'] = FloatList(float(x) for x in e['scale'])
                what.append('scale')
            order = ['Translation', 'Orientation#Rotor', 'Orientation#OrientProxy', 'Scale']
            n['Data'] = {k: d[k] for k in sorted(d, key=lambda k: order.index(k) if k in order
                                                 else len(order))}
            log.append('%s: %s' % (e['path'], ', '.join(what) or 'unchanged'))
        else:
            raise ValueError('unknown edit %r' % op)
    _order_new_processors(net, first_new)
    return log


def build(scene_path, bent_path, edits):
    """Load originals, apply edits, validate. Returns (tree, net, bent, log, errors, warnings)."""
    tree = octio.load(scene_path)
    net = SceneNetwork.from_subnetwork(tree['SubNetworkPool']['SubNetwork#0'])
    net._outer = tree
    bent = channels.Bent.load(bent_path) if bent_path else None
    log = apply(net, bent, edits)
    sn = net.build()
    out_tree = dict(tree)
    out_tree['SubNetworkPool'] = {'SubNetwork#0': sn}
    errs, warns = validate_subnetwork(sn)
    e2, w2 = validate_associations(out_tree, sn)
    errs, warns = errs + e2, warns + w2
    import c3toc2                          # v2.0.21: errors the untouched file already has don't block
    stock_e = c3toc2.stock_errors(tree)
    errs, kept = c3toc2.split_stock(errs, stock_e)
    warns += kept
    net._stock_errors = stock_e
    if bent is not None:
        errs += ['.bent: ' + x for x in channels.bent_invariants(bent)]
        errs += ['scene/.bent: ' + x for x in channels.validate_pair(
            SceneNetwork.from_subnetwork(sn), bent)]
    return out_tree, net, bent, log, errs, warns


def channel_labels(bent):
    """{template: {slot: label}} for every slot a .bent names - what the
    animation add-on needs to import/export custom channels."""
    out = {}
    for _, i in bent.interfaces:
        if 'BlenderChannelMap' not in i:
            continue
        tm = i['ConnectionTemplate']
        out[tm] = {s: label_of(n) for s, n in enumerate(bent.channels(tm)) if n}
    return out


def build_with_geometry(scene_path, bent_path, edits, geometry=None, materials=None):
    """build() plus, if geometry is given ({'meshes': [...], 'new_bones': [...]}
    in c2geom.rebuild terms), the character's meshes replaced by it.
    Returns (tree, vbuf or None, ibuf or None, bent, log, errors, warnings)."""
    import c2geom
    tree, net, bent, log, errs, warns = build(scene_path, bent_path, edits)
    vbuf = ibuf = None
    if geometry is not None and not errs:
        tree, vbuf, ibuf, glog = c2geom.rebuild(tree, geometry['meshes'], geometry.get('new_bones', ()))
        log += glog
        import c3toc2
        c3toc2.link_chains_lazy(tree, getattr(net, '_chains', []))
        sn = tree['SubNetworkPool']['SubNetwork#0']
        e1, w1 = validate_subnetwork(sn)
        e2, w2 = validate_associations(tree, sn)
        errs, warns = e1 + e2, w1 + w2
        errs, kept = c3toc2.split_stock(errs, getattr(net, '_stock_errors', set()))
        warns += kept
        if bent is not None:
            errs += ['scene/.bent: ' + x for x in channels.validate_pair(SceneNetwork.from_subnetwork(sn), bent)]
    if materials and materials.get('apply') and not errs:         # v2.0: the game materials (charmat)
        mlog, merrs, tfx = materials['apply'](tree)
        log += mlog
        errs += merrs
        materials['tfx'] = tfx
    elif materials and materials.get('patches') and not errs:     # v1.92: settings, textures, effects
        import c2fx
        mlog, merrs, tfx = c2fx.apply_materials(tree, materials['patches'], materials.get('fx'),
                                                materials.get('car', 'car'))
        log += mlog
        errs += merrs
        materials['tfx'] = tfx
    return tree, vbuf, ibuf, bent, log, errs, warns


def export(scene_path, bent_path, edits, out_dir, geometry=None, materials=None):
    """Write the edited character flat into out_dir (v2.0.13): <car>.oct +
    <car>_0.vbuf/.ibuf - rebuilt with geometry, else the character's own buffer
    files copied - and the .bent only when it changed. No effects/ (.tfx are
    never written: characters use the stock effects)."""
    import shutil
    tree, vbuf, ibuf, bent, log, errs, warns = build_with_geometry(scene_path, bent_path, edits, geometry, materials)
    if errs:
        return None, log, errs, warns
    stem = os.path.splitext(os.path.basename(scene_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    sp = os.path.join(out_dir, stem + '.oct')
    written = []
    import c2geom
    if vbuf is not None:
        c2geom.write(tree, vbuf, ibuf, sp)
        written += [sp, sp[:-4] + '_0.vbuf', sp[:-4] + '_0.ibuf']
    else:
        octio.save(tree, sp)
        written.append(sp)
        for src in c2geom.buffer_files(scene_path, tree):        # the buffers the .oct names, unchanged
            if os.path.isfile(src):
                dst = os.path.join(out_dir, os.path.basename(src))
                if os.path.abspath(dst) != os.path.abspath(src):
                    shutil.copyfile(src, dst)
                written.append(dst)
    if bent is not None:
        bp = os.path.join(out_dir, os.path.basename(bent_path))
        bent.save(bp)
        try:
            with open(bp, 'rb') as f:
                new = f.read()
            with open(bent_path, 'rb') as f:
                same = f.read() == new
        except OSError:
            same = False
        if same and os.path.abspath(bp) != os.path.abspath(bent_path):
            os.remove(bp)                         # unchanged: the game keeps using its own
        else:
            written.append(bp)
    return written, log, errs, warns

