"""Check a Scene SubNetwork against the rules every shipping Cars 2 rig obeys.

    python validate.py file.oct [more.oct ...]

ERROR   would break loading/evaluation for certain (bad reference, unknown type,
        malformed header tree, wrong template length, two writers, ...)
WARN    legal in the format but not seen in shipping data, or risky
"""
import json
import os
import struct
import sys

import c2oct as octio
from c2headers import decode_paths
from c2scene import (SceneNetwork, REF_FIELDS, TRANSFORM_TYPES, NONE16,
                   _inputs, _outputs, _schema, _catalog)


def validate_subnetwork(sn, schema=None, catalog=None, allow_old_templates=False):
    schema = schema or _schema()
    catalog = catalog or _catalog()
    errs, warns = [], []
    E = errs.append
    W = warns.append
    eng = schema.get('engine') or {}

    if sn.get('Type') not in (eng.get('SubNetwork') or ['Clip', 'Scene']):
        E('SubNetwork Type %r unknown to the engine' % sn.get('Type'))
    if sn.get('Type') != 'Scene':
        return errs, warns

    # header tree
    try:
        paths = decode_paths(sn['HeaderStrings'], sn['HeaderStringIndices'], sn['HeaderLayout'])
    except Exception as ex:
        E('header tables invalid: %s' % ex)
        return errs, warns
    if len(set(paths)) != len(paths):
        # v2.0.21: a WARNING - shipping King has two separate 'Anchor/Null' branches and the game loads it
        W('same-named entries in header tree: %s' % ', '.join(sorted({p for p in paths if paths.count(p) > 1})[:4]))
    if any(i < 0 or i >= len(sn['HeaderStrings']) for i in sn['HeaderStringIndices']):
        E('HeaderStringIndices out of range')

    dn = list(sn.get('DataNodePool', {}).values())
    pn = list(sn.get('ProcessorNodePool', {}).values())
    nd = len(dn)
    seen_h = {}
    for kind, pool in (('DataNode', dn), ('ProcessorNode', pn)):
        for i, n in enumerate(pool):
            tag = '%s#%d' % (kind, i)
            h = n.get('Header')
            if not isinstance(h, int) or not 1 <= h <= len(paths):
                E('%s Header %r out of range' % (tag, h)); continue
            if h in seen_h:
                E('%s shares Header %d (%s) with %s' % (tag, h, paths[h - 1], seen_h[h]))
            seen_h[h] = tag
            tag += ' ' + paths[h - 1]
            t = n.get('Type')
            if eng.get(kind) and t not in eng[kind]:
                E('%s: type %r not registered in the game' % (tag, t))
            elif t not in schema.get(kind, {}):
                W('%s: type %r is engine-valid but used by no shipping rig' % (tag, t))
            else:
                ref = schema[kind][t]
                for f, freq in ref['fields'].items():
                    if freq == 1.0 and f not in n:
                        E('%s: missing field %s (present on every shipping %s)' % (tag, f, t))
                for f in n:
                    if f not in ref['fields']:
                        W('%s: field %s never seen on a shipping %s' % (tag, f, t))
                if kind == 'ProcessorNode' and ref['attribute_layouts'] and \
                        list(n.get('DataNodeAttributes', [])) not in ref['attribute_layouts']:
                    E('%s: DataNodeAttributes %r not one of %r' % (tag, n.get('DataNodeAttributes'),
                                                                   ref['attribute_layouts']))
                if kind == 'DataNode' and isinstance(n.get('Data'), dict):
                    for k in n['Data']:
                        if k not in ref['data_keys']:
                            W('%s: Data key %s never seen on a shipping %s' % (tag, k, t))
            if kind == 'DataNode':
                b = n.get('BasisDataNodeRef')
                if b is not None:
                    if not 0 <= b < nd:
                        E('%s: BasisDataNodeRef %d out of range' % (tag, b))
                    elif dn[b]['Type'] not in TRANSFORM_TYPES:
                        E('%s: basis is a %s, not a transform' % (tag, dn[b]['Type']))
                    elif b >= i:
                        W('%s: basis comes later in the pool (shipping pools list parents first)' % tag)
            else:
                for f in REF_FIELDS:
                    v = n.get(f)
                    if v is None:
                        continue
                    for x in (v if isinstance(v, list) else [v]):
                        if not (isinstance(x, int) and 0 <= x < nd):
                            E('%s: %s ref %r out of range' % (tag, f, x))
                if 'DataNodeRefs' in n and len(n['DataNodeRefs']) != len(n.get('DataNodeAttributes', [])):
                    E('%s: DataNodeRefs/DataNodeAttributes length mismatch' % tag)
    if errs:
        return errs, warns

    # evaluation order / writers
    writer = {}
    for i, p in enumerate(pn):
        for o in _outputs(p):
            if o in writer:
                E('data node %s written by both %s and %s' % (paths[dn[o]['Header'] - 1],
                                                           paths[pn[writer[o]]['Header'] - 1],
                                                           paths[p['Header'] - 1]))
            writer[o] = i
    for i, p in enumerate(pn):
        for d in _inputs(p):
            w = writer.get(d)
            if w is not None and w > i:
                W('%s reads %s before %s writes it (pool order is evaluation order)'
                  % (paths[p['Header'] - 1], paths[dn[d]['Header'] - 1], paths[pn[w]['Header'] - 1]))

    # blob contents: constraints and driven keys
    import c2ubercon as ubercon, c2clipblock as clipblock
    for p in pn:
        tag = paths[p['Header'] - 1]
        if p['Type'] == 'UberConstraintProcessor':
            blob = dn[p['UberConstraintDataInput']].get('UberConstraintDataBlock')
            try:
                ops = ubercon.decode(blob)
            except Exception as ex:
                E('%s: constraint block unreadable (%s)' % (tag, ex)); continue
            if not ops:
                E('%s: constraint has no ops' % tag)
            n_in = len(p['Input']) if isinstance(p['Input'], list) else 1
            for op in ops:
                if op['input'] >= n_in:
                    E('%s: op %s reads input %d, processor has %d' % (tag, op['op'], op['input'], n_in))
                if op['op'] == 'aim':
                    need = {'vector': 'WorldUpVector', 'object': 'WorldUpPoint'}.get(op.get('up_type'))
                    if need and need not in p:
                        E('%s: aim op uses %s up but processor has no %s' % (tag, op['up_type'], need))
            for o in (p['Output'] if isinstance(p['Output'], list) else [p['Output']]):
                if dn[o]['Type'] not in TRANSFORM_TYPES:
                    E('%s: constrains a %s, not a transform' % (tag, dn[o]['Type']))
        elif p['Type'] == 'ClipProcessor':
            blob = dn[p['ClipData']].get('ClipDataBlock')
            try:
                blk = clipblock.decode(blob)
            except Exception as ex:
                E('%s: driven-key block unreadable (%s)' % (tag, ex)); continue
            if dn[p['Input']]['Type'] != 'Float':
                E('%s: driver is a %s, must be a Float' % (tag, dn[p['Input']]['Type']))
            outs = p['Output'] if isinstance(p['Output'], list) else [p['Output']]
            for b in blk['binds']:
                slots = sum(len(blk['commands'][c]['params']) for c in b['commands'] if c < len(blk['commands']))
                if any(c >= len(blk['commands']) for c in b['commands']):
                    E('%s: bind references a missing command' % tag)
                npool = len(blk['pool']) // 4
                for r in b['animated']:
                    if r['target'] >= len(outs):
                        E('%s: binds output %d, processor has %d' % (tag, r['target'], len(outs))); continue
                    t = dn[outs[r['target']]]['Type']
                    if bool(r['flags'] & 0x20) != (t == 'Float'):
                        E('%s: output %s is a %s but the record is %s' % (tag, paths[dn[outs[r['target']]]['Header'] - 1],
                                                                       t, 'scalar' if r['flags'] & 0x20 else 'transform'))
                    for sw in r['subs']:
                        for k, operand in enumerate(sw[1:]):
                            src = (sw[0] >> (4 - 2 * k)) & 3 if len(sw) == 4 else (sw[0] >> 4) & 3
                            if src == 1 and operand >= slots:
                                E('%s: reads buffer slot %d of %d' % (tag, operand, slots))
                            elif src != 1 and operand >= npool:
                                E('%s: reads pool float %d of %d' % (tag, operand, npool))
    if errs:
        return errs, warns

    # connection maps
    cm = list(sn.get('ConnectionMapUsedDataNodes', []))
    if any(not 0 <= d < nd for d in cm):
        E('ConnectionMapUsedDataNodes out of range')
    if len(set(cm)) != len(cm):
        E('ConnectionMapUsedDataNodes has duplicates')
    used = set()
    for ls in sn.get('BaseDataNodeLinksPool', {}).values():
        ct, links = ls['ConnectionTemplate'], ls['DataNodeLinks']
        for d in links:
            if d >= len(cm):
                E('%s: link %d beyond ConnectionMapUsedDataNodes' % (ls['Name'], d))
            elif d >= 0:
                used.add(d)
        e = catalog.get(ct)
        if e is None:
            W('%s: template %s not in catalog (cannot check slots)' % (ls['Name'], ct))
            continue
        if len(links) > e['length']:
            W('%s: %d custom slot(s) beyond the shipping %d - the character .bent must define them '
              '(check with channels.validate_pair)' % (ls['Name'], len(links) - e['length'], e['length']))
            links = links[:e['length']]
        if len(links) != e['length']:
            revs = [r['length'] for r in e.get('other_revisions', [])]
            old = len(links) in revs
            (W if old and allow_old_templates else E)(
                '%s: %d slots, Arcade template %s has %d%s' % (ls['Name'], len(links), ct, e['length'],
                                                          ' (matches an older revision, e.g. the Wii build)' if old else ''))
            continue
        for s, d in enumerate(links):
            if d < 0 or d >= len(cm):
                continue
            slot = e['slots'][s]
            node = dn[cm[d]]
            path = paths[node['Header'] - 1]
            if slot is None:
                W('%s slot %d: no shipping rig binds this slot' % (ls['Name'], s))
            elif path != slot['channel']:
                E('%s slot %d expects %s, bound to %s' % (ls['Name'], s, slot['channel'], path))
            elif node['Type'] not in slot['types'] and not (
                    node['Type'] in TRANSFORM_TYPES and set(slot['types']) <= set(TRANSFORM_TYPES)):
                E('%s slot %d (%s) expects %s, node is %s' % (ls['Name'], s, path, slot['types'], node['Type']))
    if cm and used != set(range(len(cm))):
        W('ConnectionMapUsedDataNodes has entries no link uses')

    # rest positions
    ri, rd = sn.get('RestPositionIndices', b''), sn.get('RestPositionData', b'')
    if cm:
        if len(ri) != 4 * len(cm):
            E('RestPositionIndices is %d bytes, expected %d' % (len(ri), 4 * len(cm)))
        else:
            nf = len(rd) // 4 if len(rd) >= 4 else 0
            idx = struct.unpack('<%dH' % (2 * len(cm)), ri)
            for j in range(len(cm)):
                a, b = idx[2 * j], idx[2 * j + 1]
                if (a == NONE16) != (b == NONE16):
                    E('rest entry %d half-empty' % j)
                elif a != NONE16 and (a + 3 > nf or b + 4 > nf):
                    E('rest entry %d points past RestPositionData' % j)
                elif a != NONE16 and dn[cm[j]]['Type'] not in TRANSFORM_TYPES:
                    E('rest entry %d on non-transform %s' % (j, dn[cm[j]]['Type']))

    # network buffer
    nb = len(sn.get('NetworkBuffer', b''))
    for p in pn:
        for o in p.get('NetworkBufferOffsets', []):
            if p['Type'] == 'UberConstraintProcessor':
                if o != nb:
                    W('%s: UberConstraint buffer offset %d != buffer end %d' % (paths[p['Header'] - 1], o, nb))
            elif o >= nb:
                E('%s: NetworkBufferOffset %d past buffer (%d bytes)' % (paths[p['Header'] - 1], o, nb))
    return errs, warns


