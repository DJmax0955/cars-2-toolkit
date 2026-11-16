"""c2oct tree -> the JSON shape of `offsetting oct decode` (what the Cars 3 location importer was written against), in
memory - no offsetting.exe, no decoded/ cache. Verified identical (json-equal) on every .oct of the Cars 3 zones
tested (see README). Differences handled:
- a key repeated in one container: c2oct 'Key', 'Key~1', ... -> offsetting one 'Key' holding a list (first position);
- 16-byte 'Uuid' fields (exactly that name) -> UUID text (bytes_le layout); other binary -> 'base64:...'; typed lists -> plain lists."""
import base64, uuid

try: from . import c2oct
except ImportError: import c2oct


def to_offsetting(v, key=''):
    if isinstance(v, dict):
        groups = {}
        for k, x in v.items():
            groups.setdefault(k.split('~', 1)[0], []).append(to_offsetting(x, k.split('~', 1)[0]))
        return {k: (xs[0] if len(xs) == 1 else xs) for k, xs in groups.items()}
    if isinstance(v, (bytes, bytearray)):
        if len(v) == 16 and key == 'Uuid': return str(uuid.UUID(bytes_le=bytes(v)))     # only 'Uuid' (MeridianNodeUUID stays base64)
        return 'base64:' + base64.b64encode(bytes(v)).decode()
    if isinstance(v, list): return [to_offsetting(x, key) for x in v]
    return v


def decode(data):
    """.oct bytes -> offsetting-style JSON object."""
    return to_offsetting(c2oct.decode(data)[0])
