"""Minimal Python reader for Avalanche OCT files (port of offsetting/matryoshka decode).

    python octread.py file.oct > file.json   # same JSON shape as `offsetting oct decode`
"""
import struct, sys, json, base64

def decode(raw):
    if raw[:8] == bytes([0x29,0x76,0x01,0x45,0xcd,0xcc,0x8c,0x3f]): e = '<'
    elif raw[:8] == bytes([0x45,0x01,0x76,0x29,0x3f,0x8c,0xcc,0xcd]): e = '>'
    else: raise ValueError('bad magic')
    _, st_size, dt_size = struct.unpack_from(e+'III', raw, 8)
    p = 0x3C
    strings = raw[p:p+st_size].split(b'\0')
    strings = [s.decode('latin1') for s in strings]
    p += st_size
    end = p + dt_size
    def rint(p, n, signed):
        b = raw[p:p+n]
        v = int.from_bytes(b, 'little' if e=='<' else 'big', signed=signed)
        return v, p+n
    nodes = []
    while p < end:
        h = struct.unpack_from(e+'H', raw, p)[0]; p += 2
        typ = h & 3; hasname = (h>>2)&1; dtype = (h>>3)&7
        lens = ((h>>6)&3)+1; ints = ((h>>8)&3)+1; level = h>>10
        key = strings[struct.unpack_from(e+'H', raw, p)[0]]; p += 2
        if hasname:
            key += '#' + strings[struct.unpack_from(e+'H', raw, p)[0]]; p += 2
        if typ == 1: val = {}
        elif dtype == 1 and typ == 3:
            val = strings[struct.unpack_from(e+'H', raw, p)[0]]; p += 2
        elif dtype == 1 and typ == 2:
            n, p = rint(p, lens, False)
            val = [strings[i] for i in struct.unpack_from(e+'%dH'%n, raw, p)]; p += 2*n
        elif dtype == 2 and typ == 3:
            val = struct.unpack_from(e+'f', raw, p)[0]; p += 4
        elif dtype == 2 and typ == 2:
            n, p = rint(p, lens, False)
            val = list(struct.unpack_from(e+'%df'%n, raw, p)); p += 4*n
        elif dtype == 3 and typ == 3:
            val, p = rint(p, ints, True)
        elif dtype == 3 and typ == 2:
            n, p = rint(p, lens, False); val = []
            for _ in range(n):
                v, p = rint(p, ints, True); val.append(v)
        elif dtype == 4:
            n, p = rint(p, lens, False)
            val = bytes(raw[p:p+n]); p += n
        else:
            raise ValueError('unhandled %d %d' % (typ, dtype))
        nodes.append((level, key, val))
    # build tree
    root_level, rk, root = nodes[0]
    stack = [(root_level, root)]
    for level, key, val in nodes[1:]:
        while stack[-1][0] >= level: stack.pop()
        parent = stack[-1][1]
        k = key; i = 1
        while k in parent: k = '%s~%d' % (key, i); i += 1
        parent[k] = val
        if isinstance(val, dict): stack.append((level, val))
    return root, e

def load(path):
    return decode(open(path, 'rb').read())[0]

def dumps(tree, bin_preview=False):
    def conv(v):
        if isinstance(v, dict): return {k: conv(x) for k, x in v.items()}
        if isinstance(v, bytes): return ('<bin %d>' % len(v)) if bin_preview else 'base64:' + base64.b64encode(v).decode().rstrip('=')
        return v
    return json.dumps(conv(tree), indent=1)


def clip_block(raw):
    """ClipDataBlock bytes of a clip .oct (None if the file is not a clip)."""
    t = decode(raw)[0]
    sn = t.get('SubNetworkPool', {}).get('SubNetwork#0', {})
    if sn.get('Type') != 'Clip':
        return None
    for node in sn.get('DataNodePool', {}).values():
        if isinstance(node, dict) and isinstance(node.get('ClipDataBlock'), bytes):
            return node['ClipDataBlock']
    return None


def clip_templates(raw):
    """Template names of a clip's LayerBinds, in bind order."""
    t = decode(raw)[0]
    sn = t.get('SubNetworkPool', {}).get('SubNetwork#0', {})
    return [b.get('Name') for b in sn.get('LayerBindPool', {}).values()]


if __name__ == '__main__':
    print(dumps(load(sys.argv[1]), bin_preview='-b' in sys.argv))
