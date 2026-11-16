"""Editable model of a Cars 2 Scene SubNetwork (SubNetworkPool/SubNetwork#0, Type 'Scene').

Everything the engine cross-references by number is addressed here by *path*
(e.g. 'Anchor/Main_Ctrl/RotateFront') and regenerated on build():

    Header ids, HeaderStrings / HeaderStringIndices / HeaderLayout
    ConnectionMapUsedDataNodes and the indices inside DataNodeLinks
    RestPositionIndices / RestPositionData for newly linked transforms
    NetworkBuffer and NetworkBufferOffsets for newly added buffered processors

Existing nodes keep their DataNodePool index (new nodes are appended), so the
opaque blocks that may hold node indices (UberConstraintDataBlock,
RelationshipDataBlock, ClipDataBlock) stay valid. An unedited network rebuilds
byte-identically (verified on all shipping Cars 2 Arcade scenes).

    net = SceneNetwork.load('mcqueen.oct')
    net.add_data('Anchor/Main_Ctrl/MyDial', 'Float', data=0.0)
    net.connect('Anchor/Main_Ctrl/MyDial', 'Anchor/Main_Ctrl/RotateFront')
    net.save('mcqueen.oct')
"""
import copy
import json
import os
import struct

import c2oct as octio
from c2oct import IntList, FloatList, StrList
from c2headers import decode_paths, decode_paths_unique, encode_paths, allocate

HERE = os.path.dirname(os.path.abspath(__file__))
TRANSFORM_TYPES = ('Transform', 'Dilation')
NONE16 = 0xFFFF

# Fields on processors whose ints are DataNodePool indices.
REF_FIELDS = ('DataNodeRefs', 'Input', 'Output', 'LazyDataNode', 'UberConstraintDataInput',
              'RelationshipData', 'ClipData', 'ChainNode', 'WorldUpVector', 'WorldUpPoint')
BUFFERED = {'RadialBasisApplyFloat': 4}   # bytes of NetworkBuffer state, observed
SHARED_AT_END = ('UberConstraintProcessor',)  # offset == len(NetworkBuffer), no bytes


def _catalog():
    p = os.path.join(HERE, 'ct_catalog.json')
    return json.load(open(p)) if os.path.exists(p) else {}


def _schema():
    p = os.path.join(HERE, 'schema.json')
    return json.load(open(p)) if os.path.exists(p) else {}