def validate_associations(tree, sn):
    """Scene-tree <-> network binding is by name: Bone/Transform association
    'Anchor|X' needs a transform data node 'Anchor/X/Transform'; a Material
    association 'Anchor|M' reads float params under 'Anchor/M/'."""
    errs, warns = [], []
    if sn.get('Type') != 'Scene' or 'AssociationPool' not in tree:
        return errs, warns
    try:
        paths = decode_paths(sn['HeaderStrings'], sn['HeaderStringIndices'], sn['HeaderLayout'])
        nodes = {paths[n['Header'] - 1]: n['Type'] for n in sn['DataNodePool'].values()}
    except Exception:
        return errs, warns       # header errors are reported by validate_subnetwork
    for a in tree['AssociationPool'].values():
        name = a.get('NodeName', '').replace('|', '/')
        if a.get('Type') in ('Bone', 'Transform'):
            q = name + '/Transform'
            if nodes.get(q) not in TRANSFORM_TYPES:
                errs.append('association %s (%s) has no transform node %s' % (a['NodeName'], a['Type'], q))
            lz = a.get('LazyDataNodeName')
            if lz and lz.replace('|', '/') not in nodes:
                errs.append('association %s: lazy group %s missing' % (a['NodeName'], lz))
        elif a.get('Type') == 'Material':
            if not any(k.startswith(name + '/') for k in nodes):
                warns.append('material association %s drives no network params' % a['NodeName'])
    return errs, warns


def validate_file(path, allow_old_templates=False):
    tree = octio.load(path)
    sn = tree['SubNetworkPool']['SubNetwork#0']
    e, w = validate_subnetwork(sn, allow_old_templates=allow_old_templates)
    e2, w2 = validate_associations(tree, sn)
    return e + e2, w + w2


if __name__ == '__main__':
    rc = 0
    old = '--allow-old-templates' in sys.argv
    for f in [a for a in sys.argv[1:] if not a.startswith('--')]:
        e, w = validate_file(f, old)
        print('%s: %d error(s), %d warning(s)' % (f, len(e), len(w)))
        for x in e: print('  ERROR', x)
        for x in w[:50]: print('  WARN ', x)
        if len(w) > 50: print('  ... %d more warnings' % (len(w) - 50))
        rc |= bool(e)
    sys.exit(rc)
