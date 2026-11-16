"""UberConstraintDataBlock codec (byte-exact on every shipping Cars 2 Arcade block).

Layout
    u32 op_count
    per op:  u32 word  = kind | size << 8 | flags << 16      (size includes this header)
             u32 input = index into the processor's Input list
             payload   (size - 8 bytes)
    pad with 0xEE to a multiple of 8 bytes
flags: 0x4000 last op, 0x2000 op carries an offset.

    kind  name           payload
    0x15  point          -                     copy position (Maya point)
    0x16  orient         -                     copy rotation (Maya orient)
    0x18  scale          -                     copy scale
    0x1a  point_offset   f32 offset[3]         point, maintain offset
    0x1b  orient_offset  f32 rotor[4] (x,y,z,w) orient, maintain offset
    0x17  aim            f32 aim[3], f32 up[3], u32 up_type
          up_type 8 = 'vector': processor has WorldUpVector (a Vector node)
          up_type 4 = 'object': processor has WorldUpPoint  (a Position node)

An op repeated with input 0, 1, ... is a multi-target (averaged) constraint,
e.g. Guido's sleeves and Finn's cheeks.
"""
import struct

KINDS = {0x15: 'point', 0x16: 'orient', 0x18: 'scale', 0x1a: 'point_offset',
         0x1b: 'orient_offset', 0x17: 'aim'}
CODES = {v: k for k, v in KINDS.items()}
UP_TYPES = {8: 'vector', 4: 'object'}
UP_CODES = {v: k for k, v in UP_TYPES.items()}
LAST, OFFSET = 0x4000, 0x2000


def decode(blob):
    count = struct.unpack_from('<I', blob, 0)[0]
    p, ops = 4, []
    for k in range(count):
        word, inp = struct.unpack_from('<II', blob, p)
        kind, size, flags = word & 0xFF, (word >> 8) & 0xFF, word >> 16
        if kind not in KINDS:
            raise ValueError('unknown UberConstraint op 0x%02x' % kind)
        op = {'op': KINDS[kind], 'input': inp}
        pay = blob[p + 8:p + size]
        if kind == 0x1a:
            op['offset'] = list(struct.unpack('<3f', pay))
        elif kind == 0x1b:
            op['rotor'] = list(struct.unpack('<4f', pay))
        elif kind == 0x17:
            v = struct.unpack('<6fI', pay)
            op['aim'], op['up'] = list(v[:3]), list(v[3:6])
            op['up_type'] = UP_TYPES.get(v[6], v[6])
        expect = (LAST if k == count - 1 else 0) | (OFFSET if kind in (0x1a, 0x1b) else 0)
        if flags != expect:
            op['flags'] = flags          # never happens in shipping data; kept for fidelity
        ops.append(op)
        p += size
    return ops


def encode(ops):
    out = bytearray(struct.pack('<I', len(ops)))
    for k, op in enumerate(ops):
        kind = CODES[op['op']]
        if kind == 0x1a:
            pay = struct.pack('<3f', *op.get('offset', (0.0, 0.0, 0.0)))
        elif kind == 0x1b:
            pay = struct.pack('<4f', *op.get('rotor', (0.0, 0.0, 0.0, 1.0)))
        elif kind == 0x17:
            ut = op.get('up_type', 'vector')
            pay = struct.pack('<6fI', *(list(op['aim']) + list(op.get('up', (0.0, 1.0, 0.0)))),
                              UP_CODES.get(ut, ut))
        else:
            pay = b''
        flags = op.get('flags', (LAST if k == len(ops) - 1 else 0) |
                       (OFFSET if kind in (0x1a, 0x1b) else 0))
        out += struct.pack('<II', kind | (8 + len(pay)) << 8 | flags << 16, op.get('input', 0)) + pay
    out += b'\xee' * (-len(out) % 8)
    return bytes(out)


def parent_ops(n_targets=1, scale=False):
    """Maya parent-style follow with no offset: orient + point (+ scale) per target,
    in the order shipping rigs use (all orients, all points, all scales)."""
    ops = [{'op': 'orient', 'input': i} for i in range(n_targets)]
    ops += [{'op': 'point', 'input': i} for i in range(n_targets)]
    if scale:
        ops += [{'op': 'scale', 'input': i} for i in range(n_targets)]
    return ops