class SceneNetwork:
    # ------------------------------------------------------------------ load
    def __init__(self):
        self.name = 'Scene'
        self.key_order = None
        self.paths = []          # header paths in original id order
        self.data = []           # DataNodePool, dicts with 'Header' = path
        self.procs = []          # ProcessorNodePool, same
        self.links = []          # [{'name', 'ct', 'nodes': [data index | -1]}]
        self.cm = []             # original ConnectionMapUsedDataNodes (data indices)
        self.rest = {}           # data index -> (translation offset, rotor offset)
        self.rest_pool = []      # floats
        self.network_buffer = b''
        self.has_rest_keys = False
        self.has_nb_key = False
        self.has_links_key = False
        self._outer = None
        self._ct = _catalog()

    @classmethod
    def from_subnetwork(cls, sn):
        if sn.get('Type') != 'Scene':
            raise ValueError('not a Scene SubNetwork: %r' % sn.get('Type'))
        self = cls()
        self.name = sn.get('Name', 'Scene')
        self.key_order = list(sn.keys())
        self.paths = decode_paths_unique(sn['HeaderStrings'], sn['HeaderStringIndices'], sn['HeaderLayout'])
        for pool, out in (('DataNodePool', self.data), ('ProcessorNodePool', self.procs)):
            for v in sn.get(pool, {}).values():
                n = copy.deepcopy(v)
                n['Header'] = self.paths[v['Header'] - 1]
                out.append(n)
        self.cm = list(sn.get('ConnectionMapUsedDataNodes', []))
        self.has_links_key = 'BaseDataNodeLinksPool' in sn
        for v in sn.get('BaseDataNodeLinksPool', {}).values():
            self.links.append({'name': v['Name'], 'ct': v['ConnectionTemplate'],
                               'nodes': [self.cm[d] if d >= 0 else -1 for d in v['DataNodeLinks']]})
        self.has_rest_keys = 'RestPositionIndices' in sn
        ri, rd = sn.get('RestPositionIndices', b''), sn.get('RestPositionData', b'')
        if ri:
            idx = struct.unpack('<%dH' % (len(ri) // 2), ri)
            for j, d in enumerate(self.cm):
                if idx[2 * j] != NONE16 or idx[2 * j + 1] != NONE16:
                    self.rest[d] = (idx[2 * j], idx[2 * j + 1])
        if len(rd) >= 4:   # an empty pool is written as a single 0x00 byte
            self.rest_pool = list(struct.unpack('<%df' % (len(rd) // 4), rd))
        self.has_nb_key = 'NetworkBuffer' in sn
        self.network_buffer = bytes(sn.get('NetworkBuffer', b''))
        return self

    @classmethod
    def load(cls, path):
        tree = octio.load(path)
        self = cls.from_subnetwork(tree['SubNetworkPool']['SubNetwork#0'])
        self._outer = tree
        return self

    # ----------------------------------------------------------------- lookup
    def index(self, ref):
        """Data node index from a path or an index."""
        if isinstance(ref, int):
            return ref
        for i, n in enumerate(self.data):
            if n['Header'] == ref:
                return i
        raise KeyError('no data node at %r' % ref)

    def proc_index(self, path):
        for i, n in enumerate(self.procs):
            if n['Header'] == path:
                return i
        raise KeyError('no processor at %r' % path)

    def has(self, path):
        return any(n['Header'] == path for n in self.data + self.procs)

    def path_of(self, i):
        return self.data[i]['Header']

    def template(self, ct):
        e = self._ct.get(ct)
        if e is None:
            raise KeyError('connection template %r not in ct_catalog.json' % ct)
        return e

    # ------------------------------------------------------------------- edit
    def _check_new(self, path, kind, typ):
        if self.has(path):
            raise ValueError('%r already exists' % path)
        if not path or any(not seg for seg in path.split('/')):
            raise ValueError('malformed path %r' % path)
        eng = (_schema().get('engine') or {}).get(kind)
        if eng and typ not in eng:
            raise ValueError('%s %r is not registered in the game executable' % (kind, typ))

    def add_data(self, path, typ, data=None, basis=None, **extra):
        """Append a DataNode. `basis` (path/index) is the parent transform for
        Transform/Dilation/Vector nodes. Returns its index."""
        self._check_new(path, 'DataNode', typ)
        n = {'Header': path, 'Type': typ}
        if basis is not None:
            n['BasisDataNodeRef'] = self.index(basis)
        if typ in TRANSFORM_TYPES and data is None:
            data = {}
        if data is not None:
            if typ in TRANSFORM_TYPES:
                d = {}
                for k, v in data.items():
                    d[k] = v if isinstance(v, (dict, int)) else FloatList(float(x) for x in v)
                data = d
            elif typ == 'Float':
                data = float(data)
            elif typ == 'Bool':
                data = int(bool(data))
            elif isinstance(data, (list, tuple)) and not any(
                    c.__name__ in ('IntList', 'FloatList', 'StrList') for c in type(data).__mro__):
                data = FloatList(float(x) for x in data)
            n['Data'] = data
        n.update(extra)
        self.data.append(n)
        return len(self.data) - 1

    def add_processor(self, path, typ, **fields):
        """Append a ProcessorNode. Fields in REF_FIELDS accept paths. Use
        refs=[(attribute, node), ...] for DataNodeRefs/DataNodeAttributes
        processors (Connection, RadialBasis*, KinematicStateDecompose)."""
        self._check_new(path, 'ProcessorNode', typ)
        n = {'Header': path, 'Type': typ}
        refs = fields.pop('refs', None)
        if refs is not None:
            n['DataNodeRefs'] = IntList(self.index(r) for _, r in refs)
            n['DataNodeAttributes'] = StrList(a for a, _ in refs)
        for k, v in fields.items():
            if k in REF_FIELDS:
                v = IntList(self.index(x) for x in v) if isinstance(v, (list, tuple)) else self.index(v)
            n[k] = v
        if typ in BUFFERED and 'NetworkBufferOffsets' not in n:
            self._grow_buffer(BUFFERED[typ], n)
        elif typ in SHARED_AT_END and 'NetworkBufferOffsets' not in n:
            n['NetworkBufferOffsets'] = IntList([len(self.network_buffer)])
            n['NetworkBufferNames'] = StrList(['Data'])
        self.procs.append(n)
        return len(self.procs) - 1

    def _grow_buffer(self, size, node, initial=None):
        old = len(self.network_buffer)
        node['NetworkBufferOffsets'] = IntList([old])
        node['NetworkBufferNames'] = StrList(['Data'])
        self.network_buffer += initial if initial is not None else bytes(size)
        self.has_nb_key = True
        for p in self.procs:      # shared end-of-buffer slots move with the end
            if p['Type'] in SHARED_AT_END:
                p['NetworkBufferOffsets'] = IntList(len(self.network_buffer) if o == old else o
                                                    for o in p['NetworkBufferOffsets'])

    def insert_data(self, position, path, typ, data=None, basis=None, **extra):
        """Like add_data, but places the node at DataNodePool `position`
        (e.g. ahead of a node that will use it as its basis - shipping pools
        always list parents first) and renumbers every reference: basis refs,
        all processor ref fields, template links, connection map and rest data.
        Safe because no blob addresses data nodes globally (UberConstraint and
        ClipData blocks index their processor's own lists; identical
        RelationshipData blocks ship with different global numbering)."""
        i = self.add_data(path, typ, data=data, basis=basis, **extra)
        node = self.data.pop(i)
        if not 0 <= position <= len(self.data):
            raise IndexError(position)
        shift = lambda x: x + 1 if isinstance(x, int) and x >= position else x
        for d in self.data:
            if 'BasisDataNodeRef' in d:
                d['BasisDataNodeRef'] = shift(d['BasisDataNodeRef'])
        if 'BasisDataNodeRef' in node:
            node['BasisDataNodeRef'] = shift(node['BasisDataNodeRef'])
        for p in self.procs:
            for k in REF_FIELDS:
                if k in p:
                    v = p[k]
                    p[k] = IntList(shift(x) for x in v) if isinstance(v, list) else shift(v)
        for ls in self.links:
            ls['nodes'] = [shift(x) if x >= 0 else x for x in ls['nodes']]
        self.cm = [shift(x) for x in self.cm]
        self.rest = {shift(k): v for k, v in self.rest.items()}
        self.data.insert(position, node)
        return position

    def set_basis(self, node, basis):
        """Re-parent a transform node (its local Data is kept as-is)."""
        i, b = self.index(node), self.index(basis)
        if b > i:
            raise ValueError('basis %s comes after %s in the pool; insert it earlier with insert_data'
                             % (self.path_of(b), self.path_of(i)))
        self.data[i]['BasisDataNodeRef'] = b

    def connect(self, src, dst, path=None):
        """Connection processor: copies src -> dst every evaluation."""
        if path is None:
            path = self.path_of(self.index(dst)) + '/Connection'
        return self.add_processor(path, 'Connection', refs=[('Input', src), ('Output', dst)])

    def set_refs(self, proc_path, **fields):
        """Rewire an existing processor. Fields in REF_FIELDS take paths/indices;
        for DataNodeRefs-style processors pass the attribute name, e.g.
        set_refs('.../Connection', Input='Anchor/X/Y')."""
        p = self.procs[self.proc_index(proc_path)]
        for k, v in fields.items():
            if 'DataNodeAttributes' in p and k in p['DataNodeAttributes']:
                refs = list(p['DataNodeRefs'])
                refs[list(p['DataNodeAttributes']).index(k)] = self.index(v)
                p['DataNodeRefs'] = IntList(refs)
            elif k in REF_FIELDS:
                p[k] = IntList(self.index(x) for x in v) if isinstance(v, (list, tuple)) else self.index(v)
            else:
                raise KeyError('%s has no ref field %s' % (proc_path, k))

    # ------------------------------------------------- constraints / driven keys
    def add_constraint(self, owner, targets, ops, lazy=None, world_up=None, path=None):
        """UberConstraint: `owner` (transform path) follows `targets` (list of
        transform paths; op 'input' indexes this list). `ops` is a list of
        ubercon op dicts, e.g. ubercon.parent_ops(). `lazy` = Bool group node
        (e.g. 'Anchor/Lazy_MouthConstraintGroup'); `world_up` = Vector or
        Position node for aim ops. Returns the processor index."""
        import c2ubercon as ubercon
        targets = targets if isinstance(targets, (list, tuple)) else [targets]
        owner_name = self.path_of(self.index(owner)).rsplit('/', 1)[0].split('/', 1)[1]
        root = self.path_of(self.index(owner)).split('/', 1)[0]
        base = path or '%s/UberConstraint_custom%s/%s' % (root, root, owner_name)
        blob = self.add_data(base + '/UberConstraintData', 'UberConstraintData',
                             Static=1, UberConstraintDataBlock=ubercon.encode(ops))
        fields = {}
        if lazy is not None:
            fields['LazyDataNode'] = lazy
        fields['UberConstraintDataInput'] = blob
        fields['Output'] = [owner]
        fields['Input'] = list(targets)
        if world_up is not None:
            kind = self.data[self.index(world_up)]['Type']
            fields['WorldUpVector' if kind == 'Vector' else 'WorldUpPoint'] = [world_up]
        return self.add_processor(base + '/UberConstraintProcessor', 'UberConstraintProcessor', **fields)

    def constraint_of(self, owner):
        i = self.index(owner)
        for k, p in enumerate(self.procs):
            if p['Type'] == 'UberConstraintProcessor' and i in _outputs(p):
                return k
        return None

    def get_constraint_ops(self, owner):
        import c2ubercon as ubercon
        p = self.procs[self.constraint_of(owner)]
        return ubercon.decode(self.data[p['UberConstraintDataInput']]['UberConstraintDataBlock'])

    def set_constraint_ops(self, owner, ops, targets=None):
        """Replace the ops (and optionally the targets) of the constraint driving `owner`."""
        import c2ubercon as ubercon
        k = self.constraint_of(owner)
        if k is None:
            raise KeyError('no UberConstraint drives %r' % owner)
        p = self.procs[k]
        self.data[p['UberConstraintDataInput']]['UberConstraintDataBlock'] = ubercon.encode(ops)
        if targets is not None:
            p['Input'] = IntList(self.index(t) for t in targets)

    def add_driven_key(self, driver, outputs, curve, order='zxy', path=None):
        """Set-driven key: `outputs` = [(node path, property or [properties])]
        driven by the Float `driver` through `curve` (clipblock.make_curve).
        Properties: rot_x/y/z (radians), loc_x/y/z, scale_x/y/z, value (Float)."""
        import c2clipblock as clipblock
        blob = clipblock.driven_key(curve, [p for _, p in outputs], order)
        base = path or self.path_of(self.index(driver)) + '/DrivenKey%d' % sum(
            1 for q in self.procs if q['Header'].startswith(self.path_of(self.index(driver)) + '/DrivenKey'))
        d = self.add_data(base + '/ClipData', 'ClipData', Static=1, ClipDataBlock=blob)
        return self.add_processor(base + '/ClipProcessor', 'ClipProcessor',
                                  Input=driver, ClipData=d, Output=[n for n, _ in outputs])

    def set_driven_key(self, proc_path, curve, props=None, order=None):
        """Replace the curve of an existing ClipProcessor (keeping its outputs and
        bound properties unless `props` is given)."""
        import c2clipblock as clipblock
        p = self.procs[self.proc_index(proc_path)]
        old = clipblock.decode(self.data[p['ClipData']]['ClipDataBlock'])
        if props is None or order is None:
            found, o = [], 'zxy'
            for r in old['binds'][0]['animated']:
                pr = []
                for sw in r['subs']:
                    w = sw[0]
                    if w & 0xF000 == 0x1000:
                        o = clipblock.MAYA_ORDERS[(w >> 6) & 7]
                        pr += ['rot_' + 'xyz'[a] for a in range(3) if (w >> (4 - 2 * a)) & 3 == 1]
                    else:
                        pr += [k for k, v in clipblock.OUT_OPS.items() if v == (w & 0xF)]
                found.append(pr)
            props = props or found
            order = order or o
        self.data[p['ClipData']]['ClipDataBlock'] = clipblock.driven_key(curve, props, order)

    def writer_of(self, node):
        i = self.index(node)
        for k, p in enumerate(self.procs):
            if i in _outputs(p):
                return k
        return None

    def link(self, ct, channel, node, set_name=None):
        """Bind `node` to the template slot named `channel` in every link set
        for `ct` (or only the set called `set_name`)."""
        e = self.template(ct)
        names = [s and s['channel'] for s in e['slots']]
        if channel not in names:
            raise KeyError('%s has no channel %r' % (ct, channel))
        slot = names.index(channel)
        hit = False
        for ls in self.links:
            if ls['ct'] == ct and (set_name is None or ls['name'] == set_name):
                if len(ls['nodes']) != e['length']:
                    raise ValueError('%s link set %s has %d slots, catalog has %d'
                                     % (ct, ls['name'], len(ls['nodes']), e['length']))
                ls['nodes'][slot] = self.index(node)
                hit = True
        if not hit:
            raise KeyError('no link set for %s; call add_link_set first' % ct)

    def add_link_set(self, ct, name=None):
        e = self.template(ct)
        if name is None:
            name = 'Anchor-' + os.path.splitext(os.path.basename(ct))[0]
        self.links.append({'name': name, 'ct': ct, 'nodes': [-1] * e['length']})
        self.has_links_key = True
        return name

    def autolink(self, ct=None):
        """Fill every empty template slot whose channel path exists here."""
        n = 0
        bypath = {d['Header']: i for i, d in enumerate(self.data)}
        for ls in self.links:
            if ct and ls['ct'] != ct:
                continue
            e = self._ct.get(ls['ct'])
            if not e or len(ls['nodes']) != e['length']:
                continue
            for s, slot in enumerate(e['slots']):
                if slot and ls['nodes'][s] < 0 and slot['channel'] in bypath:
                    ls['nodes'][s] = bypath[slot['channel']]; n += 1
        return n

    def clone_processor(self, src_path, new_path, remap, rename=None):
        """Duplicate a processor. `remap` maps old data paths -> new data paths
        for its inputs/outputs. Static data nodes owned only by this processor
        (UberConstraintData, RelationshipData, ClipData...) are duplicated too
        and named via rename(old_path) (default: replace the processor's owner
        prefix)."""
        src = self.procs[self.proc_index(src_path)]
        owner_old = src_path.rsplit('/', 1)[0]
        owner_new = new_path.rsplit('/', 1)[0]
        rename = rename or (lambda p: p.replace(owner_old, owner_new, 1))
        users = {}
        for p in self.procs:
            for i in _refs(p):
                users.setdefault(i, set()).add(p['Header'])
        n = copy.deepcopy(src)
        n['Header'] = new_path
        idxmap = {}
        for i in _refs(src):
            old = self.path_of(i)
            if old in remap:
                idxmap[i] = self.index(remap[old])
            elif self.data[i].get('Static') and users.get(i) == {src_path}:
                c = copy.deepcopy(self.data[i]); c['Header'] = rename(old)
                if self.has(c['Header']):
                    raise ValueError('clone target %r exists' % c['Header'])
                self.data.append(c); idxmap[i] = len(self.data) - 1
        for k in REF_FIELDS:
            if k in n:
                v = n[k]
                n[k] = IntList(idxmap.get(x, x) for x in v) if isinstance(v, list) else idxmap.get(v, v)
        if n['Type'] in BUFFERED:
            off = src['NetworkBufferOffsets'][0]
            self._grow_buffer(BUFFERED[n['Type']], n, self.network_buffer[off:off + BUFFERED[n['Type']]])
        self.procs.append(n)
        return len(self.procs) - 1

    # ------------------------------------------------------------------ build
    def _rest_for(self, node, pool):
        """Rest pose (translation, rotor) for a newly linked transform, packed
        into the shared float pool the way the exporter does (exact runs are
        reused, values below 1e-6 snapped to 0). Matches shipping rest data to
        <1e-5 on 892/910 nodes; the rest are nodes the exporter evaluated from
        the Maya pose (e.g. scaled eye frames), which file data cannot recover."""
        snap = lambda v: [0.0 if abs(x) < 1e-6 else float(x) for x in v]
        d = node.get('Data') if isinstance(node.get('Data'), dict) else {}
        t = snap(d.get('Translation', [0.0, 0.0, 0.0]))
        r = d.get('Orientation#Rotor')
        if r is None and 'Orientation#OrientProxy' in d:
            r = d['Orientation#OrientProxy'].get('LocalToLocalProxy#Rotor')
        r = snap(r if r is not None else [0.0, 0.0, 0.0, 1.0])
        def find(seq):
            n = len(seq)
            for o in range(len(pool) - n + 1):
                if all(struct.pack('<f', pool[o + k]) == struct.pack('<f', seq[k]) for k in range(n)):
                    return o
            pool.extend(seq)
            return len(pool) - n
        return (find(t), find(r))

    def build(self):
        node_paths = [n['Header'] for n in self.data + self.procs]
        if len(set(node_paths)) != len(node_paths):
            raise ValueError('duplicate node paths')
        known = set(self.paths)
        ordered = allocate(self.paths + [p for p in node_paths if p not in known])
        if ordered[:len(self.paths)] != self.paths:
            # ids of later-allocated names shift; that is legal (Header is the
            # only consumer) - just make sure the result is self-consistent.
            pass
        strings, indices, layout = encode_paths(ordered)
        hid = {p: i + 1 for i, p in enumerate(ordered)}

        linked = []
        for ls in self.links:
            for d in ls['nodes']:
                if d >= 0 and d not in linked:
                    linked.append(d)
        cm = [d for d in self.cm if d in linked] + [d for d in linked if d not in self.cm]
        cmpos = {d: j for j, d in enumerate(cm)}

        pool = list(self.rest_pool)
        rest_idx = IntList()
        packed = []
        for d in cm:
            if d in self.rest:
                a, b = self.rest[d]
            elif d not in self.cm and self.data[d]['Type'] in TRANSFORM_TYPES:
                a, b = self._rest_for(self.data[d], pool)
            else:
                a, b = NONE16, NONE16
            packed += [a, b]

        def pool_of(nodes, label):
            out = {}
            for i, n in enumerate(nodes):
                m = {}
                for k, v in n.items():
                    m[k] = hid[v] if k == 'Header' else v
                out['%s#%d' % (label, i)] = m
            return out

        fields = {
            'Name': self.name, 'Type': 'Scene',
            'HeaderStrings': StrList(strings), 'HeaderStringIndices': IntList(indices),
            'HeaderLayout': IntList(layout),
            'DataNodePool': pool_of(self.data, 'DataNode'),
            'ProcessorNodePool': pool_of(self.procs, 'ProcessorNode'),
        }
        if cm or self.has_links_key:
            fields['ConnectionMapUsedDataNodes'] = IntList(cm)
        if cm or self.has_rest_keys:
            fields['RestPositionIndices'] = struct.pack('<%dH' % len(packed), *packed)
            fields['RestPositionData'] = struct.pack('<%df' % len(pool), *pool) if pool else b'\x00'
        if self.links or self.has_links_key:
            fields['BaseDataNodeLinksPool'] = {
                'BaseDataNodeLinks#%d' % i: {
                    'Name': ls['name'], 'ConnectionTemplate': ls['ct'],
                    'DataNodeLinks': IntList(cmpos[d] if d >= 0 else -1 for d in ls['nodes'])}
                for i, ls in enumerate(self.links)}
        if self.network_buffer or self.has_nb_key:
            fields['NetworkBuffer'] = self.network_buffer
        order = self.key_order or ['Name', 'Type', 'HeaderStrings', 'HeaderStringIndices', 'HeaderLayout',
                                   'DataNodePool', 'ProcessorNodePool', 'ConnectionMapUsedDataNodes',
                                   'RestPositionIndices', 'RestPositionData', 'BaseDataNodeLinksPool',
                                   'NetworkBuffer']
        out = {k: fields[k] for k in order if k in fields}
        for k in fields:
            out.setdefault(k, fields[k])
        return out

    def to_tree(self, outer=None):
        tree = copy.copy(outer if outer is not None else self._outer)
        if tree is None:
            raise ValueError('no surrounding .oct tree; pass outer=')
        tree = dict(tree)
        tree['SubNetworkPool'] = {'SubNetwork#0': self.build()}
        return tree

    def save(self, path, outer=None):
        octio.save(self.to_tree(outer), path)


def _refs(p):
    out = []
    for k in REF_FIELDS:
        v = p.get(k)
        if v is None:
            continue
        out += list(v) if isinstance(v, list) else [v]
    return [x for x in out if isinstance(x, int) and x >= 0]


def _outputs(p):
    if 'DataNodeAttributes' in p:
        return [d for d, a in zip(p['DataNodeRefs'], p['DataNodeAttributes']) if a == 'Output']
    out = []
    v = p.get('Output')
    if v is not None:
        out += list(v) if isinstance(v, list) else [v]
    if p['Type'] == 'DynamicChain' and 'ChainNode' in p:
        v = p['ChainNode']; out += list(v) if isinstance(v, list) else [v]
    return out


def _inputs(p):
    if 'DataNodeAttributes' in p:
        return [d for d, a in zip(p['DataNodeRefs'], p['DataNodeAttributes']) if a != 'Output']
    out = []
    for k in ('Input', 'ClipData', 'UberConstraintDataInput', 'RelationshipData', 'WorldUpVector', 'WorldUpPoint'):
        v = p.get(k)
        if v is not None:
            out += list(v) if isinstance(v, list) else [v]
    return out
