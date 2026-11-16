"""New animation channels (template slots) without .ct files.

There are no .ct files in the game and the executable holds no template data
(no template or channel names in it). A connection template exists at runtime
only as a slot index space that three shipped files agree on:

  .bent    Interface.BlenderChannelMap   one u16 per slot: blender << 6 | use#
           Blender                       channel name, type, owner slot, flags
           Filter '<ClipSet>-<tmpl>'     one weight per slot
  scene    BaseDataNodeLinks             slot -> data node
  clip     LayerBind(ConnectionTemplate) bind records target slot indices

Verified on the Arcade data: all 20,775 BlenderChannelMap entries name the
slot's channel, 162,732 / 162,757 clip records hit a slot of the right kind,
BlenderChannelMap is exactly one u16 per slot, every Filter has one weight
per slot. So a template grows by appending the same slot index in all three.

Blender facts used here (each holds on every shipping .bent):
  Type 0 transform, 1 KinematicState, 2 Float, 3 Visibility
  map value = blender_index << 6 | use number; the owner (Blender.InterfaceName
  / IndexInInterfaceConnectionMap) is the first user in InterfacePool order and
  use numbers follow InterfacePool order
  ChannelFlags = one byte per user in that order; ChannelBufferIndex = offset
  of those bytes in a dense, conflict-free shared byte pool
  flag bit 0 = the user's ClipSet is additive; owner always has bit 1 set
  Default*Index fields are optional byte offsets into Blenders.DefaultBuffer
  (7,908 / 8,125 blenders have none)
"""
import copy
import os
import struct

import c2oct as octio
from c2oct import FloatList
from c2scene import SceneNetwork

TYPES = {'transform': 0, 'kinematic': 1, 'float': 2, 'visibility': 3}
NODE_TYPES = {0: ('Transform', 'Dilation'), 1: ('KinematicState',), 2: ('Float',), 3: ('Bool',)}


def _short(ct):
    return os.path.splitext(os.path.basename(ct))[0]


