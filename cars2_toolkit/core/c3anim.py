"""Cars 3 (Driven to Win) animation clips for the Cars 2 toolkit - a thin
wrapper over cars3_to_cars2.py (the Cars 3 -> Cars 2 clip converter, see
CARS3_ANIM_NOTES.md): convert in memory, write a binary Cars 2 clip .oct
(the game's own layout), or hand the Cars 2 clip to the animation importer.

    doc, info = convert('mack_fe_select_idle.oct')     # mode auto: frontend here
    write_oct(doc, 'mack_fe_select_idle.cars2.oct')
"""
import base64
import os

import c2oct
import cars3_to_cars2 as c32

MODES = ('auto', 'game', 'frontend')
SCALES = ('face', 'keep', 'drop')


# v2.0.56: the name rules live in cars3_to_cars2.py (shared with its command
# line and port_car.py; shrd_ -> shared_ built in).
load_name_map = c32.load_name_map
cars2_name = c32.cars2_clip_name


def load_part_ports(path):
    """A <name>.cars3_ports.json written by Export Cars 3 as Cars 2:
    {port: {template: [slot, name]}} - the Cars 3 parts (Fritter's blade)
    that got their own channels on the Cars 2 character."""
    import json
    with open(path, 'r', encoding='utf-8') as f:
        return {int(p): v for p, v in json.load(f).items()}


def convert(src, mode='auto', templates='', extra_map=None, scale='face', keep_rest=False,
            part_ports=None, cars2_names=True):
    """A Cars 3 clip (.oct or offsetting .json) -> (Cars 2 clip doc in
    offsetting's JSON shape, info {name, mode, templates, summary, dropped}).
    part_ports: {port: {template: [slot, name]}} (load_part_ports) - those
    ports go to the character's own channels instead of being dropped."""
    clip = c32.read_cars3_json(src)
    if mode == 'auto':
        mode = c32.detect_mode(src, clip['name'])
    wanted = c32.parse_mode(templates if mode == 'custom' else mode)
    portmap = c32.load_portmap(extra_map or None, wanted)
    for port, per in (part_ports or {}).items():
        entries = [(tm, int(v[0]), v[1], 'part') for tm, v in per.items() if tm in wanted]
        if entries:
            portmap[int(port)] = entries
    block2, used, report = c32.convert_block(clip['block'], portmap, scale, wanted, keep_rest)
    if not used:
        raise ValueError('no channel in this clip has a Cars 2 equivalent')
    problems = c32.verify_block(block2, used)
    if problems:
        raise ValueError('; '.join(problems))
    name = cars2_name(clip['name']) if cars2_names else clip['name']
    doc = c32.build_cars2_json(name, block2, used, clip['date'], clip['time'])
    return doc, {'name': name, 'cars3_name': clip['name'], 'mode': mode, 'templates': list(used),
                 'summary': c32.describe(report, None),
                 'dropped': sorted(report.dropped_ports)}


def to_tree(doc):
    """The converter's JSON-shaped clip -> the plugin's typed tree (binary .oct)."""
    sn = doc['SubNetworkPool']['SubNetwork#0']

    def val(v):
        if isinstance(v, str) and v.startswith('base64:'):
            r = v[len('base64:'):]
            return base64.b64decode(r + '=' * (-len(r) % 4))
        return v
    return {
        'SubNetworkPool': {'SubNetwork#0': {
            'Type': sn['Type'],
            'HeaderStrings': c2oct.StrList(sn['HeaderStrings']),
            'HeaderStringIndices': c2oct.IntList(sn['HeaderStringIndices']),
            'HeaderLayout': c2oct.IntList(sn['HeaderLayout']),
            'DataNodePool': {k: {kk: val(vv) for kk, vv in v.items()} for k, v in sn['DataNodePool'].items()},
            'LayerBindPool': {k: dict(v) for k, v in sn['LayerBindPool'].items()},
        }},
        'ExporterDate': doc.get('ExporterDate', 'Mar  4 2011'),
        'ExporterTime': doc.get('ExporterTime', '00:00:00'),
    }


def write_oct(doc, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c2oct.save(to_tree(doc), path)
    return path