class Bent:
    def __init__(self, tree):
        self.tree = tree

    @classmethod
    def load(cls, path):
        return cls(octio.load(path))

    def save(self, path):
        octio.save(self.tree, path)

    # -- views
    @property
    def interfaces(self):
        return [(k.split('#', 1)[1], v) for k, v in self.tree['InterfacePool'].items()]

    @property
    def blenders(self):
        return [(k.split('#', 1)[1], v) for k, v in self.tree['Blenders']['BlenderPool'].items()]

    def slot_map(self, iface):
        m = iface['BlenderChannelMap']
        return list(struct.unpack('<%dH' % (len(m) // 2), m))

    def template_length(self, tmpl):
        ls = [len(self.slot_map(i)) for _, i in self.interfaces
              if i.get('ConnectionTemplate') == _short(tmpl) and 'BlenderChannelMap' in i]
        if not ls:
            raise KeyError('no interface uses template %s' % tmpl)
        if len(set(ls)) != 1:
            raise ValueError('interfaces disagree on %s length: %s' % (tmpl, ls))
        return ls[0]

    def channels(self, tmpl):
        """slot -> channel name for one template."""
        bl = self.blenders
        for _, i in self.interfaces:
            if i.get('ConnectionTemplate') == _short(tmpl) and 'BlenderChannelMap' in i:
                return [bl[v >> 6][0] if v != 0xFFFF else None for v in self.slot_map(i)]
        raise KeyError(tmpl)

    def _users(self):
        users = {}
        for pos, (name, i) in enumerate(self.interfaces):
            if 'BlenderChannelMap' not in i:
                continue
            for s, v in enumerate(self.slot_map(i)):
                if v != 0xFFFF:
                    users.setdefault(v >> 6, []).append((pos, name, s))
        return users

    def _flag_pool(self):
        pool = {}
        for _, b in self.blenders:
            for k, x in enumerate(b['ChannelFlags']):
                pool[b['ChannelBufferIndex'] + k] = x
        return bytes(pool[i] for i in range(len(pool)))

    def _additive(self, iface):
        cs = self.tree.get('ClipSetPool', {}).get('ClipSet#' + iface['ClipSet'], {})
        return bool(cs.get('Additive', 0))

    # -- edit
    def add_channel(self, name, kind, templates, flags=None, filter_weight=1.0):
        """Append channel `name` as a new slot of each template in `templates`.
        kind: 'transform' | 'float' | 'visibility'. Returns {template: slot}.
        flags (one byte per using interface, InterfacePool order) default to
        [2|additive] for a single interface; for several, they are copied from
        an existing channel with the same users and type."""
        typ = TYPES[kind] if isinstance(kind, str) else kind
        if any(n == name for n, _ in self.blenders):
            raise ValueError('channel %s already exists in this .bent' % name)
        shorts = [_short(t) for t in templates]
        ifs = [(pos, n, i) for pos, (n, i) in enumerate(self.interfaces)
               if i.get('ConnectionTemplate') in shorts and 'BlenderChannelMap' in i]
        if {i['ConnectionTemplate'] for _, _, i in ifs} != set(shorts):
            raise KeyError('templates without an interface here: %s'
                           % (set(shorts) - {i['ConnectionTemplate'] for _, _, i in ifs}))
        if flags is None:
            if len(ifs) == 1:
                flags = [2 | int(self._additive(ifs[0][2]))]
            else:
                want = [n for _, n, _ in ifs]
                users = self._users()
                for bi, us in users.items():
                    if [u[1] for u in sorted(us)] == want and self.blenders[bi][1]['Type'] == typ:
                        flags = list(self.blenders[bi][1]['ChannelFlags']); break
                else:
                    raise ValueError('no existing %s channel is used by exactly %s; pass flags= explicitly'
                                     % (kind, want))
        if len(flags) != len(ifs):
            raise ValueError('need one flag per using interface (%d)' % len(ifs))
        # flag pool: reuse an existing run, else append
        pool, f = self._flag_pool(), bytes(flags)
        at = pool.find(f)
        if at < 0:
            at = len(pool)
        bi = len(self.blenders)
        slots = {}
        for use, (pos, n, i) in enumerate(ifs):
            m = self.slot_map(i)
            slots[i['ConnectionTemplate']] = len(m)
            m.append(bi << 6 | use)
            i['BlenderChannelMap'] = struct.pack('<%dH' % len(m), *m)
        owner = ifs[0]
        self.tree['Blenders']['BlenderPool']['Blender#' + name] = {
            'Type': typ, 'InterfaceName': owner[1],
            'IndexInInterfaceConnectionMap': slots[owner[2]['ConnectionTemplate']],
            'ChannelBufferIndex': at, 'ChannelFlags': f}
        for fk, fv in self.tree.get('FilterPool', {}).items():
            pre, tm = fk.split('#', 1)[1].rsplit('-', 1)
            if tm in shorts:
                fv.append(float(filter_weight))
        return {t: slots[_short(t)] for t in templates}


# ------------------------------------------------------------- scene side
def extend_scene(net, template, slot, node):
    """Bind `node` at `slot` of every link set for `template`, appending slots
    as needed (the .bent defines how many there are)."""
    hit = False
    for ls in net.links:
        if ls['ct'] == template or _short(ls['ct']) == _short(template):
            while len(ls['nodes']) <= slot:
                ls['nodes'].append(-1)
            ls['nodes'][slot] = net.index(node)
            hit = True
    if not hit:
        raise KeyError('scene has no link set for %s' % template)


# -------------------------------------------------------------- clip side
def add_clip_curve(clip_tree, template, slot, keys, prop='value', order='zxy', linear=None):
    """Add an animation curve for `slot` of `template` to a clip .oct tree.
    keys: [(time_seconds, value), ...]; prop as in clipblock.driven_key
    ('value' for Float channels, rot_x/loc_y/... for transforms)."""
    import c2clipblock as clipblock
    sn = clip_tree['SubNetworkPool']['SubNetwork#0']
    lbs = [lb for lb in sn['LayerBindPool'].values() if _short(lb['ConnectionTemplate']) == _short(template)]
    if not lbs:
        raise KeyError('clip has no LayerBind for %s' % template)
    lb = lbs[0]
    dn = list(sn['DataNodePool'].values())[lb['ClipDataNodeRef']]
    blk = clipblock.decode(dn['ClipDataBlock'])
    bind = blk['binds'][lb['ClipDataCommandBlockOffset']]
    if any(r['target'] == slot for r in bind['animated'] + bind['static']):
        raise ValueError('clip already drives slot %d' % slot)
    curve = clipblock.make_curve(keys, linear=linear)
    bias, scale, qkeys = clipblock._quantize(curve)
    t0, t1 = curve[0][0], curve[-1][1]
    span = (t1 - t0) or 1.0
    cmd = {'pre': 0, 'post': 0, 'opcode': 0, 't_start': t0, 't_end': t1,
           'params': [[0.0, curve[-1][2][3], bias, scale]],
           'times': [int(round((s[0] - t0) / span * 65535.0)) for s in curve], 'keys': qkeys}
    blk['commands'].append(cmd)
    bind['commands'].append(len(blk['commands']) - 1)
    buf = bind['slot_count']
    bind['slot_count'] += 1
    if prop == 'value':
        rec = {'target': slot, 'flags': 0x20, 'subs': [[0x0019, buf]]}
    elif prop in clipblock.EULER:
        a = clipblock.EULER[prop]
        pool = list(struct.unpack('<%df' % (len(blk['pool']) // 4), blk['pool']))
        if 0.0 not in pool:
            pool.append(0.0)
            blk['pool'] = struct.pack('<%df' % len(pool), *pool)
        z = pool.index(0.0)
        word = 0x1000 | clipblock.MAYA_ORDERS.index(order) << 6 | 1 << (4 - 2 * a)
        rec = {'target': slot, 'flags': 0, 'subs': [[word] + [buf if k == a else z for k in range(3)]]}
    else:
        rec = {'target': slot, 'flags': 0, 'subs': [[clipblock.OUT_OPS[prop] | 0x10, buf]]}
    bind['animated'].append(rec)
    dn['ClipDataBlock'] = clipblock.encode(blk)


# ------------------------------------------------------------- validation
def validate_pair(net, bent):
    """Cross-check a scene and its .bent: every interface's slot count equals
    the scene link set it names, and every blended slot is bound to the node
    the blender names, of a matching type."""
    errs = []
    sets = {ls['name']: ls for ls in net.links}
    bl = bent.blenders
    for name, i in bent.interfaces:
        if 'BlenderChannelMap' not in i:
            continue
        ls = sets.get(i['ConnectionMap'])
        if ls is None:
            errs.append('%s: scene has no link set %s' % (name, i['ConnectionMap'])); continue
        m = bent.slot_map(i)
        if len(m) != len(ls['nodes']):
            errs.append('%s: .bent has %d slots, scene %s has %d'
                        % (name, len(m), i['ConnectionMap'], len(ls['nodes'])))
            continue
        for s, v in enumerate(m):
            if v == 0xFFFF or ls['nodes'][s] < 0:
                continue
            bname, b = bl[v >> 6]
            node = net.data[ls['nodes'][s]]
            if node['Header'] != bname:
                errs.append('%s slot %d: blender %s but scene binds %s' % (name, s, bname, node['Header']))
            elif node['Type'] not in NODE_TYPES.get(b['Type'], ()):
                errs.append('%s slot %d (%s): blender type %d, node is %s' % (name, s, bname, b['Type'], node['Type']))
    return errs


def clip_users(bent_paths, clip_filename):
    """.bent files whose ClipSets reference a clip (by its Filename value)."""
    out = []
    for p in bent_paths:
        t = octio.load(p)
        for cs in t.get('ClipSetPool', {}).values():
            if any(c.get('Filename') == clip_filename for c in cs.get('ClipPool', {}).values()):
                out.append(p); break
    return out


def bent_invariants(bent):
    """Rules every shipping .bent obeys; returns a list of violations."""
    errs = []
    if 'Blenders' not in bent.tree:
        return errs
    bl = bent.blenders
    names = [n for n, _ in bent.interfaces]
    users = bent._users()
    for bi, (bname, b) in enumerate(bl):
        us = sorted(users.get(bi, []))
        if not us:
            errs.append('blender %s is not used by any interface' % bname); continue
        if b['Type'] not in NODE_TYPES:
            errs.append('blender %s: unknown type %r' % (bname, b['Type']))
        if len(b['ChannelFlags']) != len(us):
            errs.append('blender %s: %d flags for %d users' % (bname, len(b['ChannelFlags']), len(us)))
        if (us[0][1], us[0][2]) != (b['InterfaceName'], b['IndexInInterfaceConnectionMap']):
            errs.append('blender %s: owner is not its first user' % bname)
    for pos, (n, i) in enumerate(bent.interfaces):
        if 'BlenderChannelMap' not in i:
            continue
        for s, v in enumerate(bent.slot_map(i)):
            if v == 0xFFFF:
                continue
            bi, use = v >> 6, v & 63
            if bi >= len(bl):
                errs.append('%s slot %d: blender %d out of range' % (n, s, bi)); continue
            rank = [u[0] for u in sorted(users[bi])].index(pos)
            if use != rank:
                errs.append('%s slot %d: use number %d, expected %d' % (n, s, use, rank))
            if (b := bl[bi][1])['ChannelFlags'][rank] & 1 != int(bent._additive(i)):
                errs.append('%s slot %d: flag additive bit disagrees with ClipSet' % (n, s))
    pool = {}
    for bname, b in bl:
        for k, x in enumerate(b['ChannelFlags']):
            j = b['ChannelBufferIndex'] + k
            if pool.setdefault(j, x) != x:
                errs.append('flag pool conflict at %d (%s)' % (j, bname))
    if sorted(pool) != list(range(len(pool))):
        errs.append('flag pool has holes')
    for fk, fv in bent.tree.get('FilterPool', {}).items():
        tm = fk.split('#', 1)[1].rsplit('-', 1)[1]
        try:
            if len(fv) != bent.template_length(tm):
                errs.append('filter %s has %d weights, template has %d slots' % (fk, len(fv), bent.template_length(tm)))
        except KeyError:
            pass
    nb = len(bent.tree['Blenders'].get('DefaultBuffer', b''))
    for bname, b in bl:       # Default*Index are byte offsets into DefaultBuffer
        for k, size in (('DefaultIndex', 4), ('DefaultTranslationIndex', 12),
                        ('DefaultRotationIndex', 16), ('DefaultScaleIndex', 12)):
            if k in b and b[k] + size > nb:
                errs.append('blender %s: %s past DefaultBuffer' % (bname, k))
    return errs


def validate_clip(clip_tree, bent):
    """Check a clip against a .bent that plays it: every record targets an
    existing slot of its template, of the matching kind (scalar records on
    Float channels, transform records on transform channels)."""
    import c2clipblock as clipblock
    errs = []
    sn = clip_tree['SubNetworkPool']['SubNetwork#0']
    dn = list(sn['DataNodePool'].values())
    bl = bent.blenders
    for lb in sn.get('LayerBindPool', {}).values():
        tm = _short(lb['ConnectionTemplate'])
        try:
            iface = next(i for _, i in bent.interfaces if i.get('ConnectionTemplate') == tm and 'BlenderChannelMap' in i)
        except StopIteration:
            continue                                  # template not used by this character
        m = bent.slot_map(iface)
        blk = clipblock.decode(dn[lb['ClipDataNodeRef']]['ClipDataBlock'])
        bind = blk['binds'][lb['ClipDataCommandBlockOffset']]
        for r in bind['animated'] + bind['static']:
            s = r['target']
            if s >= len(m):
                errs.append('%s: record targets slot %d, template has %d' % (tm, s, len(m))); continue
            if m[s] == 0xFFFF:
                continue
            bname, b = bl[m[s] >> 6]
            scalar = bool(r['flags'] & 0x20)
            if scalar != (b['Type'] in (2, 3)):
                errs.append('%s slot %d (%s): %s record on a type-%d channel'
                            % (tm, s, bname, 'scalar' if scalar else 'transform', b['Type']))
    return errs


def pad_template(bent, net, template, length):
    """Grow `template` to `length` slots with UNUSED slots (0xFFFF in the
    .bent map, filter weight 0, unbound in the scene) - the form unused slots
    already take in shipping rigs. Use it to keep every character's template
    the same length as one that gained custom channels."""
    tm = _short(template)
    for _, i in bent.interfaces:
        if i.get('ConnectionTemplate') == tm and 'BlenderChannelMap' in i:
            m = bent.slot_map(i)
            m += [0xFFFF] * (length - len(m))
            i['BlenderChannelMap'] = struct.pack('<%dH' % len(m), *m)
    for fk, fv in bent.tree.get('FilterPool', {}).items():
        if fk.split('#', 1)[1].rsplit('-', 1)[1] == tm:
            fv.extend([0.0] * (length - len(fv)))
    for ls in net.links:
        if _short(ls['ct']) == tm:
            ls['nodes'].extend([-1] * (length - len(ls['nodes'])))
