# Vendored from oct_import_double_eyes.py (Cars 2 model importer) for the
# Cars 2 Toolkit add-on. Changes are marked "Cars 2 Toolkit".
import os
import re
import sys
import base64
import math
import json
import struct
import bpy
from mathutils import Matrix, Vector, Quaternion

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

# ---------------------------------------------------------------------
# CONFIG -- edit these paths
# ---------------------------------------------------------------------
JSON_PATH = ""  # unused by the Cars 2 Toolkit
VBUF_PATH = ""
IBUF_PATH = ""
IMPORT_TEXTURES = True
# The source data's per-vertex normals are applied via
# mesh.normals_split_custom_set_from_vertices(), which has a long history of
# native (non-Python-catchable) crashes in Blender's custom-normals code on
# imported/merged meshes -- confirmed against two crash logs on this exact
# map, both dying inside blender::bke::mesh::mesh_normals_corner_custom_set.
# Left False by default: normals are instead computed safely with Blender's
# modern auto-smooth-by-angle system (bpy.ops.object.shade_auto_smooth),
# which never touches that code path. Set True only if you need the exact
# source normals and are working with small/simple files -- it's the same
# call that worked fine on the single-character import earlier.
USE_SOURCE_NORMALS = True
AUTO_SMOOTH_ANGLE_DEGREES = 60
# Model comes in lying down; rotate it upright around X. Flip the sign
# (-90) if it ends up upright-but-facing-wrong or upside down instead.
GLOBAL_ROTATE_X_DEGREES = 90
# Rebuild the game's animation control rig (the *_Ctrl / *_Proxy nodes in
# the SubNetwork) as extra non-deforming bones, so .oct animation clips have
# something real to drive. Needs the channel-map tables from the Cars 2
# animation add-on: they're picked up automatically if that add-on is
# enabled, otherwise point ANIM_ADDON_PATH at its .py file.
BUILD_CONTROL_RIG = True
# Cars 2 Toolkit: leave out rig bones that never move on their own (the
# game's pass-through group nodes: no clip channel, constraint, slider or
# driver uses them). Their children move up to the nearest kept bone with
# the same rest, so motion is unchanged; on McQueen this drops 37 bones and
# the deepest nesting goes from 30 to 18.
TIDY_RIG = True
# Cars 2 Toolkit: put each imported character in its own collection.
MODEL_COLLECTION = True
# Wheel spin: clips drive it through the Main_Ctrl/RotateFront and
# RotateRear values (degrees), which the rig turns into rotation of the
# *_SpinJoint bones. Flip to -1.0 if the wheels roll the wrong way.
WHEEL_SPIN_DIRECTION = 1.0
# Rebuild car_eyes_* materials as a node-based version of the game's eye
# renderer (transcribed from Cars 2 Arcade's sdaemon.exe), driven by the clip.
BUILD_EYE_SHADER = True
# Eye material surface. Flat-colour lids (diffuseColorA1 only): 1.0 / 1.0
# keeps the lid colour as saturated as the body instead of washed out by
# Principled's default sheen. Textured lids (diffuseMapA1): metallic 0,
# roughness 1.
EYE_METALLIC = 1.0
EYE_ROUGHNESS = 1.0
EYE_METALLIC_TEXTURED = 0.0
EYE_ROUGHNESS_TEXTURED = 1.0
ANIM_ADDON_PATH = r""
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Logging -- writes to a Blender Text datablock (Text Editor > "OctImportLog")
# instead of relying on a system console, which isn't available on every
# Blender build (Steam builds especially often lack the console toggle).
# ---------------------------------------------------------------------------

LOG_LINES = []


def log(msg):
    print(msg)
    LOG_LINES.append(str(msg))


def flush_log():
    name = "OctImportLog"
    text = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    text.clear()
    text.write('\n'.join(LOG_LINES))
    return text


# ---------------------------------------------------------------------------
# Pool helpers -- pools are JSON objects keyed like "Node#0", "Node#1", ...
# ---------------------------------------------------------------------------

_INDEX_RE = re.compile(r'#(\d+)$')


def pool_list(pool_dict):
    """Return a pool's entries as a plain list, ordered by the numeric
    suffix in their key (Node#0, Node#1, ...)."""
    if not pool_dict:
        return []
    def key_fn(item):
        m = _INDEX_RE.search(item[0])
        return int(m.group(1)) if m else 0
    return [v for k, v in sorted(pool_dict.items(), key=key_fn)]


# ---------------------------------------------------------------------------
# Vertex element decoding
# ---------------------------------------------------------------------------
# Confirmed against a real sample: Uv1(1)=2 floats, Position(2)=3 floats,
# BoneIndices(24)=4 ubytes, BlendWeights(25)=4 ubytes all pack exactly into
# the declared stream Width. Normal(3) is read as 3 floats at its offset;
# the observed 16-byte stride for Normal-only streams is presumed to be
# alignment padding rather than extra packed data.

ELEMENT_TYPES = {
    1: ('uv', 2, 'f'),
    2: ('pos', 3, 'f'),
    3: ('normal', 3, 'f'),
    24: ('boneidx', 4, 'B'),
    25: ('boneweight', 4, 'B'),
    # Arcade-build (e.g. mi_italy) element types -- unused by the PC/console
    # files tested earlier, so absent from the map above until now. Both
    # confirmed by reading raw bytes straight out of a .vbuf rather than
    # guessed from the type IDs alone:
    44: ('uv', 2, 'e'),      # Uv1 / lightmapUV / foamUV, 4 bytes for 2
                              # components. Decoding as half-float gives
                              # smooth, plausible tiling UVs (2.93, 2.96,
                              # 3.28, ...); decoding as normalized int16
                              # instead gives near-flat noise clustered at
                              # +-0.5 -- clearly the wrong format.
    45: ('normal', 4, 'e'),  # Normal / Tangent / Binormal, 8 bytes for 4
                              # components (xyz used, w is padding).
                              # Decoding as half-float turns one raw sample
                              # into exactly (0, 1, 0, 0) -- a perfect unit
                              # vector -- and every other sampled vertex
                              # comes out at magnitude ~1.0 as half-float
                              # vs. ~0.7 as normalized int16.
}

# Type 45 is reused for three different things -- Tangent, Normal, and
# Binormal can all appear as separate elements within the very same stream
# -- so, unlike every other type ID here, it can't be resolved to a single
# semantic 'kind' by ID alone; it has to be disambiguated by the element's
# own declared Name. Only the Normal element is kept (Tangent/Binormal
# aren't used by this importer -- Blender derives tangents itself at
# render/export time), so any other Name for this type is dropped.
AMBIGUOUS_TYPES = {45}


def resolve_element(type_id, name):
    """Look up an element's (kind, count, fmt), applying the Name-based
    disambiguation above for AMBIGUOUS_TYPES. Returns None for anything
    unrecognized or filtered out."""
    info = ELEMENT_TYPES.get(type_id)
    if info is None:
        return None
    if type_id in AMBIGUOUS_TYPES and (name or '').strip().lower() != 'normal':
        return None
    return info


def read_element(buf, base, offset, type_id, name=None):
    info = resolve_element(type_id, name)
    if info is None:
        return None
    _, count, fmt = info
    return struct.unpack_from('<%d%s' % (count, fmt), buf, base + offset)


_NUMPY_DTYPE = {'f': 'f4', 'B': 'u1', 'e': 'f2'} if HAVE_NUMPY else {}


def decode_stream_numpy(raw, stride, vertex_count, elements):
    """Vectorized replacement for the per-vertex/per-element Python loop in
    extract_node_geometry. Builds one structured numpy dtype describing all
    of a stream's elements at their declared byte offsets within the
    stride, then reads the whole stream in one shot. Returns a dict of
    kind -> (vertex_count, component_count) numpy array, or None if numpy
    isn't available or none of the elements are recognized."""
    if not HAVE_NUMPY:
        return None

    names, formats, offsets = [], [], []
    for el in elements:
        t = el.get('Type')
        off = el.get('Offset', 0)
        info = resolve_element(t, el.get('Name'))
        if info is None:
            continue
        kind, count, fmt = info
        if kind in names:
            # A duplicate kind within one stream shouldn't happen (each
            # observed layout has at most one), but numpy structured
            # dtypes reject duplicate field names outright, so guard it
            # defensively rather than let the whole stream fail to decode.
            continue
        names.append(kind)
        formats.append((_NUMPY_DTYPE[fmt], count))
        offsets.append(off)

    if not names:
        return None

    dtype = np.dtype({'names': names, 'formats': formats, 'offsets': offsets, 'itemsize': stride})
    arr = np.frombuffer(raw, dtype=dtype, count=vertex_count)
    return {kind: arr[kind] for kind in names}


def list_to_mat4(values, context_label=None):
    """The source data stores each matrix with translation in the last row
    (values[12:15]) and a [0,0,0,1] last COLUMN -- i.e. the row-vector/
    DirectX convention (v' = v @ M), confirmed against real data: e.g. one
    map node's raw values decode as uniform-scale 0.6706892 on the diagonal
    with translation (8.495702, 34.7752, -11.526696) sitting in the last
    row. mathutils/Blender instead expects the column-vector convention
    (v' = M @ v), with translation in the last COLUMN and [0,0,0,1] as the
    last row. Reading the values directly into Matrix(rows) as before and
    then doing `matrix @ Vector(p)` silently drops translation entirely --
    row 3 (where it lives) only ever feeds the discarded homogeneous `w`
    output of that multiplication, so every non-identity node's position
    offset was lost, even though rotation/scale looked fine. Transposing
    once here (moving translation into the last column) makes both the
    world-matrix chain multiply (parent_world @ local) and the per-vertex
    `matrix @ Vector(p)` apply correctly.

    Also falls back to identity (with a log warning, if a context_label is
    given) for a matrix with missing/non-numeric values rather than
    crashing the whole import over one bad node -- confirmed against
    tokyo_a: a stray editor Camera node ('persp11326', not a mesh, so it
    never mattered before this function started being called for every
    node rather than just mesh nodes) has its translation exported as
    literal None, None, None instead of numbers."""
    if not values or len(values) < 16:
        return Matrix.Identity(4)
    if any(not isinstance(v, (int, float)) for v in values[:16]):
        if context_label:
            log("WARNING: malformed LocalToParentMatrix (non-numeric "
                "value) on %s -- using identity instead." % context_label)
        return Matrix.Identity(4)
    rows = [values[i * 4:i * 4 + 4] for i in range(4)]
    return Matrix(rows).transposed()


# ---------------------------------------------------------------------------
# Load + index all pools up front
# ---------------------------------------------------------------------------

class OctData:
    def __init__(self, json_path):
        # Cars 2 Toolkit: also accepts the decoded tree directly (native .oct
        # reader), so no offsetting / JSON step is needed.
        if isinstance(json_path, dict):
            raw = json_path
        else:
            with open(json_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)

        self.textures = pool_list(raw.get('TexturePool'))
        self.materials = pool_list(raw.get('MaterialPool'))
        self.index_buffers = pool_list(raw.get('IndexBufferPool'))
        self.vertex_streams = pool_list(raw.get('VertexStreamPool'))
        self.index_streams = pool_list(raw.get('IndexStreamPool'))
        self.scene_components = pool_list(raw.get('SceneComponentPool'))
        self.nodes = pool_list(raw.get('SceneTreeNodePool'))
        self.sub_networks = raw.get('SubNetworkPool')

        self.bone_nodes = [(i, n) for i, n in enumerate(self.nodes) if n.get('Type') == 'Bone']
        self.mesh_nodes = [(i, n) for i, n in enumerate(self.nodes) if n.get('Type') in ('SubGeometryLit', 'SubGeometry')]

        self.bone_id_to_name = {n['BoneID']: n.get('NodeName', 'bone_%d' % n['BoneID'])
                                 for _, n in self.bone_nodes}

        self._world_matrix_cache = {}
        self._compute_world_matrices()

    def _compute_world_matrices(self):
        """Memoize each node's full accumulated LocalToParentMatrix chain,
        root down. A mesh node's own LocalToParentMatrix is only relative
        to its immediate parent -- on this data, meshes commonly sit 1-20
        scene-tree levels deep (Scene -> Transform -> Geometry ->
        SubGeometry, etc.), and it's very often the intermediate
        Transform/Geometry nodes -- not the mesh's own node -- that carry
        the real translation/rotation. Confirmed against st_andysroom: 280
        of 316 mesh nodes have a non-identity ancestor transform above
        their own node. Using only the leaf's own local matrix (as before)
        silently drops all of that, which is why map objects end up
        mispositioned/piled near the origin. Single-character imports
        didn't show this because every bone-node parent in that data
        happens to sit at identity."""
        def get_world(index, visiting):
            if index in self._world_matrix_cache:
                return self._world_matrix_cache[index]
            node = self.nodes[index]
            local = list_to_mat4(
                node.get('LocalToParentMatrix'),
                context_label='%s#%d (%s)' % (node.get('Type'), index, node.get('NodeName')))
            parents = node.get('ParentNodeReferences')
            if parents and parents[0] != index and parents[0] not in visiting:
                world = get_world(parents[0], visiting | {index}) @ local
            else:
                # No parent, self-referential, or a cycle -- treat as root.
                world = local
            self._world_matrix_cache[index] = world
            return world

        for i in range(len(self.nodes)):
            get_world(i, set())

    def world_matrix(self, node_index):
        return self._world_matrix_cache.get(node_index, Matrix.Identity(4))


# ---------------------------------------------------------------------------
# Textures / materials
#
# The .oct -> .oct.json extraction tool drops a sibling "<name>.textures"
# folder next to the .json, with each texture saved as "<TextureName>.dds"
# (matching the TexturePool entry's "Name" field -- the original in-game
# "Data"/"SourceFilePath" fields are not used). Only the diffuseMapA1
# texture entry is used per material.
# ---------------------------------------------------------------------------

def guess_character_name(json_path):
    # e.g. ".../buzzlightyear_alternate.oct.json" -> "buzzlightyear_alternate"
    base = os.path.basename(json_path)
    for suffix in ('.oct.json', '.json'):
        if base.lower().endswith(suffix):
            return base[:-len(suffix)]
    return os.path.splitext(base)[0]


def guess_textures_folder(json_path):
    # e.g. ".../buzzlightyear.oct.json" -> ".../buzzlightyear.oct.textures"
    # (only the trailing ".json" is swapped for ".textures" -- whatever
    # comes before that, ".oct" included, is kept as-is).
    root, _ext = os.path.splitext(json_path)
    return root + '.textures'


def load_diffuse_texture(tex_index, data, textures_folder, cache):
    if tex_index in cache:
        return cache[tex_index]
    if tex_index is None or not (0 <= tex_index < len(data.textures)):
        cache[tex_index] = None
        return None
    tex_data = data.textures[tex_index]
    tex_name = tex_data.get('Name', 'texture_%d' % tex_index)

    # The .textures folder mirrors the original game's relative folder
    # structure (e.g. "characters\<char>\maps\foo.dds"), given by the
    # "Data" field with its "file:" prefix stripped -- it is NOT flat,
    # so plain "<Name>.dds" at the folder root is wrong.
    data_field = tex_data.get('Data', '')
    if data_field.startswith('file:'):
        rel_path = data_field[len('file:'):].replace('\\', os.sep)
    else:
        rel_path = tex_name + '.dds'  # fallback if Data is missing/unexpected

    full_path = os.path.join(textures_folder, rel_path)
    if not os.path.isfile(full_path):
        log("WARNING: texture file not found: %s" % full_path)
        parent = os.path.dirname(full_path)
        try:
            actual = os.listdir(parent)
            log("         %s contains %d file(s), e.g.: %s" % (
                parent, len(actual), ', '.join(actual[:8])))
        except OSError as e:
            log("         and %s could not be listed: %s" % (parent, e))
        cache[tex_index] = None
        return None
    try:
        img = bpy.data.images.load(full_path, check_existing=True)
        img.name = tex_name
        cache[tex_index] = img
        log("Loaded texture: %s" % full_path)
        return img
    except Exception as e:
        log("WARNING: could not load texture %s: %s" % (full_path, e))
        cache[tex_index] = None
        return None


def hook_up_diffuse(mat, image):
    nt = mat.node_tree
    bsdf = nt.nodes.get('Principled BSDF')
    if not bsdf:
        return
    tex_node = nt.nodes.new('ShaderNodeTexImage')
    tex_node.image = image
    nt.links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])


def load_materials(data, textures_folder, import_textures):
    mats = []
    tex_cache = {}
    hooked = 0
    no_diffuse_entry = 0
    for mat_data in data.materials:
        name = mat_data.get('Name', 'Material')
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        if import_textures:
            tex_entries = pool_list(mat_data.get('TextureEntries'))
            diffuse_entry = next((te for te in tex_entries if te.get('Usage') == 'diffuseMapA1'), None)
            if diffuse_entry is None:
                no_diffuse_entry += 1
            else:
                img = load_diffuse_texture(diffuse_entry.get('TextureReference'), data, textures_folder, tex_cache)
                if img:
                    hook_up_diffuse(mat, img)
                    hooked += 1
        mats.append(mat)
    if import_textures:
        log("Materials: %d total, %d with a diffuse texture attached, %d with no diffuseMapA1 entry at all." % (
            len(mats), hooked, no_diffuse_entry))
    return mats


# ---------------------------------------------------------------------------
# Armature
#
# Two layers, mirroring how the game itself is rigged:
#
#   * Skin bones -- the SceneTreeNode "Bone" entries the mesh is weighted to
#     (Main, Body, Jaw, TireFLeft_centerJoint, ...). Their rest pose comes
#     from BoneToModelMatrix. The earlier version put every one of them at
#     the origin, so every rotation an animation applied pivoted around the
#     car's origin instead of the jaw hinge / wheel hub / etc.
#
#   * Control bones -- the animation rig from the SubNetwork (Main_Ctrl,
#     Body2_Ctrl, FLeft_Center_Ctrl, Jaw_Proxy, Jaw_Ctrl, ...). Clips never
#     animate the skin bones directly: they animate these controls, and each
#     skin bone copies its control's world transform through an
#     UberConstraint. Here that copy is done by parenting the skin bone to
#     its control (their rests coincide exactly), which is equivalent.
#
# Without the control layer, channels like Body2 / Body3 / FrontTires /
# FLeft_Center have no bone to land on, and the lower lip doesn't follow the
# jaw (in the game its control sits under Jaw_Ctrl; in the skin hierarchy
# it's a sibling of the jaw).
# ---------------------------------------------------------------------------

_XFORM_TYPES = ('Transform', 'Dilation')
_SLIDER_PREFIXES = ('vsm_', 'Mth_')


def _rotor_to_matrix(rotor):
    """Rotors are stored (x, y, z, w) and, relative to mathutils, conjugated
    -- confirmed against the brow controls, whose rotors only reproduce the
    skin brows' BoneToModelMatrix orientation when conjugated."""
    x, y, z, w = rotor
    return Quaternion((w, -x, -y, -z)).to_matrix().to_4x4()


def _data_node_local(node):
    d = node.get('Data')
    if not isinstance(d, dict):
        return Matrix.Identity(4)
    t = d.get('Translation') or (0.0, 0.0, 0.0)
    rot = Matrix.Identity(4)
    if 'Orientation#Rotor' in d:
        rot = _rotor_to_matrix(d['Orientation#Rotor'])
    proxy = d.get('Orientation#OrientProxy')
    if isinstance(proxy, dict):
        rot = (_rotor_to_matrix(proxy.get('ParentProxyToParent#Rotor', (0, 0, 0, 1)))
               @ _rotor_to_matrix(proxy.get('LocalToLocalProxy#Rotor', (0, 0, 0, 1))))
    sc = d.get('Scale') or (1.0, 1.0, 1.0)
    scale = Matrix.Diagonal((sc[0], sc[1], sc[2], 1.0))
    return Matrix.Translation(Vector(t)) @ rot @ scale


# Channel index -> control name per ConnectionTemplate, copied from the
# Cars 2 animation add-on (captured in-game, not stored in any file). Kept
# here so the control bones get their real names even when the add-on isn't
# enabled at import time -- without them every control came out as rig_N,
# the clips could only find the skin bones, and FrontEnd, the wheels and
# the Body2/Body3 chain stopped responding.
_EMBEDDED_CHANNEL_MAPS = {
    "car_body": {
        0: "BackEnd", 1: "BackTires", 2: "BLeft_bttm", 3: "BLeft_Center",
        4: "Body2", 5: "Body3", 6: "Body_Baxle", 7: "Body", 8: "BRight_bttm",
        9: "BRight_Center", 10: "FLeft_bttm", 11: "FLeft_Center",
        12: "FRight_bttm", 13: "FRight_Center", 14: "FrontEnd", 15: "FrontTires",
        16: "FX_Marker_Battery_Center", 17: "FX_Marker_Battery_Left",
        18: "FX_Marker_Battery_Right", 19: "FX_Marker_Bumper",
        20: "FX_Marker_Exhaust_Left1", 21: "FX_Marker_Exhaust_Left2",
        22: "FX_Marker_Exhaust_Right1", 23: "FX_Marker_Exhaust_Right2",
        24: "FX_Marker_Headlight_Left", 25: "FX_Marker_Headlight_Right",
        26: "FX_Marker_MachineGun_Left", 27: "FX_Marker_MachineGun_Right",
        28: "FX_Marker_SkateJack", 29: "FX_Marker_SpareTire",
        30: "FX_Marker_WGPCam", 31: "Main_Baxle", 32: "Main", 33: "MainOrient",
        34: "TCable_Pulley", 35: "TowCrane", 36: "TowCraneBase",
    },
    "car_bodyblend": {
        0: "BackTires", 1: "BLeft_bttm", 2: "BLeft_Center", 3: "Body2",
        4: "Body3", 5: "Body", 6: "BRight_bttm", 7: "BRight_Center",
        8: "FLeft_bttm", 9: "FLeft_Center", 10: "FRight_bttm",
        11: "FRight_Center", 12: "FrontEnd", 13: "FrontTires", 14: "Main",
        15: "MainOrient",
    },
    "car_choblender": {
        0: "Body3", 1: "MainOrient",
    },
    "car_choreography": {
        0: "KinematicState", 2: "BackEnd", 3: "BackTires", 4: "BLeft_bttm",
        5: "BLeft_Center", 6: "Body2", 7: "Body3", 8: "Body_Baxle", 9: "Body",
        10: "BRight_bttm", 11: "BRight_Center", 12: "BrowCenter",
        13: "BrowCornerLeft", 14: "BrowCornerRight", 15: "BrowMidLeft",
        16: "BrowMidRight", 17: "CheekLowerLeft", 18: "CheekLowerRight",
        19: "CheekUpperLeft", 20: "CheekUpperRight", 21: "ControlHandLeft",
        22: "ControlHandRight", 23: "Eyes_Ctrl/BTM_CNTR_Notch",
        24: "Eyes_Ctrl/BTM_L_Bow", 25: "Eyes_Ctrl/BTM_L_Bow_Angle",
        26: "Eyes_Ctrl/BTM_L_Bow_InOut", 27: "Eyes_Ctrl/BTM_L_Edge",
        28: "Eyes_Ctrl/BTM_L_Edge_Angle", 29: "Eyes_Ctrl/BTM_L_Notch",
        30: "Eyes_Ctrl/BTM_L_Notch_Angle", 31: "Eyes_Ctrl/BTM_R_Bow",
        32: "Eyes_Ctrl/BTM_R_Bow_Angle", 33: "Eyes_Ctrl/BTM_R_Bow_InOut",
        34: "Eyes_Ctrl/BTM_R_Edge", 35: "Eyes_Ctrl/BTM_R_Edge_Angle",
        36: "Eyes_Ctrl/BTM_R_Notch", 37: "Eyes_Ctrl/BTM_R_Notch_Angle",
        38: "Eyes_Ctrl/EyeLeft", 39: "Eyes_Ctrl/EyeLeftVertical",
        40: "Eyes_Ctrl/EyeRight", 41: "Eyes_Ctrl/Monocle",
        42: "Eyes_Ctrl/MonocleInOut", 43: "Eyes_Ctrl/TOP_CNTR_Notch",
        44: "Eyes_Ctrl/TOP_L_Bow", 45: "Eyes_Ctrl/TOP_L_Bow_Angle",
        46: "Eyes_Ctrl/TOP_L_Bow_InOut", 47: "Eyes_Ctrl/TOP_L_Edge",
        48: "Eyes_Ctrl/TOP_L_Edge_Angle", 49: "Eyes_Ctrl/TOP_L_Notch",
        50: "Eyes_Ctrl/TOP_L_Notch_Angle", 51: "Eyes_Ctrl/TOP_L_Notch_InOut",
        52: "Eyes_Ctrl/TOP_R_Bow", 53: "Eyes_Ctrl/TOP_R_Bow_Angle",
        54: "Eyes_Ctrl/TOP_R_Bow_InOut", 55: "Eyes_Ctrl/TOP_R_Edge",
        56: "Eyes_Ctrl/TOP_R_Edge_Angle", 57: "Eyes_Ctrl/TOP_R_Notch",
        58: "Eyes_Ctrl/TOP_R_Notch_Angle", 59: "Eyes_Ctrl/TOP_R_Notch_InOut",
        60: "Eyes", 61: "Eyes_Frame_Proxy", 62: "FLeft_bttm", 63: "FLeft_Center",
        64: "FRight_bttm", 65: "FRight_Center", 66: "FrontEnd", 67: "FrontTires",
        68: "Jaw", 69: "LipLowerCenter", 70: "LipLowerCornerLeft",
        71: "LipLowerCornerRight", 72: "LipLowerMidLeft", 73: "LipLowerMidRight",
        74: "LipUpperCenter", 75: "LipUpperCornerLeft",
        76: "LipUpperCornerRight", 77: "LipUpperMidLeft", 78: "LipUpperMidRight",
        79: "Main_Baxle", 80: "Main_Ctrl/RotateFront",
        81: "Main_Ctrl/RotateRear", 82: "Main", 83: "MainOrient",
        84: "Mouth_Ctrl/Mth_Corner_L", 85: "Mouth_Ctrl/Mth_Corner_R",
        86: "Mouth_Ctrl/Mth_Expand_L", 87: "Mouth_Ctrl/Mth_Expand_R",
        88: "Mouth_Ctrl/Mth_Offest", 89: "Mouth_Ctrl/Mth_Offset",
        90: "Mouth_Ctrl/vsm_AA", 91: "Mouth_Ctrl/vsm_AH",
        92: "Mouth_Ctrl/vsm_AO", 93: "Mouth_Ctrl/vsm_CH",
        94: "Mouth_Ctrl/vsm_EH", 95: "Mouth_Ctrl/vsm_ER",
        96: "Mouth_Ctrl/vsm_EY", 97: "Mouth_Ctrl/vsm_F", 98: "Mouth_Ctrl/vsm_IH",
        99: "Mouth_Ctrl/vsm_L", 100: "Mouth_Ctrl/vsm_M", 101: "Mouth_Ctrl/vsm_N",
        102: "Mouth_Ctrl/vsm_OY", 103: "Mouth_Ctrl/vsm_W",
        104: "Mouth_Ctrl/vsm_Y", 105: "TCable_Pulley", 106: "TeethLower",
        107: "TeethUpper", 108: "Tongue1", 109: "Tongue2", 110: "TongueRoot",
        111: "TowCrane", 112: "TowCraneBase",
    },
    "car_eyelids": {  # same output list as car_facial; only the .bent filter masks differ
        0: "BrowCenter", 1: "BrowCornerLeft", 2: "BrowCornerRight",
        3: "BrowMidLeft", 4: "BrowMidRight", 5: "CheekLowerLeft",
        6: "CheekLowerRight", 7: "CheekUpperLeft", 8: "CheekUpperRight",
        9: "Eyes_Ctrl/BTM_CNTR_Notch", 10: "Eyes_Ctrl/BTM_L_Bow",
        11: "Eyes_Ctrl/BTM_L_Bow_Angle", 12: "Eyes_Ctrl/BTM_L_Bow_InOut",
        13: "Eyes_Ctrl/BTM_L_Edge", 14: "Eyes_Ctrl/BTM_L_Edge_Angle",
        15: "Eyes_Ctrl/BTM_L_Notch", 16: "Eyes_Ctrl/BTM_L_Notch_Angle",
        17: "Eyes_Ctrl/BTM_R_Bow", 18: "Eyes_Ctrl/BTM_R_Bow_Angle",
        19: "Eyes_Ctrl/BTM_R_Bow_InOut", 20: "Eyes_Ctrl/BTM_R_Edge",
        21: "Eyes_Ctrl/BTM_R_Edge_Angle", 22: "Eyes_Ctrl/BTM_R_Notch",
        23: "Eyes_Ctrl/BTM_R_Notch_Angle", 24: "Eyes_Ctrl/EyeLeft",
        25: "Eyes_Ctrl/EyeLeftVertical", 26: "Eyes_Ctrl/EyeRight",
        27: "Eyes_Ctrl/Monocle", 28: "Eyes_Ctrl/MonocleInOut",
        29: "Eyes_Ctrl/TOP_CNTR_Notch", 30: "Eyes_Ctrl/TOP_L_Bow",
        31: "Eyes_Ctrl/TOP_L_Bow_Angle", 32: "Eyes_Ctrl/TOP_L_Bow_InOut",
        33: "Eyes_Ctrl/TOP_L_Edge", 34: "Eyes_Ctrl/TOP_L_Edge_Angle",
        35: "Eyes_Ctrl/TOP_L_Notch", 36: "Eyes_Ctrl/TOP_L_Notch_Angle",
        37: "Eyes_Ctrl/TOP_L_Notch_InOut", 38: "Eyes_Ctrl/TOP_R_Bow",
        39: "Eyes_Ctrl/TOP_R_Bow_Angle", 40: "Eyes_Ctrl/TOP_R_Bow_InOut",
        41: "Eyes_Ctrl/TOP_R_Edge", 42: "Eyes_Ctrl/TOP_R_Edge_Angle",
        43: "Eyes_Ctrl/TOP_R_Notch", 44: "Eyes_Ctrl/TOP_R_Notch_Angle",
        45: "Eyes_Ctrl/TOP_R_Notch_InOut", 46: "Eyes", 47: "Jaw",
        48: "LipLowerCenter", 49: "LipLowerCornerLeft",
        50: "LipLowerCornerRight", 51: "LipLowerMidLeft", 52: "LipLowerMidRight",
        53: "LipUpperCenter", 54: "LipUpperCornerLeft",
        55: "LipUpperCornerRight", 56: "LipUpperMidLeft", 57: "LipUpperMidRight",
        58: "Mouth_Ctrl/Mth_Corner_L", 59: "Mouth_Ctrl/Mth_Corner_R",
        60: "Mouth_Ctrl/Mth_Expand_L", 61: "Mouth_Ctrl/Mth_Expand_R",
        62: "Mouth_Ctrl/Mth_Offset", 63: "TeethLower", 64: "TeethUpper",
        65: "Tongue1", 66: "Tongue2", 67: "TongueRoot",
    },
    "car_eyes": {
        0: "Eyes_Ctrl/EyeLeft", 1: "Eyes_Ctrl/EyeLeftVertical",
        2: "Eyes_Ctrl/EyeRight", 3: "Eyes",
    },
    "car_facial": {
        0: "BrowCenter", 1: "BrowCornerLeft", 2: "BrowCornerRight",
        3: "BrowMidLeft", 4: "BrowMidRight", 5: "CheekLowerLeft",
        6: "CheekLowerRight", 7: "CheekUpperLeft", 8: "CheekUpperRight",
        9: "Eyes_Ctrl/BTM_CNTR_Notch", 10: "Eyes_Ctrl/BTM_L_Bow",
        11: "Eyes_Ctrl/BTM_L_Bow_Angle", 12: "Eyes_Ctrl/BTM_L_Bow_InOut",
        13: "Eyes_Ctrl/BTM_L_Edge", 14: "Eyes_Ctrl/BTM_L_Edge_Angle",
        15: "Eyes_Ctrl/BTM_L_Notch", 16: "Eyes_Ctrl/BTM_L_Notch_Angle",
        17: "Eyes_Ctrl/BTM_R_Bow", 18: "Eyes_Ctrl/BTM_R_Bow_Angle",
        19: "Eyes_Ctrl/BTM_R_Bow_InOut", 20: "Eyes_Ctrl/BTM_R_Edge",
        21: "Eyes_Ctrl/BTM_R_Edge_Angle", 22: "Eyes_Ctrl/BTM_R_Notch",
        23: "Eyes_Ctrl/BTM_R_Notch_Angle", 24: "Eyes_Ctrl/EyeLeft",
        25: "Eyes_Ctrl/EyeLeftVertical", 26: "Eyes_Ctrl/EyeRight",
        27: "Eyes_Ctrl/Monocle", 28: "Eyes_Ctrl/MonocleInOut",
        29: "Eyes_Ctrl/TOP_CNTR_Notch", 30: "Eyes_Ctrl/TOP_L_Bow",
        31: "Eyes_Ctrl/TOP_L_Bow_Angle", 32: "Eyes_Ctrl/TOP_L_Bow_InOut",
        33: "Eyes_Ctrl/TOP_L_Edge", 34: "Eyes_Ctrl/TOP_L_Edge_Angle",
        35: "Eyes_Ctrl/TOP_L_Notch", 36: "Eyes_Ctrl/TOP_L_Notch_Angle",
        37: "Eyes_Ctrl/TOP_L_Notch_InOut", 38: "Eyes_Ctrl/TOP_R_Bow",
        39: "Eyes_Ctrl/TOP_R_Bow_Angle", 40: "Eyes_Ctrl/TOP_R_Bow_InOut",
        41: "Eyes_Ctrl/TOP_R_Edge", 42: "Eyes_Ctrl/TOP_R_Edge_Angle",
        43: "Eyes_Ctrl/TOP_R_Notch", 44: "Eyes_Ctrl/TOP_R_Notch_Angle",
        45: "Eyes_Ctrl/TOP_R_Notch_InOut", 46: "Eyes", 47: "Jaw",
        48: "LipLowerCenter", 49: "LipLowerCornerLeft",
        50: "LipLowerCornerRight", 51: "LipLowerMidLeft", 52: "LipLowerMidRight",
        53: "LipUpperCenter", 54: "LipUpperCornerLeft",
        55: "LipUpperCornerRight", 56: "LipUpperMidLeft", 57: "LipUpperMidRight",
        58: "Mouth_Ctrl/Mth_Corner_L", 59: "Mouth_Ctrl/Mth_Corner_R",
        60: "Mouth_Ctrl/Mth_Expand_L", 61: "Mouth_Ctrl/Mth_Expand_R",
        62: "Mouth_Ctrl/Mth_Offset", 63: "TeethLower", 64: "TeethUpper",
        65: "Tongue1", 66: "Tongue2", 67: "TongueRoot",
    },
    "car_frontend_frontend": {
        0: "BackEnd", 1: "BackTires", 2: "BLeft_bttm", 3: "BLeft_Center",
        4: "Body2", 5: "Body3", 6: "Body_Baxle", 7: "Body", 8: "BRight_bttm",
        9: "BRight_Center", 10: "BrowCenter", 11: "BrowCornerLeft",
        12: "BrowCornerRight", 13: "BrowMidLeft", 14: "BrowMidRight",
        15: "CheekLowerLeft", 16: "CheekLowerRight", 17: "CheekUpperLeft",
        18: "CheekUpperRight", 19: "ControlArmLeft", 20: "ControlArmRight",
        21: "ControlBrowCenter", 22: "ControlBrowCornerLeft",
        23: "ControlBrowCornerRight", 24: "ControlBrowMidLeft",
        25: "ControlBrowMidRight", 26: "ControlHandLeft", 27: "ControlHandRight",
        28: "ControlStacheLeft", 29: "ControlStacheRight",
        30: "Eyes_Ctrl/BTM_CNTR_Notch", 31: "Eyes_Ctrl/BTM_L_Bow",
        32: "Eyes_Ctrl/BTM_L_Bow_Angle", 33: "Eyes_Ctrl/BTM_L_Bow_InOut",
        34: "Eyes_Ctrl/BTM_L_Edge", 35: "Eyes_Ctrl/BTM_L_Edge_Angle",
        36: "Eyes_Ctrl/BTM_L_Notch", 37: "Eyes_Ctrl/BTM_L_Notch_Angle",
        38: "Eyes_Ctrl/BTM_R_Bow", 39: "Eyes_Ctrl/BTM_R_Bow_Angle",
        40: "Eyes_Ctrl/BTM_R_Bow_InOut", 41: "Eyes_Ctrl/BTM_R_Edge",
        42: "Eyes_Ctrl/BTM_R_Edge_Angle", 43: "Eyes_Ctrl/BTM_R_Notch",
        44: "Eyes_Ctrl/BTM_R_Notch_Angle", 45: "Eyes_Ctrl/EyeLeft",
        46: "Eyes_Ctrl/EyeLeftVertical", 47: "Eyes_Ctrl/EyeRight",
        48: "Eyes_Ctrl/Monocle", 49: "Eyes_Ctrl/MonocleInOut",
        50: "Eyes_Ctrl/TOP_CNTR_Notch", 51: "Eyes_Ctrl/TOP_L_Bow",
        52: "Eyes_Ctrl/TOP_L_Bow_Angle", 53: "Eyes_Ctrl/TOP_L_Bow_InOut",
        54: "Eyes_Ctrl/TOP_L_Edge", 55: "Eyes_Ctrl/TOP_L_Edge_Angle",
        56: "Eyes_Ctrl/TOP_L_Notch", 57: "Eyes_Ctrl/TOP_L_Notch_Angle",
        58: "Eyes_Ctrl/TOP_L_Notch_InOut", 59: "Eyes_Ctrl/TOP_R_Bow",
        60: "Eyes_Ctrl/TOP_R_Bow_Angle", 61: "Eyes_Ctrl/TOP_R_Bow_InOut",
        62: "Eyes_Ctrl/TOP_R_Edge", 63: "Eyes_Ctrl/TOP_R_Edge_Angle",
        64: "Eyes_Ctrl/TOP_R_Notch", 65: "Eyes_Ctrl/TOP_R_Notch_Angle",
        66: "Eyes_Ctrl/TOP_R_Notch_InOut", 67: "Eyes", 68: "FLeft_bttm",
        69: "FLeft_Center", 70: "FRight_bttm", 71: "FRight_Center",
        72: "FrontEnd", 73: "FrontTires", 74: "Jaw", 75: "LipLowerCenter",
        76: "LipLowerCornerLeft", 77: "LipLowerCornerRight",
        78: "LipLowerMidLeft", 79: "LipLowerMidRight", 80: "LipUpperCenter",
        81: "LipUpperCornerLeft", 82: "LipUpperCornerRight",
        83: "LipUpperMidLeft", 84: "LipUpperMidRight", 85: "Main_Baxle",
        86: "Main_Ctrl/RotateFront", 87: "Main_Ctrl/RotateRear", 88: "Main",
        89: "Main_CtrlGrp", 90: "MainOrient", 91: "Mouth_Ctrl/Mth_Corner_L",
        92: "Mouth_Ctrl/Mth_Corner_R", 93: "Mouth_Ctrl/Mth_Expand_L",
        94: "Mouth_Ctrl/Mth_Expand_R", 95: "Mouth_Ctrl/Mth_Offest",
        96: "Mouth_Ctrl/Mth_Offset", 97: "Mouth_Ctrl/vsm_AA",
        98: "Mouth_Ctrl/vsm_IH", 99: "Mouth_Ctrl/vsm_M",
        100: "SideMirrorMountLeft", 101: "SideMirrorMountRight",
        102: "TCable_Pulley", 103: "TeethLower", 104: "TeethUpper",
        105: "Tongue1", 106: "Tongue2", 107: "TongueRoot", 108: "TowCrane",
        109: "TowCraneBase",
    },
    "car_frontend_mouth": {
        0: "BrowCenter", 1: "BrowCornerLeft", 2: "BrowCornerRight",
        3: "BrowMidLeft", 4: "BrowMidRight", 5: "CheekLowerLeft",
        6: "CheekLowerRight", 7: "CheekUpperLeft", 8: "CheekUpperRight",
        9: "Jaw", 10: "LipLowerCenter", 11: "LipLowerCornerLeft",
        12: "LipLowerCornerRight", 13: "LipLowerMidLeft", 14: "LipLowerMidRight",
        15: "LipUpperCenter", 16: "LipUpperCornerLeft",
        17: "LipUpperCornerRight", 18: "LipUpperMidLeft", 19: "LipUpperMidRight",
        20: "Mouth_Ctrl/Mth_Corner_L", 21: "Mouth_Ctrl/Mth_Corner_R",
        22: "Mouth_Ctrl/Mth_Expand_L", 23: "Mouth_Ctrl/Mth_Expand_R",
        24: "Mouth_Ctrl/Mth_Offest", 25: "Mouth_Ctrl/Mth_Offset",
        26: "Mouth_Ctrl/vsm_AA", 27: "Mouth_Ctrl/vsm_IH", 28: "Mouth_Ctrl/vsm_M",
        29: "TeethLower", 30: "TeethUpper", 31: "Tongue1", 32: "Tongue2",
        33: "TongueRoot",
    },
    "car_leanclipset": {
        0: "BackEnd", 1: "BackTires", 2: "BLeft_bttm", 3: "BLeft_Center",
        4: "Body2", 5: "Body3", 6: "Body", 7: "BRight_bttm", 8: "BRight_Center",
        9: "ControlLeftBarrels", 10: "ControlRightBarrels", 11: "FLeft_bttm",
        12: "FLeft_Center", 13: "FRight_bttm", 14: "FRight_Center",
        15: "FrontEnd", 16: "FrontTires", 17: "Main", 18: "MainOrient",
        19: "TowCraneBase",
    },
    "car_mouth": {
        0: "CheekLowerLeft", 1: "CheekLowerRight", 2: "CheekUpperLeft",
        3: "CheekUpperRight", 4: "Jaw", 5: "LipLowerCenter",
        6: "LipLowerCornerLeft", 7: "LipLowerCornerRight", 8: "LipLowerMidLeft",
        9: "LipLowerMidRight", 10: "LipUpperCenter", 11: "LipUpperCornerLeft",
        12: "LipUpperCornerRight", 13: "LipUpperMidLeft", 14: "LipUpperMidRight",
        15: "Mouth_Ctrl/Mth_Corner_L", 16: "Mouth_Ctrl/Mth_Corner_R",
        17: "Mouth_Ctrl/Mth_Expand_L", 18: "Mouth_Ctrl/Mth_Expand_R",
        19: "Mouth_Ctrl/Mth_Offset", 20: "TeethLower", 21: "TeethUpper",
        22: "Tongue1", 23: "Tongue2", 24: "TongueRoot",
    },
    "car_rattles": {
        0: "BackEnd", 1: "BackTires", 2: "BLeft_bttm", 3: "BLeft_Center",
        4: "Body2", 5: "Body3", 6: "Body", 7: "BRight_bttm", 8: "BRight_Center",
        9: "BrowCenter", 10: "BrowCornerLeft", 11: "BrowCornerRight",
        12: "BrowMidLeft", 13: "BrowMidRight", 14: "CheekLowerLeft",
        15: "CheekLowerRight", 16: "CheekUpperLeft", 17: "CheekUpperRight",
        18: "FLeft_bttm", 19: "FLeft_Center", 20: "FRight_bttm",
        21: "FRight_Center", 22: "FrontEnd", 23: "FrontTires", 24: "Jaw",
        25: "LipLowerCenter", 26: "LipLowerCornerLeft",
        27: "LipLowerCornerRight", 28: "LipLowerMidLeft", 29: "LipLowerMidRight",
        30: "LipUpperCenter", 31: "LipUpperCornerLeft",
        32: "LipUpperCornerRight", 33: "LipUpperMidLeft", 34: "LipUpperMidRight",
        35: "Main_Ctrl/RotateFront", 36: "Main_Ctrl/RotateRear", 37: "Main",
        38: "MainOrient", 39: "TeethLower", 40: "TeethUpper", 41: "Tongue1",
        42: "Tongue2", 43: "TongueRoot",
    },
}


def _find_channel_maps():
    """BUILTIN_CHANNEL_MAPS from the Cars 2 animation add-on if it's loaded
    (or at ANIM_ADDON_PATH), else the copy embedded in this script."""
    for mod in list(sys.modules.values()):
        maps = getattr(mod, 'BUILTIN_CHANNEL_MAPS', None)
        if isinstance(maps, dict) and 'car_body' in maps:
            return maps
    if ANIM_ADDON_PATH and os.path.isfile(ANIM_ADDON_PATH):
        src = open(ANIM_ADDON_PATH, 'r', encoding='utf-8').read()
        start = src.find('BUILTIN_CHANNEL_MAPS = {')
        if start >= 0:
            # The table is a plain dict literal; evaluate just that
            # statement rather than executing the whole add-on.
            depth, i = 0, src.index('{', start)
            for j in range(i, len(src)):
                if src[j] == '{':
                    depth += 1
                elif src[j] == '}':
                    depth -= 1
                    if depth == 0:
                        import ast
                        return ast.literal_eval(src[i:j + 1])
    return _EMBEDDED_CHANNEL_MAPS


def _decode_value_clip(blob):
    """Decode the rig's small internal ClipDataBlocks (the ones a
    ClipProcessor uses to turn one float into rotations, e.g. RotateFront
    -> wheel spin). Returns (linear_slope, intercept, [(output, axis)]) or
    None when the block isn't a single linear curve bound to Euler axes.

    Layout, from McQueen's spin blocks:
      f32 inStart, f32 inEnd, u16 nBinds, u16 nCmds, u32 poolOffset,
      u32 offsets[nBinds + nCmds]
      command: u32 word (opcode in bits 6-9, channels 10-19, keys 20-31),
               f32 tStart, f32 tEnd, per channel 4 f32 (pre, post, bias,
               scale), key times (u16 normalised for opcode 0, f32 for
               opcode 2), then 4 i16 Bezier deltas per key per channel.
      bind block: same record format as the animation clips' binds.
    """
    u16 = lambda o: struct.unpack_from('<H', blob, o)[0]
    u32 = lambda o: struct.unpack_from('<I', blob, o)[0]
    f32 = lambda o: struct.unpack_from('<f', blob, o)[0]
    n_binds, n_cmds = u16(8), u16(10)
    if n_binds != 1 or n_cmds != 1:
        return None
    bind_off, cmd_off = u32(16), u32(20)

    w = u32(cmd_off)
    opcode, n_ch, n_keys = (w >> 6) & 0xF, (w >> 10) & 0x3FF, w >> 20
    if opcode not in (0, 2) or n_ch != 1 or n_keys < 1:
        return None
    t_start, t_end = f32(cmd_off + 4), f32(cmd_off + 8)
    _pre, _post, bias, scale = struct.unpack_from('<4f', blob, cmd_off + 12)
    p = cmd_off + 28 + n_keys * (2 if opcode == 0 else 4)
    pts = []
    for k in range(n_keys):
        d = struct.unpack_from('<4h', blob, p + 8 * k)
        c = [d[0], d[0] + d[1], d[0] + d[1] + d[2], d[0] + d[1] + d[2] + d[3]]
        pts.append([bias + scale * v for v in c])
    v0, v1 = pts[0][0], pts[-1][3]
    if t_end == t_start:
        return None
    slope = (v1 - v0) / (t_end - t_start)
    # Only accept a straight line: every Bezier control point must sit on it.
    step = (v1 - v0) / (3 * n_keys)
    flat = [v for seg in pts for v in seg[:3]] + [v1]
    expected = [v0 + step * i for i in range(3 * n_keys)] + [v1]
    if any(abs(a - b) > 1e-3 * max(1.0, abs(v1 - v0)) for a, b in zip(flat, expected)):
        return None
    intercept = v0 - slope * t_start
    # The stored curve is quantised to 16 bits; McQueen's spin curve is
    # degrees -> radians to within that error, so snap it to exact.
    if abs(slope - math.pi / 180.0) < 1e-4 and abs(intercept) < 1e-3:
        slope, intercept = math.pi / 180.0, 0.0

    # Bind records: target = index into the processor's Output list; an
    # Euler sub-op (category 0x1000) reads the curve on each axis whose
    # operand-source bits say "command buffer".
    n_cmd_idx = u16(bind_off + 2)
    base = bind_off + 4 + 2 * n_cmd_idx
    count = u16(base + 2)
    q = base + 4
    targets = []
    for _ in range(count):
        word, target = u16(q), u16(q + 2)
        q += 4
        for _ in range(word & 0xF):
            sub = u16(q)
            if (sub & 0xF000) in (0x0000, 0x3000):
                q += 4
                continue
            if (sub & 0xF000) == 0x1000:
                for axis in range(3):
                    if ((sub >> (4 - 2 * axis)) & 3) == 1:
                        targets.append((target, axis))
            q += 8
    return (slope, intercept, targets) if targets else None


def _decode_uber(blob):
    """Ops of an UberConstraintDataBlock, in evaluation order.

    u32 count, then per op a u32 word -- type in the low byte, record size
    in the next byte, 0x40000000 on the last op -- and a u32, followed by
    the op's payload:
      0x15 position (Maya point)
      0x16 orientation (Maya orient)
      0x18 scale
      0x17 aim:      f32 aim[3], f32 up[3], u32 up type
      0x1b parent, with a stored offset (4 f32)
    0x15 / 0x16 were first read the other way round. Mater settles it: his
    brows use 0x15 alone and his clips only move the brow controls up and
    down (and they move in-game), while his TowCrane, side mirrors and
    FrontEnd use 0x16 alone and have to turn with their controls. Every
    link of either kind has matching rest position and orientation on both
    ends, so neither changes anything at rest.
    """
    count = struct.unpack_from('<I', blob, 0)[0]
    p, ops = 4, []
    for _ in range(min(count, 16)):
        word = struct.unpack_from('<I', blob, p)[0]
        kind, size = word & 0xFF, (word >> 8) & 0xFF
        if kind == 0x16:
            ops.append(('rot',))
        elif kind == 0x15:
            ops.append(('loc',))
        elif kind == 0x18:
            ops.append(('scale',))
        elif kind == 0x17:
            ops.append(('aim', struct.unpack_from('<3f', blob, p + 8)))
        elif kind == 0x1b:
            ops.append(('parent',))
        else:
            ops.append(('unknown', kind))
        # (guard against a duplicated op in odd blocks)
        if ops.count(('rot',)) > 1:
            ops.remove(('rot',))
        if size < 8:
            break
        p += size
    return ops


def plan_control_rig(sub_networks, skin_bones):
    """Work out the control bones and how the skin bones hang off them.

    skin_bones: list of (name, rest_matrix, skin_parent_name) in hierarchy
    order. Returns a dict, or None when this file has no usable rig network
    (e.g. props, or games that don't use one) -- the caller then just keeps
    the skin skeleton.
    """
    net = None
    for sn in pool_list(sub_networks):
        if sn.get('DataNodePool') and sn.get('ProcessorNodePool'):
            net = sn
            break
    if net is None:
        return None

    dn = pool_list(net.get('DataNodePool'))
    procs = pool_list(net.get('ProcessorNodePool'))
    used_map = net.get('ConnectionMapUsedDataNodes') or []
    strings = net.get('HeaderStrings') or []
    string_idx = net.get('HeaderStringIndices') or []

    # --- world rest matrix of every transform node --------------------------
    world = {}

    def get_world(i, guard=0):
        if i in world:
            return world[i]
        parent = dn[i].get('BasisDataNodeRef')
        local = _data_node_local(dn[i])
        if parent is not None and 0 <= parent < len(dn) and guard < 256:
            m = get_world(parent, guard + 1) @ local
        else:
            m = local
        world[i] = m
        return m

    xform = [i for i, n in enumerate(dn) if n.get('Type') in _XFORM_TYPES]
    for i in xform:
        get_world(i)

    # --- channel label -> data node, via each LayerBind's template -----------
    maps = _find_channel_maps()
    if maps is None:
        log("WARNING: Cars 2 animation add-on not found (enable it, or set "
            "ANIM_ADDON_PATH) -- control bones get generic names and clips "
            "won't find them.")
        maps = {}
    label_of = {}
    for links in pool_list(net.get('BaseDataNodeLinksPool')):
        template = os.path.splitext(
            (links.get('ConnectionTemplate') or '').replace('\\', '/').split('/')[-1])[0]
        table = maps.get(template, {})
        for ch, ref in enumerate(links.get('DataNodeLinks') or []):
            if ref is None or ref < 0 or ref >= len(used_map) or ch not in table:
                continue
            label_of.setdefault(used_map[ref], table[ch])

    # --- UberConstraints: skin-side joint node <- control node --------------
    uber = {}
    for p in procs:
        if p.get('Type') == 'UberConstraintProcessor':
            for out, inp in zip(p.get('Output') or [], p.get('Input') or []):
                uber[out] = inp
    controls = set(uber.values())

    # --- match skin bones to joint nodes -------------------------------------
    # Both trees hang off a runtime root (the kinematic state) whose value
    # isn't stored, so the rig sits at a constant offset from model space
    # (0, 0.44, 0) on McQueen. Find it by voting rather than assuming.
    from collections import Counter
    votes = Counter()
    for _name, rest, _parent in skin_bones:
        p = rest.to_translation()
        for i in xform:
            d = p - world[i].to_translation()
            votes[(round(d.x, 3), round(d.y, 3), round(d.z, 3))] += 1
    if not votes:
        return None
    offset = Vector(votes.most_common(1)[0][0])

    def depth(i):
        k = 0
        while dn[i].get('BasisDataNodeRef') is not None and k < 256:
            i = dn[i]['BasisDataNodeRef']
            k += 1
        return k

    clip_outputs = set()
    for p in procs:
        if p.get('Type') == 'ClipProcessor':
            clip_outputs.update(o for o in (p.get('Output') or []) if isinstance(o, int))

    skin_names = set(n for n, _r, _p in skin_bones)
    # Group nodes above the clip's control nodes belong to the control rig,
    # never to the skeleton; on some rigs one sits exactly where a skin bone
    # is (The King's Body), so keep them out of the matching.
    label_anc = set()
    for i in label_of:
        k = dn[i].get('BasisDataNodeRef') if 0 <= i < len(dn) else None
        n = 0
        while k is not None and k not in label_anc and n < 256:
            label_anc.add(k)
            k = dn[k].get('BasisDataNodeRef')
            n += 1
    joint_of, taken = {}, set()
    # Pass 1, by name: a joint constrained to a control with the skin bone's
    # own name. This catches joints whose rest doesn't sit exactly on the
    # skin bone -- Mater's right-hand mouth controls are placed a little
    # higher than the symmetric bind pose, which is his crooked grin.
    for out, inp in uber.items():
        lab = label_of.get(inp)
        if lab in skin_names and lab not in joint_of and out not in taken:
            joint_of[lab] = out
            taken.add(out)
    # Pass 2, by position, in skeleton order.
    for name, rest, parent in skin_bones:
        if name in joint_of:
            continue
        p = rest.to_translation()
        cands = [i for i in xform
                 if i not in taken and i not in controls and i not in label_of
                 and i not in label_anc
                 and ((world[i].to_translation() + offset) - p).length < 2e-3]
        parent_joint = joint_of.get(parent)
        wants_spin = 'spin' in name.lower()
        # Several joints often share one position (a wheel's centre, spin and
        # suspension joints). Tell them apart by: spin joints are the ones a
        # ClipProcessor drives, then constrained joints first, then a parent
        # that matches the skin parent's joint, then the shallowest.
        cands.sort(key=lambda i: ((i in clip_outputs) != wants_spin,
                                  i not in uber,
                                  dn[i].get('BasisDataNodeRef') != parent_joint,
                                  depth(i)))
        if cands:
            joint_of[name] = cands[0]
            taken.add(cands[0])
    # Pass 3, nearest driven joint: some models (The King, ...) ship a shared
    # car rig built on McQueen's proportions, so their wheel and body joints
    # sit a few cm to ~25 cm from the model's own bones and nothing matches
    # exactly -- in-game the rig is re-based onto each character's skeleton.
    # Take the nearest joint the rig actually drives, same tie-breaks as
    # above, and let these bones keep their own rest (see the followers
    # below) while following the control's motion.
    fallback = set()
    driven = set(uber) | clip_outputs
    pairs = []
    for name, rest, parent in skin_bones:
        if name in joint_of:
            continue
        p = rest.to_translation()
        wants_spin = 'spin' in name.lower()
        for i in xform:
            if (i in driven and i not in taken and i not in controls
                    and i not in label_of and i not in label_anc):
                dist = ((world[i].to_translation() + offset) - p).length
                if dist < 0.5:
                    pairs.append(((i in clip_outputs) != wants_spin, dist, i not in uber,
                                  depth(i), name, i))
    # Closest pairs first across all bones (a root bone must not grab the
    # body's joint just because it comes first in the skeleton).
    for _spin, _dist, _nu, _d, name, i in sorted(pairs, key=lambda t: t[:4]):
        if name in joint_of or i in taken:
            continue
        joint_of[name] = i
        taken.add(i)
        fallback.add(name)
    if fallback:
        log("Control rig: %d bones matched to the nearest rig joint (rig built "
            "on different proportions): %s" % (len(fallback), ', '.join(sorted(fallback))))
    skin_of_joint = {j: n for n, j in joint_of.items()}

    # Refine the offset from constrained pairs that coincide at rest.
    pairs = []
    for name, rest, _p in skin_bones:
        j = joint_of.get(name)
        c = uber.get(j)
        if c in world:
            a, b = rest.to_translation(), world[c].to_translation()
            if ((b + offset) - a).length < 2e-3:
                pairs.append((a, b))
    if pairs:
        offset = sum((a - b for a, b in pairs), Vector((0, 0, 0))) / len(pairs)

    # --- which rig nodes become helper bones ---------------------------------
    # Every constraint source, every clip channel and every relationship
    # output, plus their ancestors -- except where an ancestor is a joint
    # that already has a skin bone: the helper hangs off that skin bone.
    wanted = set(i for i in controls if i in world and i not in skin_of_joint)
    wanted |= set(i for i, lab in label_of.items()
                  if i in world and '/' not in lab and i not in skin_of_joint)
    rel = next((p for p in procs if p.get('Type') == 'RelationshipProcessor'), None)
    if rel:
        wanted |= set(i for i in (rel.get('Output') or [])
                      if i in world and i not in skin_of_joint)
    helpers = set()
    for i in wanted:
        k = 0
        while (i is not None and i in world and i not in helpers
               and i not in skin_of_joint and k < 256):
            helpers.add(i)
            i = dn[i].get('BasisDataNodeRef')
            k += 1

    children = {}
    for i in helpers:
        b = dn[i].get('BasisDataNodeRef')
        children.setdefault(b, []).append(i)

    named = sorted(set(lab for i, lab in label_of.items() if i in helpers))
    log("Control rig: %d labelled controls (e.g. %s)." % (
        len(named), ', '.join(n for n in named if n in ('FrontEnd', 'FLeft_Center', 'Body2', 'Jaw'))))

    # Cars 2 Toolkit: unlabelled rig nodes are named after their node in the
    # network (Anchor/Body_Final_Proxy/Transform -> Body_Final_Proxy) instead
    # of rig_<index>. Header ids come from HeaderLayout: (parent id, child
    # count) pairs in depth-first order, ids starting at 1.
    hdr_parent = [None] * (len(string_idx) + 1)
    _nxt = 1
    _lay = list(net.get('HeaderLayout') or [])
    for _k in range(0, len(_lay) - 1, 2):
        for _c in range(_lay[_k + 1]):
            if _nxt <= len(string_idx):
                hdr_parent[_nxt] = _lay[_k]
            _nxt += 1

    def node_name(i):
        h = dn[i].get('Header') if 0 <= i < len(dn) else None
        if not isinstance(h, int) or not 1 <= h <= len(string_idx):
            return None
        si = string_idx[h - 1]
        nm = strings[si] if 0 <= si < len(strings) else None
        if nm == 'Transform' and hdr_parent[h]:
            si = string_idx[hdr_parent[h] - 1]
            nm = strings[si] if 0 <= si < len(strings) else None
        if not nm or nm == 'Anchor' or '/' in nm or nm.startswith('UberConstraint'):
            return None
        return nm

    names, seen = {}, set()
    rel_outputs = set(rel.get('Output') or []) if rel else set()
    for i in sorted(helpers):
        lab = label_of.get(i)
        if lab:
            # The add-on tries "<label>_Ctrl" before the bare label, so
            # these win over same-named skin bones (Main, Body, Jaw, ...).
            nm = lab.replace('/', '_')
            if '/' not in lab and not lab.endswith(('_Ctrl', '_CtrlGrp', '_Proxy')):
                nm += '_Ctrl'
        elif i in rel_outputs:
            kid = next((c for c in children.get(i, []) if label_of.get(c)), None)
            nm = (label_of[kid].replace('/', '_') if kid is not None else 'rig_%d' % i) + '_Proxy'
        else:
            nm = node_name(i) or 'rig_%d' % i
            if nm in skin_names:
                nm += '_Rig'
        base, k = nm, 1
        while nm in seen or nm in skin_names:
            nm = '%s.%03d' % (base, k)
            k += 1
        seen.add(nm)
        names[i] = nm

    def bone_for(node):
        if node in skin_of_joint:
            return skin_of_joint[node]
        return names.get(node)

    offset_m = Matrix.Translation(offset)
    rest_of = {n: r for n, r, _p in skin_bones}
    control_bones = []
    for i in sorted(helpers, key=depth):
        b = dn[i].get('BasisDataNodeRef')
        rest = offset_m @ world[i]
        rest_of[names[i]] = rest
        control_bones.append((names[i], rest, bone_for(b)))

    # --- UberConstraints -> Blender bone constraints --------------------------
    # The game doesn't parent a joint to its control, it constrains it, and
    # not always fully: McQueen copies position + rotation, Mater's FrontEnd
    # only position, his brows only rotation, his face position + rotation +
    # scale, his axle pieces aim at the opposite wheel, and his tow-cable rig
    # is a chain of aims. Each op becomes the matching Blender constraint, in
    # armature space and without offset, exactly like the game.
    constraints = []
    for out, inp in uber.items():
        owner, target = bone_for(out), bone_for(inp)
        if not owner or not target or owner == target:
            continue
        p = next((q for q in procs if q.get('Type') == 'UberConstraintProcessor'
                  and out in (q.get('Output') or [])), None)
        ops = None
        blob_node = p.get('UberConstraintDataInput') if p else None
        if isinstance(blob_node, int) and 0 <= blob_node < len(dn):
            blob = dn[blob_node].get('UberConstraintDataBlock')
            if isinstance(blob, str) and blob.startswith('base64:'):
                raw = blob[len('base64:'):]
                try:
                    ops = _decode_uber(base64.b64decode(raw + '=' * (-len(raw) % 4)))
                except struct.error:
                    ops = None
        if not ops:
            ops = [('loc',), ('rot',)]
        resolved = []
        follower = None
        if owner in fallback and any(op[0] in ('loc', 'rot', 'scale') for op in ops):
            # Keep the bone's own rest: a follower under the control, sitting
            # at the bone's rest, carries the control's motion to it.
            follower = owner + '_Follow'
            control_bones.append((follower, rest_of.get(owner, Matrix.Identity(4)), target))
        for op in ops:
            if follower and op[0] in ('loc', 'rot', 'scale'):
                resolved.append((op[0] + '_via', follower))
                continue
            if op[0] == 'aim':
                if out not in world or owner not in rest_of:
                    continue
                joint_rot = world[out].decompose()[1].to_matrix()
                owner_rot = rest_of[owner].decompose()[1].to_matrix()
                local = owner_rot.transposed() @ (joint_rot @ Vector(op[1]))
                comps = [local.x, local.y, local.z]
                ax = max(range(3), key=lambda a: abs(comps[a]))
                resolved.append(('aim', ('TRACK_' if comps[ax] > 0 else 'TRACK_NEGATIVE_')
                                 + 'XYZ'[ax]))
            elif op[0] == 'parent':
                # Parent constraint with an offset: a follower bone under the
                # source, sitting at the owner's rest, holds that offset, and
                # the owner copies its full transform.
                follower = owner + '_Offset'
                control_bones.append((follower, rest_of.get(owner, Matrix.Identity(4)), target))
                resolved.append(('parent_from', follower))
            elif op[0] == 'unknown':
                log("WARNING: unknown UberConstraint op 0x%02x on %s -- using "
                    "a full copy instead." % (op[1], owner))
                resolved += [('loc',), ('rot',)]
            else:
                resolved.append(op)
        constraints.append((owner, target, resolved))

    # --- mouth relationship (sliders -> *_Proxy offsets) ----------------------
    relationship = None
    if rel is not None:
        blob_node = rel.get('RelationshipData')
        blob = None
        if isinstance(blob_node, int) and 0 <= blob_node < len(dn):
            blob = dn[blob_node].get('RelationshipDataBlock')
        if isinstance(blob, str) and blob.startswith('base64:'):
            raw = blob[len('base64:'):]
            raw_bytes = base64.b64decode(raw + '=' * (-len(raw) % 4))
            inputs = []
            for i in rel.get('Input') or []:
                lab = label_of.get(i)
                nm = lab.split('/')[-1] if lab else None
                if nm is None and 0 <= i < len(dn):
                    # Not in any template (vsm_AW on McQueen): fall back on
                    # the node's header string, which is at Header - 1.
                    h = (dn[i].get('Header') or 0) - 1
                    if 0 <= h < len(string_idx) and 0 <= string_idx[h] < len(strings):
                        cand = strings[string_idx[h]]
                        if cand.startswith(_SLIDER_PREFIXES):
                            nm = cand
                inputs.append(nm or '')
            outputs = [names.get(i, '') for i in rel.get('Output') or []]
            relationship = (base64.b64encode(raw_bytes).decode('ascii'), inputs, outputs)

    # --- float-driven rotations (wheel spin) ----------------------------------
    bone_of_joint = skin_of_joint
    spin_drivers = []
    for p in procs:
        if p.get('Type') != 'ClipProcessor':
            continue
        lab = label_of.get(p.get('Input'))
        clip_node = p.get('ClipData')
        if not lab or not isinstance(clip_node, int) or not 0 <= clip_node < len(dn):
            continue
        blob = dn[clip_node].get('ClipDataBlock')
        if not isinstance(blob, str) or not blob.startswith('base64:'):
            continue
        raw = blob[len('base64:'):]
        try:
            decoded = _decode_value_clip(base64.b64decode(raw + '=' * (-len(raw) % 4)))
        except struct.error:
            decoded = None
        if decoded is None:
            continue
        slope, intercept, targets = decoded
        outputs = p.get('Output') or []
        for target, axis in targets:
            if target < len(outputs) and outputs[target] in bone_of_joint:
                spin_drivers.append((bone_of_joint[outputs[target]], axis,
                                     lab, slope, intercept))

    # --- eyes: clip floats -> eye shader parameters --------------------------
    def header_name(i):
        h = (dn[i].get('Header') or 0) - 1
        if 0 <= h < len(string_idx) and 0 <= string_idx[h] < len(strings):
            return strings[string_idx[h]]
        return None

    eye_links = []
    for p in procs:
        if p.get('Type') != 'Connection':
            continue
        refs = p.get('DataNodeRefs') or []
        if len(refs) == 2 and all(isinstance(r, int) and 0 <= r < len(dn) for r in refs):
            lab, param = label_of.get(refs[0]), header_name(refs[1])
            if lab and param:
                # The control's own resting value (0 on every shipped car).
                # The Connection overwrites the material's value every frame,
                # so this -- not the material entry -- is what the shader
                # shows when no clip moves that control.
                rest = dn[refs[0]].get('Data')
                eye_links.append((lab, param,
                                  float(rest) if isinstance(rest, (int, float)) else 0.0))
    eye_rbf = None
    pos = next((p for p in procs if p.get('Type') == 'RadialBasisPosition'), None)
    if pos is not None and len(pos.get('DataNodeRefs') or []) == 4:
        k_ref, _ctrl, sd_ref, out_ref = pos['DataNodeRefs']
        keys = dn[k_ref].get('Data') if 0 <= k_ref < len(dn) else None
        sds = dn[sd_ref].get('Data') if 0 <= sd_ref < len(dn) else None
        outputs = []
        for p in procs:
            refs = p.get('DataNodeRefs') or []
            if p.get('Type') == 'RadialBasisApplyFloat' and len(refs) == 3 and refs[0] == out_ref:
                w = dn[refs[1]].get('Data')
                name = header_name(refs[2])
                if isinstance(w, list) and name:
                    outputs.append((name, w))
        # The interpolator reads the Eyes control's position (the clip's
        # "Eyes" channel), in its parent frame.
        eyes_node = next((i for i, lab in label_of.items() if lab == 'Eyes'), None)
        # The control moves inside Eyes_Frame_Proxy, whose Scale squashes
        # it to the windshield; the interpolator's -1..1 keys are in that
        # scaled space (unscaled, select_idle holds McQueen's irises almost
        # off the eye; scaled, he glances left, irises in view).
        frame_scale = (1.0, 1.0)
        if eyes_node is not None:
            frame = dn[eyes_node].get('BasisDataNodeRef')
            fd = dn[frame].get('Data') if isinstance(frame, int) and 0 <= frame < len(dn) else None
            sc = fd.get('Scale') if isinstance(fd, dict) else None
            if isinstance(sc, list) and len(sc) >= 2:
                frame_scale = (float(sc[0]), float(sc[1]))
        if isinstance(keys, list) and isinstance(sds, list) and outputs:
            eye_rbf = {'bone': names.get(eyes_node), 'keys': keys, 'sds': sds,
                       'outputs': outputs, 'frame_scale': frame_scale}

    # --- Cars 2 Toolkit: tidy - drop static pass-through rig bones ---------
    used = set()
    for owner, target, ops in constraints:
        used.add(owner)
        used.add(target)
        for op in ops:
            if len(op) > 1 and isinstance(op[1], str):
                used.add(op[1])
    rel_names = set(n for n in (relationship[2] if relationship else []) if n)
    if eye_rbf and eye_rbf.get('bone'):
        used.add(eye_rbf['bone'])
    kinds = {}
    for i in helpers:
        nm = names[i]
        lab = label_of.get(i)
        if lab:
            kinds[nm] = 'fx' if lab.startswith('FX') else 'control'
        elif nm == 'Body_Final_Proxy':
            kinds[nm] = 'control'                 # the bone to move to raise the body
        elif nm in rel_names or nm in used:
            kinds[nm] = 'internal'
        else:
            kinds[nm] = None
    for nm, _rest, _p in control_bones:
        kinds.setdefault(nm, 'internal')          # followers / offsets
    drop = set(nm for nm, k in kinds.items() if k is None) if TIDY_RIG else set()
    if drop:
        parent_of = {n: p for n, _r, p in control_bones}

        def keep_parent(p):
            k = 0
            while p in drop and k < 512:
                p = parent_of.get(p)
                k += 1
            return p
        control_bones = [(n, r, keep_parent(p)) for n, r, p in control_bones if n not in drop]
        log("Control rig: left out %d pass-through rig bones (TIDY_RIG)." % len(drop))

    return {
        'eye_links': eye_links,
        'eye_rbf': eye_rbf,
        'spin_drivers': spin_drivers,
        'control_bones': control_bones,
        'constraints': constraints,
        'relationship': relationship,
        'offset': offset,
        'matched': sum(1 for o, _t, _c in constraints if o in skin_names),
        # Cars 2 Toolkit: which network data node each control bone stands for
        'node_of_bone': {nm: i for i, nm in names.items() if nm not in drop},
        'bone_kinds': {n: k for n, k in kinds.items() if n not in drop},
    }


def _skin_rest_matrix(node, data, node_index):
    m = node.get('BoneToModelMatrix')
    if m and len(m) >= 16 and all(isinstance(v, (int, float)) for v in m[:16]):
        return list_to_mat4(m)
    return data.world_matrix(node_index)


def _bone_length(node):
    pivot, end = node.get('PivotPoint'), node.get('BoneEndPoint')
    try:
        length = (Vector(end) - Vector(pivot)).length
    except (TypeError, ValueError):
        length = 0.0
    return length if length > 1e-4 else 0.1


def _set_edit_bone(eb, rest, length):
    loc, rot, _scale = rest.decompose()
    eb.head = (0.0, 0.0, 0.0)
    eb.tail = (0.0, length, 0.0)
    eb.matrix = Matrix.Translation(loc) @ rot.to_matrix().to_4x4()


def _add_spin_driver(arm_obj, bone_name, axis, prop, slope, intercept):
    """Drive a spin joint's rotation from the custom property the animation
    add-on keys for that channel (e.g. ["Main_Ctrl/RotateFront"]). The
    property has to exist for the clip's F-curve to have something to
    write to, so it's created here at 0."""
    pb = arm_obj.pose.bones.get(bone_name)
    if pb is None:
        return
    if prop not in arm_obj.keys():
        arm_obj[prop] = 0.0
    pb.rotation_mode = 'XYZ'
    fc = pb.driver_add('rotation_euler', axis)
    drv = fc.driver
    drv.type = 'SCRIPTED'
    var = drv.variables.new()
    var.name = 'v'
    var.type = 'SINGLE_PROP'
    var.targets[0].id_type = 'OBJECT'
    var.targets[0].id = arm_obj
    var.targets[0].data_path = '["%s"]' % prop
    k = slope * WHEEL_SPIN_DIRECTION
    c = intercept * WHEEL_SPIN_DIRECTION
    # Plain arithmetic, so Blender's simple-expression evaluator runs it
    # without needing "Auto Run Python Scripts".
    drv.expression = 'v * %r + %r' % (k, c)


def _add_uber_constraints(arm_obj, owner, target, ops):
    pb = arm_obj.pose.bones.get(owner)
    if pb is None:
        return
    kinds = {'loc': 'COPY_LOCATION', 'rot': 'COPY_ROTATION',
             'scale': 'COPY_SCALE', 'parent_from': 'COPY_TRANSFORMS',
             'loc_via': 'COPY_LOCATION', 'rot_via': 'COPY_ROTATION',
             'scale_via': 'COPY_SCALE', 'aim': 'DAMPED_TRACK'}
    for op in ops:
        kind = kinds.get(op[0])
        if kind is None:
            continue
        con = pb.constraints.new(kind)
        con.name = 'Uber %s' % op[0]
        con.target = arm_obj
        con.subtarget = op[1] if op[0] in ('parent_from', 'loc_via', 'rot_via', 'scale_via') else target
        if op[0] == 'aim':
            con.track_axis = op[1]
        else:
            con.owner_space = 'POSE'
            con.target_space = 'POSE'


def build_armature(data, name="Armature", json_path=None):
    if not data.bone_nodes:
        return None, {}

    arm_data = bpy.data.armatures.new(name)
    arm_obj = bpy.data.objects.new(name, arm_data)
    bpy.context.collection.objects.link(arm_obj)
    bpy.context.view_layer.objects.active = arm_obj

    skin = []
    by_index = dict(data.bone_nodes)
    for node_index, node in data.bone_nodes:
        bname = node.get('NodeName', 'bone_%d' % node_index)
        parents = node.get('ParentNodeReferences')
        parent = None
        if parents and parents[0] in by_index:
            parent = by_index[parents[0]].get('NodeName', 'bone_%d' % parents[0])
        skin.append((bname, _skin_rest_matrix(node, data, node_index), parent,
                     _bone_length(node)))

    plan = None
    if BUILD_CONTROL_RIG and data.sub_networks:
        try:
            plan = plan_control_rig(data.sub_networks,
                                    [(n, r, p) for n, r, p, _l in skin])
        except Exception as e:  # never let the rig stop a model import
            import traceback
            log("WARNING: control rig skipped: %s" % e)
            log(traceback.format_exc())
            plan = None

    global _LAST_RIG_PLAN
    _LAST_RIG_PLAN = plan

    bpy.ops.object.mode_set(mode='EDIT')
    ebs = {}
    for bname, rest, _parent, length in skin:
        eb = arm_data.edit_bones.new(bname)
        _set_edit_bone(eb, rest, length)
        ebs[bname] = eb

    ctrl_names = []
    if plan:
        for cname, rest, _cparent in plan['control_bones']:
            eb = arm_data.edit_bones.new(cname)
            _set_edit_bone(eb, rest, 0.05)
            eb.use_deform = False
            ebs[cname] = eb
            ctrl_names.append(cname)
        for cname, _rest, cparent in plan['control_bones']:
            if cparent:
                ebs[cname].parent = ebs[cparent]

    # Skin bones keep the skin hierarchy (it mirrors the game's joint tree);
    # what drives them from the controls is the constraints added below.
    for bname, _rest, parent, _length in skin:
        if parent and parent in ebs:
            ebs[bname].parent = ebs[parent]

    bpy.ops.object.mode_set(mode='OBJECT')

    # Cars 2 Toolkit: what the marker/rig-bone export needs - every bone's
    # rest as imported (so Edit Mode moves can be detected), the network
    # node behind each control bone, and the rig's offset from the skeleton.
    for b in arm_data.bones:
        b["oct_rest"] = [v for row in b.matrix_local for v in row]
    if plan:
        arm_obj["cars2_rig_offset"] = list(plan['offset'])
        for bname, node in plan.get('node_of_bone', {}).items():
            if bname in arm_data.bones:
                arm_data.bones[bname]["oct_data_node"] = node

    # Cars 2 Toolkit: bone collections - Skin, Controls, FX Markers, and the
    # helpers nobody needs to touch in Rig Internals (hidden).
    try:
        colls = {}

        def bone_coll(cname, visible):
            if cname not in colls:
                c = arm_data.collections.get(cname) or arm_data.collections.new(cname)
                c.is_visible = visible
                colls[cname] = c
            return colls[cname]
        for bname, _rest, _parent, _length in skin:
            bone_coll("Skin", True).assign(arm_data.bones[bname])
        kinds = (plan or {}).get('bone_kinds', {})
        for cname in ctrl_names:
            k = kinds.get(cname, 'internal')
            target = {'control': "Controls", 'fx': "FX Markers"}.get(k, "Rig Internals")
            bone_coll(target, k != 'internal').assign(arm_data.bones[cname])
    except AttributeError:
        pass

    if plan:
        for owner, target, ops in plan.get('constraints', []):
            _add_uber_constraints(arm_obj, owner, target, ops)
        for bname, axis, prop, slope, intercept in plan.get('spin_drivers', []):
            _add_spin_driver(arm_obj, bname, axis, prop, slope, intercept)
        if plan.get('spin_drivers'):
            log("Wheel spin: %s" % ', '.join(
                '%s <- %s' % (b, p) for b, _a, p, _s, _i in plan['spin_drivers']))
        if plan['relationship']:
            blob, inputs, outputs = plan['relationship']
            arm_obj["cars2_relationship"] = blob
            arm_obj["cars2_relationship_inputs"] = json.dumps(inputs)
            arm_obj["cars2_relationship_outputs"] = json.dumps(outputs)
        log("Control rig: %d control bones, %d skin bones driven by a control, "
            "rig offset %s, mouth relationship %s." % (
                len(ctrl_names), plan['matched'], tuple(round(v, 5) for v in plan['offset']),
                "stored" if plan['relationship'] else "not found"))
        for owner, target, ops in plan.get('constraints', []):
            log("  %s <- %s (%s)" % (owner, target, ', '.join(
                o[0] if len(o) == 1 or o[0] == 'aim' else '%s via %s' % (o[0].replace('_via', '').replace('_from', ''), o[1])
                for o in ops)))

    return arm_obj, data.bone_id_to_name


# ---------------------------------------------------------------------------
# Eyes
#
# Cars 2 eyes aren't geometry or shape keys: "bodyMesh_eyeshader" is a flat
# strip over the windshield (UV 0..1 across, 0..1 top to bottom) and the
# car_eyes_* shader draws two eyeballs and the lids on it from ~45 material
# parameters. The rig drives those parameters:
#   * Connection processors copy the clip's Eyes_Ctrl/<lid control> floats
#     straight into eyeLidTop_* / eyeLidBottom_* parameters.
#   * A radial-basis interpolator turns the Eyes control's position (a 3x3
#     grid of keys at -1..1) into eyeBallOffset_u / _v and eyeLidOffset_u,
#     in degrees -- the eyeball is treated as a sphere and rolled.
# All of that wiring is reproduced exactly (as drivers). The drawing itself
# lives in the game's compiled shader, which isn't in these files, so the
# eyeball/lid geometry below is a reconstruction; everything that is a guess
# is a named input of the "Cars2 Eye" node group (EYE_CALIBRATION) so it can
# be tuned in the material editor without re-importing.
#
# From the game's car_eyes_*.tfx (D3D9 effect, disassembled): the windshield
# shader just samples a pre-drawn "renderedEyeTexture" at the strip's own UVs
# and shows lerp(alpha, eye colour, diffuseColorA1) -- i.e. the strip IS the
# eye image, whites and all, and only where that image is transparent does
# the lid colour show. Its specular is specularColorA1 * (1 - alpha), so only
# the lids get the paint shine. The eye image itself is drawn by game code
# from the eyeBall*/eyeLid* values; that part is reconstructed here.
#
# The maths is written once as an expression graph (Ex) and either evaluated
# with numpy (for previews outside Blender) or emitted as shader Math nodes.
# ---------------------------------------------------------------------------

EYE_CALIBRATION = {
    # Everything geometric now comes straight from the game's code (see
    # build_eye_graph); these are only the colours the game sets at runtime,
    # which aren't in the files.
    # Brightness of the grey lid outline band (the game's SolidColor pass).
    'lid_line_darkness': 0.45,
    # Thickness of that band, as a fraction of the strip height. The game
    # draws it 0.04 thick; slightly thinner reads closer on screen.
    'lid_line_width': 0.03,
    # Sclera brightness (the render target's clear colour).
    'sclera': 0.95,
}

# Textured eyelids (diffuseMapA1 on the eye material: Mater, King, Holley,
# Carla ...): the game's D3D v (down) becomes Blender's v (up), as for the
# mesh UVs. False only for testing.
EYE_LID_TEXTURE_FLIP_V = True

# Armature property: {slider label: value it got at model import} (v1.88).
SLIDER_REST_PROP = "cars2_slider_rest"

# Every material parameter the network uses (defaults come from the file).
EYE_PARAMS = [
    'eyeBallScale_u', 'eyeBallScale_v', 'eyeBallSpacing_u',
    'eyeBallOffset_u', 'eyeBallOffset_v', 'eyeLidOffset_u',
    'eyeLidTop_left_leftPos_v', 'eyeLidTop_left_leftAngle',
    'eyeLidTop_left_middlePos_v', 'eyeLidTop_left_middlePercent_u',
    'eyeLidTop_left_middleAngle', 'eyeLidTop_left_rightAngle',
    'eyeLidTop_notch_leftPos_v', 'eyeLidTop_notch_leftPos_u',
    'eyeLidTop_notchPos_v',
    'eyeLidTop_notch_rightPos_v', 'eyeLidTop_notch_rightPos_u',
    'eyeLidTop_right_leftAngle', 'eyeLidTop_right_middlePos_v',
    'eyeLidTop_right_middlePercent_u', 'eyeLidTop_right_middleAngle',
    'eyeLidTop_right_rightPos_v', 'eyeLidTop_right_rightAngle',
    'eyeLidBottom_left_leftPos_v', 'eyeLidBottom_left_leftAngle',
    'eyeLidBottom_left_middlePos_v', 'eyeLidBottom_left_middlePercent_u',
    'eyeLidBottom_left_middleAngle', 'eyeLidBottom_left_rightAngle',
    'eyeLidBottom_notchPos_v',
    'eyeLidBottom_right_leftAngle', 'eyeLidBottom_right_middlePos_v',
    'eyeLidBottom_right_middlePercent_u', 'eyeLidBottom_right_middleAngle',
    'eyeLidBottom_right_rightPos_v', 'eyeLidBottom_right_rightAngle',
    # car_double_eyes only: a look offset per eye, and the bottom lids'
    # inner ends (the top lids' are the notch_left/rightPos_v above).
    'eyeBall_left_offset_u', 'eyeBall_left_offset_v',
    'eyeBall_right_offset_u', 'eyeBall_right_offset_v',
    'eyeLidBottom_notch_leftPos_v', 'eyeLidBottom_notch_rightPos_v',
]


class Ex(object):
    """A node in a scalar expression graph."""
    __slots__ = ('op', 'args', 'value')

    def __init__(self, op, args=(), value=None):
        self.op, self.args, self.value = op, tuple(args), value

    @staticmethod
    def lift(x):
        return x if isinstance(x, Ex) else Ex('const', value=float(x))

    def _bin(self, op, other, swap=False):
        a, b = (Ex.lift(other), self) if swap else (self, Ex.lift(other))
        return Ex(op, (a, b))

    __add__ = lambda s, o: s._bin('ADD', o)
    __radd__ = lambda s, o: s._bin('ADD', o, True)
    __sub__ = lambda s, o: s._bin('SUBTRACT', o)
    __rsub__ = lambda s, o: s._bin('SUBTRACT', o, True)
    __mul__ = lambda s, o: s._bin('MULTIPLY', o)
    __rmul__ = lambda s, o: s._bin('MULTIPLY', o, True)
    __truediv__ = lambda s, o: s._bin('DIVIDE', o)
    __rtruediv__ = lambda s, o: s._bin('DIVIDE', o, True)
    __neg__ = lambda s: Ex('MULTIPLY', (s, Ex.lift(-1.0)))


def ex_var(name):
    return Ex('var', value=name)


def ex_fn(op, *args):
    return Ex(op, [Ex.lift(a) for a in args])


sqrt = lambda a: ex_fn('SQRT', a)
exexp = lambda a: ex_fn('EXPONENT', a)
sin = lambda a: ex_fn('SINE', a)
cos = lambda a: ex_fn('COSINE', a)
tan = lambda a: ex_fn('TANGENT', a)
exmin = lambda a, b: ex_fn('MINIMUM', a, b)
exmax = lambda a, b: ex_fn('MAXIMUM', a, b)
step = lambda a, b: ex_fn('GREATER_THAN', a, b)     # 1 where a > b
clamp01 = lambda a: exmin(exmax(a, 0.0), 1.0)
rad = lambda a: a * (math.pi / 180.0)
lerp = lambda a, b, t: a + (b - a) * t


def smooth_edge(d, width):
    """0 -> 1 across |d| < width/2 (antialiased step at d = 0)."""
    return clamp01(d / width + 0.5)


def build_eye_graph(double=False):
    """The eye image exactly as Cars 2 Arcade draws it into
    renderedEyeTexture -- transcribed from sdaemon.exe (the car_eyes
    renderer at 0x4B3EF0 and its helpers 0x4B7840 / 0x4B7570).

    Game space: x across the strip 0..1, y DOWN the strip 0..1 (the strip's
    own UVs sample renderedEyeTexture 1:1). Parameter values are percents.
      * Eyeballs: flat quads, 0.5*eyeBallScale_u wide and eyeBallScale_v
        tall, centred at x = 0.5 -/+ 0.5*spacing + (1-spacing)*0.5*offU/100,
        y = 0.5 - offV/200, textured with eyeBallTexture.
      * Lids: control points at y = 0.5 - pos/200. Between points x runs
        linearly and y is a cubic Hermite in t with the _Angle values as
        the end tangents (unscaled); the notch is straight lines. The top lid
        fills from the top edge to its curve, the bottom lid from its curve
        to the bottom edge, both centred on x = 0.5 + eyeLidOffset_u/200.
      * A copy of the top lid 0.04 lower is drawn first in a solid colour,
        leaving the grey outline band under the top lid.

    Inputs (ex_var): u, v (Blender UV, v up) and every name in EYE_PARAMS /
    EYE_CALIBRATION. Outputs: iris_u / iris_v (Blender image coords),
    iris_mask, lid (1 = lid), line (outline band), sclera, lid_u / lid_v
    (texture coords for textured lids, e.g. Mater)."""
    V = {n: ex_var(n) for n in EYE_PARAMS + list(EYE_CALIBRATION)}
    u, v = ex_var('u'), ex_var('v')
    y = 1.0 - v                              # game y, down the strip
    P = lambda n: V[n] * 0.01

    half_w = 0.25 * V['eyeBallScale_u']      # quad is 0.5*scale_u wide
    half_h = 0.5 * V['eyeBallScale_v']       # and scale_v tall
    out = {}
    if not double:
        # --- eyeballs ("car_eyes" mode) ------------------------------------
        sp = V['eyeBallSpacing_u']
        shift = (1.0 - sp) * 0.5 * P('eyeBallOffset_u')
        cy = 0.5 - 0.5 * P('eyeBallOffset_v')
        left_side = step(0.5 + shift, u)     # 1 on the u < 0.5 eye
        cx = 0.5 + shift + lerp(0.5 * sp, -0.5 * sp, left_side)
    else:
        # --- eyeballs ("car_double_eyes" mode): one look offset per eye,
        # left eye centred on x = 0.25, right on 0.75, no spacing --------------
        left_side = step(0.5, u)
        cx = lerp(0.75 + 0.25 * P('eyeBall_right_offset_u'),
                  0.25 + 0.25 * P('eyeBall_left_offset_u'), left_side)
        cy = lerp(0.5 - 0.5 * P('eyeBall_right_offset_v'),
                  0.5 - 0.5 * P('eyeBall_left_offset_v'), left_side)
    out['iris_u'] = (u - cx) / (2.0 * half_w) + 0.5
    out['iris_v'] = 0.5 - (y - cy) / (2.0 * half_h)
    out['iris_mask'] = Ex.lift(1.0)          # the texture is clipped outside 0..1

    # --- lid control points ---------------------------------------------------
    # notch centre, in -1..1 (double eyes: the lids don't move sideways)
    lo = Ex.lift(0.0) if double else P('eyeLidOffset_u')
    to01 = lambda X: X * 0.5 + 0.5
    yd = lambda n: 0.5 - 0.5 * P(n)

    if double:
        X_nl = X_nr = Ex.lift(0.0)            # each eye's lids end mid-strip
    else:
        X_nl = lo + (1.0 + lo) * P('eyeLidTop_notch_leftPos_u')
        X_nr = (1.0 - lo) * P('eyeLidTop_notch_rightPos_u') + lo
    X_lm = (P('eyeLidTop_left_middlePercent_u') * 0.5 + 0.5) * (X_nl + 1.0) - 1.0
    X_rm = (P('eyeLidTop_right_middlePercent_u') * 0.5 + 0.5) * (1.0 - X_nr) + X_nr
    X_blm = (P('eyeLidBottom_left_middlePercent_u') * 0.5 + 0.5) * (lo + 1.0) - 1.0
    X_brm = (P('eyeLidBottom_right_middlePercent_u') * 0.5 + 0.5) * (1.0 - lo) + lo
    a = lambda n: V[n]                       # _Angle values: raw tangents
    # Bottom lid's inner ends: one shared notch point, or (double eyes) one
    # per eye, both on the centre line.
    bot_nl = 'eyeLidBottom_notch_leftPos_v' if double else 'eyeLidBottom_notchPos_v'
    bot_nr = 'eyeLidBottom_notch_rightPos_v' if double else 'eyeLidBottom_notchPos_v'

    # (x, y) points and per-segment end tangents, exactly as the game calls
    # its curve helper; None marks a straight notch segment.
    top = [((0.0, yd('eyeLidTop_left_leftPos_v')),
            (to01(X_lm), yd('eyeLidTop_left_middlePos_v')),
            -a('eyeLidTop_left_leftAngle'), -a('eyeLidTop_left_middleAngle')),
           ((to01(X_lm), yd('eyeLidTop_left_middlePos_v')),
            (to01(X_nl), yd('eyeLidTop_notch_leftPos_v')),
            -a('eyeLidTop_left_middleAngle'), a('eyeLidTop_left_rightAngle')),
           ] + ([] if double else [
           ((to01(X_nl), yd('eyeLidTop_notch_leftPos_v')),
            (to01(lo), yd('eyeLidTop_notchPos_v')), None, None),
           ((to01(lo), yd('eyeLidTop_notchPos_v')),
            (to01(X_nr), yd('eyeLidTop_notch_rightPos_v')), None, None)]) + [
           ((to01(X_nr), yd('eyeLidTop_notch_rightPos_v')),
            (to01(X_rm), yd('eyeLidTop_right_middlePos_v')),
            -a('eyeLidTop_right_leftAngle'), a('eyeLidTop_right_middleAngle')),
           ((to01(X_rm), yd('eyeLidTop_right_middlePos_v')),
            (1.0, yd('eyeLidTop_right_rightPos_v')),
            a('eyeLidTop_right_middleAngle'), a('eyeLidTop_right_rightAngle'))]
    bottom = [((0.0, yd('eyeLidBottom_left_leftPos_v')),
               (to01(X_blm), yd('eyeLidBottom_left_middlePos_v')),
               -a('eyeLidBottom_left_leftAngle'), -a('eyeLidBottom_left_middleAngle')),
              ((to01(X_blm), yd('eyeLidBottom_left_middlePos_v')),
               (to01(lo), yd(bot_nl)),
               -a('eyeLidBottom_left_middleAngle'), a('eyeLidBottom_left_rightAngle')),
              ((to01(lo), yd(bot_nr)),
               (to01(X_brm), yd('eyeLidBottom_right_middlePos_v')),
               -a('eyeLidBottom_right_leftAngle'), a('eyeLidBottom_right_middleAngle')),
              ((to01(X_brm), yd('eyeLidBottom_right_middlePos_v')),
               (1.0, yd('eyeLidBottom_right_rightPos_v')),
               a('eyeLidBottom_right_middleAngle'), a('eyeLidBottom_right_rightAngle'))]

    def curve(segs):
        """y of the lid line at u: the game's helper, x linear in t and y a
        cubic Hermite in t with unscaled tangents."""
        result = None
        for (p0, p1, m0, m1) in segs:
            x0, y0 = Ex.lift(p0[0]), Ex.lift(p0[1])
            x1, y1 = Ex.lift(p1[0]), Ex.lift(p1[1])
            t = clamp01((u - x0) / exmax(x1 - x0, 1e-5))
            if m0 is None:
                h = y0 + (y1 - y0) * t
            else:
                t2 = t * t
                t3 = t2 * t
                h = ((2.0 * t3 - 3.0 * t2 + 1.0) * y0 + (t3 - 2.0 * t2 + t) * m0
                     + (-2.0 * t3 + 3.0 * t2) * y1 + (t3 - t2) * m1)
            result = h if result is None else lerp(result, h, step(u, x0))
        return result

    top_y, bot_y = curve(top), curve(bottom)
    edge = 0.003
    lid_top = smooth_edge(top_y - y, edge)          # above the top curve
    lid_bot = smooth_edge(y - bot_y, edge)          # below the bottom curve
    out['lid'] = exmax(lid_top, lid_bot)
    # The outline pass: the top lid again, lid_line_width lower (0.04 in the
    # game), under the real lid.
    band = smooth_edge(top_y + V['lid_line_width'] - y, edge) * (1.0 - out['lid'])
    out['line'] = band
    out['line_color'] = V['sclera'] * (1.0 - V['lid_line_darkness']) + v * 0.0
    out['sclera'] = V['sclera'] + v * 0.0

    # Textured lids (EyeLidsTextured, e.g. Mater, Holley, Carla): the game's
    # per-vertex UVs (sdaemon.exe 0x4B7840, the lid curve strip, and 0x4B7570,
    # the notch). u stretches each side of the notch centre c over half the
    # texture. v is measured from the lid's own edge: the top lid's strip runs
    # from (x, 0) with v = 1 - 0.5*edge_y to (x, edge_y) with v = 1, the
    # bottom lid's from (x, edge_y) with v = 0 to (x, 1) with v = 0.5*(1 -
    # edge_y). So the texture's middle rows (top lid's lashes at v = 1 in
    # D3D, bottom lid's at v = 0) always sit on the lid edges and follow
    # them - a lash line with flicks at the corners stays on the lid.
    # (Before v1.70 v ran from the strip's top/bottom instead, reversed: the
    # lashes showed along the windshield's top edge.)
    c = to01(lo)
    lu_left = (0.5 / exmax(c, 1e-4)) * u
    lu_right = ((u - c) / exmax(1.0 - c, 1e-4)) * 0.5 + 0.5
    out['lid_u'] = lerp(lu_right, lu_left, step(c, u))
    tv_top = 1.0 - 0.5 * (top_y - y)
    tv_bot = 0.5 * (y - bot_y)
    tv = lerp(tv_bot, tv_top, 1.0 - step(y, top_y))
    out['lid_v'] = (1.0 - tv) if EYE_LID_TEXTURE_FLIP_V else tv   # D3D v down -> Blender v up
    return out


def build_look_graph(keys, sds, outputs):
    """The rig's look interpolator (RadialBasisPosition + ApplyFloat):
    for each output, sum_i w_i * exp(-|p - k_i|^2 / (2 sd_i^2)), with p the
    Eyes control's position (vars look_x, look_y). Built as nodes rather
    than a driver expression because Blender cuts driver expressions off at
    256 characters, and nine Gaussian terms don't fit."""
    x, y = ex_var('look_x'), ex_var('look_y')
    out = {}
    for name, weights in outputs:
        total = None
        for i, w in enumerate(weights):
            kx, ky = keys[3 * i], keys[3 * i + 1]
            c = 1.0 / (2.0 * sds[i] * sds[i])
            dx, dy = x - kx, y - ky
            term = exexp((dx * dx + dy * dy) * -c) * w
            total = term if total is None else total + term
        if total is not None:
            out[name] = total
    return out


def eval_numpy(node, env, cache=None):
    """Evaluate an Ex graph with numpy arrays (preview / testing)."""
    import numpy as np
    if cache is None:
        cache = {}
    key = id(node)
    if key in cache:
        return cache[key]
    op = node.op
    if op == 'const':
        r = node.value
    elif op == 'var':
        r = env[node.value]
    else:
        a = [eval_numpy(x, env, cache) for x in node.args]
        f = {'ADD': lambda: a[0] + a[1], 'SUBTRACT': lambda: a[0] - a[1],
             'MULTIPLY': lambda: a[0] * a[1], 'DIVIDE': lambda: a[0] / np.where(a[1] == 0, 1e-9, a[1]),
             'SQRT': lambda: np.sqrt(np.maximum(a[0], 0)), 'SINE': lambda: np.sin(a[0]),
             'COSINE': lambda: np.cos(a[0]), 'TANGENT': lambda: np.tan(a[0]),
             'MINIMUM': lambda: np.minimum(a[0], a[1]), 'MAXIMUM': lambda: np.maximum(a[0], a[1]),
             'GREATER_THAN': lambda: (np.asarray(a[0]) > a[1]).astype(float),
             'ABSOLUTE': lambda: np.abs(a[0]), 'EXPONENT': lambda: np.exp(a[0])}[op]
        r = f()
    cache[key] = r
    return r


def canonicalize(roots):
    """Fold constants and merge structurally identical sub-expressions, so
    the node tree only gets one Math node per distinct operation."""
    import math as _m
    table, memo = {}, {}
    folds = {'ADD': lambda a, b: a + b, 'SUBTRACT': lambda a, b: a - b,
             'MULTIPLY': lambda a, b: a * b,
             'DIVIDE': lambda a, b: a / b if b else 0.0,
             'SQRT': lambda a: _m.sqrt(max(a, 0.0)), 'SINE': _m.sin,
             'COSINE': _m.cos, 'TANGENT': _m.tan,
             'MINIMUM': min, 'MAXIMUM': max,
             'GREATER_THAN': lambda a, b: 1.0 if a > b else 0.0,
             'ABSOLUTE': abs, 'EXPONENT': _m.exp}

    def canon(e):
        if id(e) in memo:
            return memo[id(e)]
        if e.op in ('const', 'var'):
            key = (e.op, e.value)
            r = table.setdefault(key, e)
        else:
            args = [canon(a) for a in e.args]
            if all(a.op == 'const' for a in args):
                r = canon(Ex('const', value=float(folds[e.op](*[a.value for a in args]))))
            elif e.op == 'MULTIPLY' and any(a.op == 'const' and a.value == 1.0 for a in args):
                r = next(a for a in args if not (a.op == 'const' and a.value == 1.0))
            elif e.op in ('ADD', 'SUBTRACT') and args[1].op == 'const' and args[1].value == 0.0:
                r = args[0]
            else:
                key = (e.op,) + tuple(id(a) for a in args)
                r = table.get(key)
                if r is None:
                    r = Ex(e.op, args)
                    table[key] = r
        memo[id(e)] = r
        return r

    return {k: canon(v) for k, v in roots.items()}


def emit_shader_nodes(tree, roots, inputs, x0=0, y0=0):
    """Create ShaderNodeMath nodes for the graph. inputs: var name -> output
    socket. Returns root name -> output socket (or a float for constants)."""
    nodes, links = tree.nodes, tree.links
    made = {}
    order = []

    def visit(e, depth):
        if id(e) in made:
            return
        made[id(e)] = None
        for a in e.args:
            visit(a, depth + 1)
        order.append(e)

    for r in roots.values():
        visit(r, 0)
    level = {}
    for e in order:
        level[id(e)] = 1 + max([level[id(a)] for a in e.args] or [0])
    count = {}
    for e in order:
        if e.op == 'const':
            made[id(e)] = e.value
            continue
        if e.op == 'var':
            made[id(e)] = inputs[e.value]
            continue
        n = nodes.new('ShaderNodeMath')
        n.operation = e.op
        n.hide = True
        lv = level[id(e)]
        n.location = (x0 + lv * 160, y0 - count.get(lv, 0) * 30)
        count[lv] = count.get(lv, 0) + 1
        for i, a in enumerate(e.args):
            src = made[id(a)]
            if isinstance(src, float):
                n.inputs[i].default_value = src
            else:
                links.new(src, n.inputs[i])
        made[id(e)] = n.outputs[0]
    return {k: made[id(v)] for k, v in roots.items()}
def _group_socket(group, name, in_out):
    if hasattr(group, 'interface'):
        return group.interface.new_socket(name=name, in_out=in_out,
                                          socket_type='NodeSocketFloat')
    coll = group.inputs if in_out == 'INPUT' else group.outputs
    return coll.new('NodeSocketFloat', name)


EYE_GROUP_VERSION = 13

# Scale applied to the Eyes control's position before the look
# interpolator (x, y). (1.0, 1.0) = the clip's values as stored, which is
# what matches the game (McQueen's select_idle irises sit low-left exactly
# where eyeBallScale * sin(look) puts them). None = scale by the rig's
# Eyes_Frame_Proxy instead. Editable afterwards as the look_x_scale /
# look_y_scale Value nodes in the eye material.
EYE_LOOK_SCALE = (1.0, 1.0)
_EYE_OUTPUTS = ('iris_u', 'iris_v', 'iris_mask', 'lid', 'line', 'line_color', 'sclera',
                'lid_u', 'lid_v')


def _get_eye_group(double=False):
    """The shared "Cars2 Eye" node group (or "Cars2 Double Eye" for
    car_double_eyes materials): UV of the eye strip + parameters in, iris
    coordinates and lid/line masks out."""
    gname = 'Cars2 Double Eye' if double else 'Cars2 Eye'
    group = bpy.data.node_groups.get(gname)
    if group is not None and group.get('cars2_eye_version') == EYE_GROUP_VERSION:
        return group
    group = bpy.data.node_groups.new(gname, 'ShaderNodeTree')
    group['cars2_eye_version'] = EYE_GROUP_VERSION
    for name in EYE_PARAMS + list(EYE_CALIBRATION):
        _group_socket(group, name, 'INPUT')
    for name in _EYE_OUTPUTS:
        _group_socket(group, name, 'OUTPUT')
    nodes, links = group.nodes, group.links
    gin = nodes.new('NodeGroupInput')
    gin.location = (-400, 0)
    gout = nodes.new('NodeGroupOutput')
    uv = nodes.new('ShaderNodeUVMap')
    uv.location = (-400, 300)
    sep = nodes.new('ShaderNodeSeparateXYZ')
    sep.location = (-200, 300)
    links.new(uv.outputs[0], sep.inputs[0])
    inputs = {name: gin.outputs[name] for name in EYE_PARAMS + list(EYE_CALIBRATION)}
    inputs['u'], inputs['v'] = sep.outputs[0], sep.outputs[1]
    roots = canonicalize(build_eye_graph(double=double))
    sockets = emit_shader_nodes(group, roots, inputs)
    gout.location = (6000, 0)
    for name in _EYE_OUTPUTS:
        src = sockets[name]
        if isinstance(src, float):
            val = nodes.new('ShaderNodeValue')
            val.outputs[0].default_value = src
            src = val.outputs[0]
        links.new(src, gout.inputs[name])
    return group


def _mix_color(tree, fac, a, b, loc):
    """Colour mix that works on both the old MixRGB and the 3.4+ Mix node.
    fac/a/b may be sockets or plain values."""
    try:
        n = tree.nodes.new('ShaderNodeMix')
        n.data_type = 'RGBA'
        fac_in = n.inputs[0]
        a_in, b_in = [s for s in n.inputs if s.type == 'RGBA'][:2]
        out = next(s for s in n.outputs if s.type == 'RGBA')
    except (RuntimeError, AttributeError, StopIteration, ValueError):
        n = tree.nodes.new('ShaderNodeMixRGB')
        fac_in, a_in, b_in, out = n.inputs[0], n.inputs[1], n.inputs[2], n.outputs[0]
    n.location = loc
    for sock, val in ((fac_in, fac), (a_in, a), (b_in, b)):
        if hasattr(val, 'is_output'):
            tree.links.new(val, sock)
        elif sock.type == 'RGBA':
            c = list(val) if hasattr(val, '__len__') else [val] * 3
            sock.default_value = (c + [1.0])[:4] if len(c) == 3 else c
        else:
            sock.default_value = val
    return out


def _driver(sock, arm_obj, variables, expression):
    fc = sock.driver_add('default_value')
    drv = fc.driver
    drv.type = 'SCRIPTED'
    for name, path in variables:
        var = drv.variables.new()
        var.name = name
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'OBJECT'
        var.targets[0].id = arm_obj
        var.targets[0].data_path = path
    drv.expression = expression


def _rbf_expression(keys, sds, weights):
    """Gaussian radial basis: sum w_i * exp(-|p - k_i|^2 / (2 sd_i^2)),
    as plain arithmetic so Blender's fast expression evaluator runs it."""
    terms = []
    for i, w in enumerate(weights):
        kx, ky = keys[3 * i], keys[3 * i + 1]
        c = 1.0 / (2.0 * sds[i] * sds[i])
        terms.append('%r*exp(-((x-(%r))*(x-(%r))+(y-(%r))*(y-(%r)))*%r)' % (
            w, kx, kx, ky, ky, c))
    return '+'.join(terms) or '0'


def build_eye_nodes(tree, props, double, iris_img, lid_img, x0=0, y0=0, bsdf=True, textured=None):
    """The eye nodes in tree: the "Cars2 Eye" group with the material's eye
    parameters, the iris, outline and lids composed as the game draws them.
    props: {parameter: value} of the material. With bsdf, ends in a Principled
    BSDF + Material Output (the eye material of the import).
    -> {'group': group node, 'color': the eye with its lids (albedo),
        'rgb', 'alpha': renderedEyeTexture as the game draws it (lids rgb =
        their texture or black, alpha 0)}"""
    def at(x, y):
        return (x0 + x, y0 + y)
    gnode = tree.nodes.new('ShaderNodeGroup')
    gnode.node_tree = _get_eye_group(double)
    gnode.location = at(-900, 0)
    gnode.width = 260
    for name in EYE_PARAMS:
        v = props.get(name)
        if isinstance(v, (list, tuple)) and v:
            v = v[0]
        gnode.inputs[name].default_value = float(v) if isinstance(v, (int, float)) else 0.0
    for name, v in EYE_CALIBRATION.items():
        gnode.inputs[name].default_value = v

    out = gnode.outputs
    comb = tree.nodes.new('ShaderNodeCombineXYZ')
    comb.location = at(-550, 200)
    tree.links.new(out['iris_u'], comb.inputs[0])
    tree.links.new(out['iris_v'], comb.inputs[1])
    if iris_img is not None:
        img = tree.nodes.new('ShaderNodeTexImage')
        img.image = iris_img
        img.extension = 'CLIP'
        img.location = at(-380, 250)
        tree.links.new(comb.outputs[0], img.inputs[0])
        iris_col, iris_alpha = img.outputs['Color'], img.outputs['Alpha']
    else:
        iris_col, iris_alpha = (0.05, 0.05, 0.05), 1.0
    a = tree.nodes.new('ShaderNodeMath')
    a.operation = 'MULTIPLY'
    a.location = at(-150, 150)
    tree.links.new(out['iris_mask'], a.inputs[0])
    if hasattr(iris_alpha, 'is_output'):
        tree.links.new(iris_alpha, a.inputs[1])
    else:
        a.inputs[1].default_value = iris_alpha
    col = _mix_color(tree, a.outputs[0], out['sclera'], iris_col, at(50, 100))

    lid_color = props.get('diffuseColorA1')
    lid_color = list(lid_color) if isinstance(lid_color, (list, tuple)) and len(lid_color) >= 3 else [1.0, 1.0, 1.0]
    limg = None
    if lid_img is not None:
        limg = tree.nodes.new('ShaderNodeTexImage')
        limg.image = lid_img
        limg.location = at(-380, -250)
        # The game's own lid texture coordinates (EyeLidsTextured).
        lcomb = tree.nodes.new('ShaderNodeCombineXYZ')
        lcomb.location = at(-550, -250)
        tree.links.new(out['lid_u'], lcomb.inputs[0])
        tree.links.new(out['lid_v'], lcomb.inputs[1])
        tree.links.new(lcomb.outputs[0], limg.inputs[0])
        lid = _mix_color(tree, 1.0, limg.outputs['Color'], lid_color[:3], at(-100, -250))
        # (factor 1 + "MIX" just passes B; multiply the tint in:)
        try:
            lid.node.blend_type = 'MULTIPLY'
        except AttributeError:
            pass
    else:
        lid = lid_color[:3]
    # Game draw order into renderedEyeTexture: eyeballs, then the outline
    # band (solid grey), then the lids; lid areas end up showing the lid.
    col = _mix_color(tree, out['line'], col, out['line_color'], at(250, 0))
    line_col = col
    col = _mix_color(tree, out['lid'], col, lid, at(450, 0))
    # v1.96: renderedEyeTexture exactly as the game draws it, for the game's eye shader (c2game): the lids
    # are their texture (eyelidstextured) or black (eyelids), with alpha 0; the shader shows its lid colour
    # there
    lid_raw = limg.outputs['Color'] if limg is not None else (0.0, 0.0, 0.0)
    rend = _mix_color(tree, out['lid'], line_col, lid_raw, at(450, -250))
    rend.node.name = 'cars2_eye_rgb'
    ra = tree.nodes.new('ShaderNodeMath')
    ra.operation = 'SUBTRACT'
    ra.name = 'cars2_eye_alpha'
    ra.location = at(450, -450)
    ra.inputs[0].default_value = 1.0
    tree.links.new(out['lid'], ra.inputs[1])

    if bsdf:
        b = tree.nodes.new('ShaderNodeBsdfPrincipled')
        b.location = at(650, 0)
        tree.links.new(col, b.inputs['Base Color'])
        # Flat-colour lids (no diffuseMapA1, e.g. McQueen): Principled's
        # default sheen washes diffuseColorA1 out, so fully metallic + fully
        # rough keeps it at full strength. Textured lids (diffuseMapA1, e.g.
        # Mater) look right at metallic 0, roughness 1.
        if textured is None:
            textured = lid_img is not None
        if 'Metallic' in b.inputs:
            b.inputs['Metallic'].default_value = EYE_METALLIC_TEXTURED if textured else EYE_METALLIC
        if 'Roughness' in b.inputs:
            b.inputs['Roughness'].default_value = EYE_ROUGHNESS_TEXTURED if textured else EYE_ROUGHNESS
        mout = tree.nodes.new('ShaderNodeOutputMaterial')
        mout.location = at(950, 0)
        tree.links.new(b.outputs[0], mout.inputs[0])
    return {'group': gnode, 'color': col, 'rgb': rend, 'alpha': ra.outputs[0]}


def drive_eye_node(tree, gnode, arm_obj, plan, x0=0, y0=0):
    """Drive the eye group's parameters from the rig (eyelid sliders and the
    Eyes control's look interpolator). -> number of parameters driven."""
    if arm_obj is None or not plan:
        return 0
    linked = 0
    for label, param, rest in plan.get('eye_links', []):
        if param not in gnode.inputs:
            continue
        if label not in arm_obj.keys():
            # The clip's F-curve needs the property to exist. Start it at
            # the control's rest value, which is also what the animation
            # add-on resets sliders to before each clip.
            arm_obj[label] = rest
            try:                       # eyelids open until a clip keys them
                from . import anim as _anim
                if not rest:
                    arm_obj[label] = _anim.scalar_rest(label)
            except ImportError:
                pass
        # v1.88: remember the value the model shows on import, so Remove
        # Clip (and the reset before each clip) puts the face back to it
        # instead of the generic open-eyes pose (anim.scalar_rest).
        try:
            rests = json.loads(arm_obj.get(SLIDER_REST_PROP, "{}"))
        except (TypeError, ValueError):
            rests = {}
        rests.setdefault(label, float(arm_obj[label]))
        arm_obj[SLIDER_REST_PROP] = json.dumps(rests)
        _driver(gnode.inputs[param], arm_obj, [('v', '["%s"]' % label)], 'v')
        linked += 1
    rbf = plan.get('eye_rbf')
    if rbf and rbf.get('bone') and rbf['bone'] in arm_obj.pose.bones:
        path = 'pose.bones["%s"].location' % rbf['bone']
        look_in = {}
        for i, axis in enumerate(('look_x', 'look_y')):
            val = tree.nodes.new('ShaderNodeValue')
            val.name = val.label = axis
            val.location = (x0 - 1700, y0 + 300 - 200 * i)
            _driver(val.outputs[0], arm_obj, [('v', '%s[%d]' % (path, i))], 'v')
            # Calibration: how the Eyes control's position maps onto the
            # interpolator's -1..1 key grid. 1.0 = raw values.
            k = tree.nodes.new('ShaderNodeValue')
            k.name = k.label = axis + '_scale'
            k.location = (x0 - 1700, y0 + 230 - 200 * i)
            k.outputs[0].default_value = (EYE_LOOK_SCALE or rbf.get('frame_scale') or (1.0, 1.0))[i]
            mul = tree.nodes.new('ShaderNodeMath')
            mul.operation = 'MULTIPLY'
            mul.location = (x0 - 1500, y0 + 300 - 200 * i)
            tree.links.new(val.outputs[0], mul.inputs[0])
            tree.links.new(k.outputs[0], mul.inputs[1])
            look_in[axis] = mul.outputs[0]
        roots = canonicalize(build_look_graph(rbf['keys'], rbf['sds'], rbf['outputs']))
        sockets = emit_shader_nodes(tree, roots, look_in, x0=x0 - 1350, y0=y0 + 600)
        for param, sock in sockets.items():
            if param in gnode.inputs and not isinstance(sock, float):
                tree.links.new(sock, gnode.inputs[param])
                linked += 1
    return linked


def setup_eye_materials(data, materials, meshes, arm_obj, plan,
                        textures_folder, import_textures):
    """Turn each car_eyes_* material into the reconstructed eye shader and
    drive its parameters from the armature (clip channels + look RBF)."""
    tex_cache = {}
    for idx, mat_data in enumerate(data.materials):
        effect = str(mat_data.get('Effect', ''))
        # car_eyes_* (one look offset, lids with a centre notch) or
        # car_double_eyes_* (a look offset per eye, lids meeting mid-strip).
        if not effect.startswith(('car_eyes', 'car_double_eyes')):
            continue
        double = effect.startswith('car_double_eyes')
        if idx >= len(materials):
            continue
        mat = materials[idx]
        props = {}
        for e in pool_list(mat_data.get('PropertyEntries')):
            props[e.get('Name')] = e.get('Value')
        tex = {te.get('Usage'): te.get('TextureReference')
               for te in pool_list(mat_data.get('TextureEntries'))}
        iris_img = lid_img = None
        if import_textures:
            if tex.get('eyeBallTexture') is not None:
                iris_img = load_diffuse_texture(tex['eyeBallTexture'], data, textures_folder, tex_cache)
            if tex.get('diffuseMapA1') is not None:
                lid_img = load_diffuse_texture(tex['diffuseMapA1'], data, textures_folder, tex_cache)
        objs = [o for o in meshes if o.type == 'MESH' and mat.name in [m.name for m in o.data.materials if m]]

        mat.use_nodes = True
        tree = mat.node_tree
        tree.nodes.clear()
        built = build_eye_nodes(tree, props, double, iris_img, lid_img, textured=tex.get('diffuseMapA1') is not None)
        gnode = built['group']
        # v2.0.59: the images, for the eye graft into the game-material rebuild (eyelook.py)
        mat['cars2_eye_iris'] = iris_img.name if iris_img is not None else ''
        mat['cars2_eye_lid'] = lid_img.name if lid_img is not None else ''
        linked = drive_eye_node(tree, gnode, arm_obj, plan)
        if arm_obj is None or not plan:
            continue
        log("Eyes: '%s' rebuilt as the reconstructed eye shader, %d parameters "
            "driven from the rig (eye size %.3f x %.3f of the strip)." % (
                mat.name, linked, gnode.inputs['eyeBallScale_u'].default_value,
                gnode.inputs['eyeBallScale_v'].default_value))


_LAST_RIG_PLAN = None


# ---------------------------------------------------------------------------
# Mesh building
#
# Several SceneTreeNodes can share one MaterialReference (e.g. a character's
# head or limbs are frequently split across many nodes that all point at the
# same material -- likely per-submesh draw calls in the original engine).
# Blender doesn't need that split, so geometry is first extracted per node
# into plain Python data, then nodes are grouped by MaterialReference and
# merged into a single mesh per group before any bpy object is created.
# ---------------------------------------------------------------------------

def extract_node_geometry(node_index, node, data, vbuf, ibuf):
    """Read one SceneTreeNode's vertex/index data into plain lists, with the
    node's full accumulated world matrix (its own LocalToParentMatrix
    combined with every ancestor's, via data.world_matrix) already baked
    into positions/normals so that geometry from different nodes can be
    merged safely regardless of where each node sits in the scene tree."""
    name = node.get('NodeName', 'Mesh')
    vstream_refs = node.get('VertexStreamReferences')
    istream_ref = node.get('IndexStreamReference')
    if vstream_refs is None or istream_ref is None:
        return None
    # v1.90: hand-edited ports switch a mesh node off by pointing it at a
    # stream that doesn't exist (claudescruggs' Glass/Headlights/BrakeLights
    # -> index streams 30/50/60 of 4, elmcqueen's Headlights -> 500000,
    # jacksonstorm's Glass -> 5 of 5). The game draws nothing for such a node
    # and the cars run fine, so the import skips it instead of failing.
    bad = [r for r in list(vstream_refs) if not isinstance(r, int) or not 0 <= r < len(data.vertex_streams)]
    if not isinstance(istream_ref, int) or not 0 <= istream_ref < len(data.index_streams):
        bad.append('index %s' % istream_ref)
    # ...or at a scene component (its skin palette) past the end of the pool:
    # fernando's Headlights -> component 7 of 7.
    bad += ['component %s' % c for c in (node.get('SceneComponentReferences') or [])
            if not isinstance(c, int) or not 0 <= c < len(data.scene_components)]
    if bad:
        log("Skipped mesh node '%s': it points at stream(s) %s that the file doesn't have "
            "(%d vertex / %d index streams) - switched off, the game doesn't draw it." % (
                name, ', '.join(str(b) for b in bad), len(data.vertex_streams), len(data.index_streams)))
        return None

    positions, uvs, normals, bone_idx, bone_wt = [], [], [], [], []
    vertex_count = 0

    for stream_index in vstream_refs:
        stream = data.vertex_streams[stream_index]
        stride = stream.get('Width', 0) + stream.get('ExtraStride', 0)
        vertex_count = stream.get('Length', 0)
        vb_ = vbuf.get(int(stream.get('VertexBufferReference', 0) or 0)) if isinstance(vbuf, dict) else vbuf
        if vb_ is None:
            log("Skipped mesh node '%s': its vertex stream %d is in buffer %s, which the file doesn't name." % (
                name, stream_index, stream.get('VertexBufferReference')))
            return None
        vb_.seek(stream.get('VertexBufferOffset', 0))
        raw = vb_.read(stride * vertex_count)
        if len(raw) < stride * vertex_count:
            log("Skipped mesh node '%s': its vertex stream %d needs %d bytes at offset %d, "
                "the vertex buffer ends before that." % (
                    name, stream_index, stride * vertex_count, stream.get('VertexBufferOffset', 0)))
            return None

        elements = pool_list(stream.get('Elements'))
        decoded = decode_stream_numpy(raw, stride, vertex_count, elements)

        if decoded is not None:
            # Vectorized path (numpy): each element is pulled for the whole
            # stream in one call instead of once per vertex in Python. This
            # is the part that matters most on map-sized files, which can
            # have hundreds of thousands of vertex entries across a
            # thousand-plus streams -- the old per-vertex loop was the main
            # source of long stalls (and, on very large maps, crashes) when
            # importing whole levels rather than a single character.
            if 'pos' in decoded and not positions:
                positions.extend(map(tuple, decoded['pos'].tolist()))
            if 'uv' in decoded and not uvs:
                uvs.extend(map(tuple, decoded['uv'].tolist()))
            if 'normal' in decoded and not normals:
                # Sliced to the first 3 components -- the arcade-build Type
                # 45 normal decodes as 4 values (xyz + an unused padding
                # w), while the original Type 3 normal is already 3.
                normals.extend(map(tuple, decoded['normal'][:, :3].tolist()))
            if 'boneidx' in decoded and not bone_idx:
                bone_idx.extend(map(tuple, decoded['boneidx'].tolist()))
            if 'boneweight' in decoded and not bone_wt:
                bone_wt.extend(map(tuple, decoded['boneweight'].tolist()))
            continue

        # Fallback: original per-vertex/per-element Python decode. Only hit
        # if numpy isn't available in this Blender's bundled Python (it
        # normally is, from 2.8 onward).
        for el in elements:
            t = el.get('Type')
            off = el.get('Offset', 0)
            el_name = el.get('Name')
            info = resolve_element(t, el_name)
            if info is None:
                continue
            kind = info[0]
            for i in range(vertex_count):
                base = i * stride
                val = read_element(raw, base, off, t, el_name)
                if val is None:
                    continue
                if kind == 'pos':
                    if i >= len(positions):
                        positions.append(val)
                elif kind == 'uv':
                    if i >= len(uvs):
                        uvs.append(val)
                elif kind == 'normal':
                    if i >= len(normals):
                        normals.append(val[:3])  # drop Type 45's unused w
                elif kind == 'boneidx':
                    if i >= len(bone_idx):
                        bone_idx.append(val)
                elif kind == 'boneweight':
                    if i >= len(bone_wt):
                        bone_wt.append(val)

    if not positions:
        return None

    # Index buffer: element byte-width comes from the IndexBufferPool entry,
    # not from the IndexStream itself.
    istream = data.index_streams[istream_ref]
    index_count = istream.get('Length', 0)
    buf_ref = istream.get('IndexBufferReference', 0)
    width = data.index_buffers[buf_ref].get('Width', 2) if buf_ref < len(data.index_buffers) else 2
    idx_fmt = {1: '<B', 2: '<H', 4: '<I'}.get(width, '<H')

    ib_ = ibuf.get(int(buf_ref or 0)) if isinstance(ibuf, dict) else ibuf
    if ib_ is None:
        log("Skipped mesh node '%s': its index stream is in buffer %s, which the file doesn't name." % (name, buf_ref))
        return None
    ib_.seek(istream.get('IndexBufferOffset', 0))
    raw = ib_.read(width * index_count)
    if len(raw) < width * index_count:
        log("Skipped mesh node '%s': its index stream needs %d bytes, the index buffer ends "
            "before that." % (name, width * index_count))
        return None
    flat = [struct.unpack_from(idx_fmt, raw, k * width)[0] for k in range(index_count)]
    faces = [tuple(flat[i:i + 3]) for i in range(0, len(flat) - 2, 3)]

    # Bake this node's full world matrix (own LocalToParentMatrix combined
    # with every ancestor's) into positions/normals now, so merged geometry
    # from multiple nodes doesn't need per-node transforms later.
    matrix = data.world_matrix(node_index)
    if matrix != Matrix.Identity(4):
        positions = [tuple(matrix @ Vector(p)) for p in positions]
        if normals:
            mat3 = matrix.to_3x3()
            normals = [tuple((mat3 @ Vector(n)).normalized()) for n in normals]

    # Resolve bone weights to (bone_name, weight) pairs per vertex now,
    # while we still have this node's own RemapData at hand -- different
    # nodes can use different remaps even when merged into one mesh later.
    bone_assign = None
    if bone_idx and bone_wt:
        scene_refs = node.get('SceneComponentReferences')
        remap = None
        if scene_refs:
            comp = data.scene_components[scene_refs[0]]
            remap = comp.get('RemapData')
        if remap:
            bone_assign = []
            for idxs, wts in zip(bone_idx, bone_wt):
                pairs = []
                for local_idx, w in zip(idxs, wts):
                    if w == 0 or local_idx >= len(remap):
                        continue
                    bone_id = remap[local_idx]
                    bname = data.bone_id_to_name.get(bone_id)
                    if bname:
                        pairs.append((bname, w / 255.0))
                bone_assign.append(pairs)

    uvs = uvs if uvs and len(uvs) == len(positions) else None
    normals = normals if normals and len(normals) == len(positions) else None
    positions, faces, uvs, normals, bone_assign = drop_unused_vertices(
        positions, faces, uvs, normals, bone_assign)
    if not faces or not positions:
        return None

    return {
        'name': name,
        'positions': positions,
        'uvs': uvs,
        'normals': normals,
        'faces': faces,
        'mat_ref': node.get('MaterialReference'),
        'bone_assign': bone_assign,
    }


def drop_unused_vertices(positions, faces, uvs, normals, bone_assign):
    """Remove vertices that no face references, remapping face indices to
    match. Merging meshes that contain such "loose" vertices and then
    calling mesh.normals_split_custom_set_from_vertices() is a confirmed
    crash (native access violation inside Blender's own custom-normals
    code, not something a Python try/except can catch) -- this data has
    them very commonly (roughly 3 out of 4 mesh nodes on the mi_oil map
    have vertex-stream entries beyond what any face indexes), so unused
    vertices are stripped per-node before anything gets merged or built
    into a bpy mesh."""
    used = set()
    for f in faces:
        used.update(f)
    if len(used) == len(positions):
        return positions, faces, uvs, normals, bone_assign  # nothing to drop

    remap = {}
    new_positions, new_uvs, new_normals, new_bone_assign = [], [], [], []
    for old_idx in sorted(used):
        remap[old_idx] = len(new_positions)
        new_positions.append(positions[old_idx])
        if uvs is not None:
            new_uvs.append(uvs[old_idx])
        if normals is not None:
            new_normals.append(normals[old_idx])
        if bone_assign is not None:
            new_bone_assign.append(bone_assign[old_idx])

    new_faces = [tuple(remap[i] for i in f) for f in faces]
    return (
        new_positions,
        new_faces,
        new_uvs if uvs is not None else None,
        new_normals if normals is not None else None,
        new_bone_assign if bone_assign is not None else None,
    )


def merge_geometries(geoms):
    """Concatenate several extracted-node geometries (already in the same
    space) into one, offsetting face indices as needed."""
    use_uv = all(g['uvs'] is not None for g in geoms)
    use_normal = all(g['normals'] is not None for g in geoms)
    use_bone = any(g['bone_assign'] is not None for g in geoms)

    positions, faces, uvs, normals, bone_assign = [], [], [], [], []
    for g in geoms:
        offset = len(positions)
        positions.extend(g['positions'])
        faces.extend(tuple(i + offset for i in f) for f in g['faces'])
        if use_uv:
            uvs.extend(g['uvs'])
        if use_normal:
            normals.extend(g['normals'])
        if use_bone:
            if g['bone_assign'] is not None:
                bone_assign.extend(g['bone_assign'])
            else:
                bone_assign.extend([[]] * len(g['positions']))

    return {
        'positions': positions,
        'faces': faces,
        'uvs': uvs if use_uv else None,
        'normals': normals if use_normal else None,
        'bone_assign': bone_assign if use_bone else None,
    }


def build_mesh_object(name, merged, mat_ref, data, materials, armature_obj):
    """Build a single Blender mesh object from an already-merged geometry
    dict (see merge_geometries)."""
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(merged['positions'], [], merged['faces'])
    mesh.update()

    if merged['uvs']:
        uv_layer = mesh.uv_layers.new(name="Uv1")     # v2.0: the game's first UV set, as the Scene Editor names it
        if HAVE_NUMPY:
            # Vectorized: pull each loop's vertex index in one call, gather
            # the matching UVs, then write the whole layer in one call.
            # The old per-loop "uv_layer.data[loop.index].uv = ..." Python
            # loop is by far the slowest part of the import on map-sized
            # meshes (hundreds of thousands of loops) -- this replaces it.
            loop_count = len(mesh.loops)
            loop_vert_idx = np.empty(loop_count, dtype=np.int64)
            mesh.loops.foreach_get('vertex_index', loop_vert_idx)
            uv_arr = np.array(merged['uvs'], dtype=np.float32)
            uv_arr[:, 1] = 1.0 - uv_arr[:, 1]
            loop_uvs = uv_arr[loop_vert_idx]
            uv_layer.data.foreach_set('uv', loop_uvs.ravel())
        else:
            for loop in mesh.loops:
                u, v = merged['uvs'][loop.vertex_index]
                uv_layer.data[loop.index].uv = (u, 1.0 - v)

    if merged['normals'] and USE_SOURCE_NORMALS:
        # Custom normals are stored relative to Blender's own normal for each
        # "fan" of corners around a vertex, and follow that fan when the mesh
        # deforms. from_pydata leaves every face flat-shaded, which makes
        # every corner its own fan tied to its own face: correct at rest,
        # but under deformation each face drags its corner's normal along
        # separately, which is the faceted look around the mouth when it
        # stretches. Smooth faces (and, before Blender 4.1, auto smooth at
        # 180 degrees so no edge gets split by angle) give one shared fan per
        # vertex, so the source normals bend with the surface as a whole.
        mesh.polygons.foreach_set('use_smooth', [True] * len(mesh.polygons))
        if hasattr(mesh, 'use_auto_smooth'):
            mesh.use_auto_smooth = True
            mesh.auto_smooth_angle = math.pi
        try:
            mesh.normals_split_custom_set_from_vertices(merged['normals'])
        except Exception as e:
            log("WARNING: could not apply custom normals for %s: %s" % (name, e))

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    # Node-local matrices were already baked into the vertex data during
    # extraction, so the merged object itself sits at identity.
    obj.matrix_local = Matrix.Identity(4)

    if mat_ref is not None and materials and 0 <= mat_ref < len(materials):
        obj.data.materials.append(materials[mat_ref])

    if merged['bone_assign'] and armature_obj:
        for vi, pairs in enumerate(merged['bone_assign']):
            for bname, w in pairs:
                if bname not in obj.vertex_groups:
                    obj.vertex_groups.new(name=bname)
                obj.vertex_groups[bname].add([vi], w, 'ADD')
        mod = obj.modifiers.new("Armature", 'ARMATURE')
        mod.object = armature_obj
        obj.parent = armature_obj

    used_source_normals = bool(merged['normals'] and USE_SOURCE_NORMALS)
    return obj, used_source_normals


def build_merged_mesh_objects(data, materials, vbuf, ibuf, armature_obj):
    """Extract geometry for every mesh node, group nodes that share a
    MaterialReference, and build one Blender mesh object per group."""
    geoms_by_key = {}
    order = []  # preserve first-seen order of each group for stable naming/log

    for node_index, node in data.mesh_nodes:
        g = extract_node_geometry(node_index, node, data, vbuf, ibuf)
        if g is None:
            continue
        mat_ref = g['mat_ref']
        # Nodes with no material at all aren't considered to "share" one,
        # so each keeps its own group instead of merging together.
        key = mat_ref if mat_ref is not None else ('nomat', id(node))
        if key not in geoms_by_key:
            geoms_by_key[key] = []
            order.append(key)
        geoms_by_key[key].append(g)

    created = []
    needs_auto_smooth = []
    for key in order:
        geoms = geoms_by_key[key]
        mat_ref = key if isinstance(key, int) else None

        if mat_ref is not None and materials and 0 <= mat_ref < len(materials):
            name = materials[mat_ref].name
        else:
            name = geoms[0]['name']

        merged = merge_geometries(geoms)
        obj, used_source_normals = build_mesh_object(name, merged, mat_ref, data, materials, armature_obj)
        created.append(obj)
        # v2.0.57: helper geometry the game never draws (SolidColor 'lambert' quads on boatsail02 and
        # bombtruck_container_damaged; shadowonly proxies) - shown as wire and left out of renders, tagged like the
        # world importer / Scene Editor tag the world's Lamberts. Still built, so exports keep it.
        if mat_ref is not None and 0 <= mat_ref < len(data.materials):
            effect = str(data.materials[mat_ref].get('Effect', ''))
            if effect in ('SolidColor', 'shadowonly'):
                obj['c2_helper'] = 'shadow' if effect == 'shadowonly' else 'hidden'
                obj.display_type = 'WIRE'
                obj.hide_render = True
        if not used_source_normals:
            needs_auto_smooth.append(obj)

        if len(geoms) > 1:
            log("Merged %d node(s) sharing material index %s into '%s' (%d verts)." % (
                len(geoms), mat_ref, name, len(merged['positions'])))

    # Applied once, batched, over every object that didn't get the source
    # data's exact per-vertex normals -- see USE_SOURCE_NORMALS above for
    # why that path is avoided by default. A single shade_auto_smooth call
    # over all selected objects is also far cheaper than calling it (or the
    # old custom-normals call) once per object on map-sized imports.
    if needs_auto_smooth:
        try:
            with bpy.context.temp_override(
                    active_object=needs_auto_smooth[0],
                    selected_editable_objects=needs_auto_smooth):
                try:
                    bpy.ops.object.shade_auto_smooth(angle=math.radians(AUTO_SMOOTH_ANGLE_DEGREES))
                except (AttributeError, TypeError, RuntimeError):
                    # Fall back for Blender versions/builds where the
                    # operator name or signature differs slightly -- plain
                    # smooth shading is a safe, crash-free default even if
                    # angle-based auto-smooth isn't available here.
                    bpy.ops.object.shade_smooth()
        except Exception as e:
            log("WARNING: could not apply smooth shading: %s" % e)

    return created


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # Disabling undo push for the duration of the import matters a lot on
    # map-sized files: with 1000+ new objects/meshes/materials/images
    # created in one script run, Blender's per-step undo snapshots can pile
    # up a large amount of extra memory and were a contributor to
    # crashes/freezes on large levels. Restored in `finally` regardless of
    # success or failure.
    prev_undo = bpy.context.preferences.edit.use_global_undo
    bpy.context.preferences.edit.use_global_undo = False
    try:
        for label, path in (("JSON_PATH", JSON_PATH), ("VBUF_PATH", VBUF_PATH), ("IBUF_PATH", IBUF_PATH)):
            if not os.path.isfile(path):
                raise FileNotFoundError("%s does not exist: %s" % (label, path))

        data = OctData(JSON_PATH)
        textures_folder = guess_textures_folder(JSON_PATH)
        log("Looking for textures in: %s" % textures_folder)
        if IMPORT_TEXTURES and not os.path.isdir(textures_folder):
            log("WARNING: textures folder not found, skipping textures: %s" % textures_folder)
        if not HAVE_NUMPY:
            log("WARNING: numpy not available in this Blender's Python -- "
                "falling back to slower per-vertex decoding.")

        character_name = guess_character_name(JSON_PATH)

        with open(VBUF_PATH, 'rb') as vbuf, open(IBUF_PATH, 'rb') as ibuf:
            materials = load_materials(data, textures_folder, IMPORT_TEXTURES and os.path.isdir(textures_folder))
            armature_obj, _ = build_armature(data, name=character_name,
                                             json_path=JSON_PATH)

            created_meshes = build_merged_mesh_objects(data, materials, vbuf, ibuf, armature_obj)

        if BUILD_EYE_SHADER:
            try:
                setup_eye_materials(data, materials, created_meshes, armature_obj,
                                    _LAST_RIG_PLAN if armature_obj else None,
                                    textures_folder,
                                    IMPORT_TEXTURES and os.path.isdir(textures_folder))
            except Exception as e:  # never let the eyes stop a model import
                import traceback
                log("WARNING: eye shader skipped: %s" % e)
                log(traceback.format_exc())

        # The source data comes in lying down relative to Blender's Z-up
        # convention. Rather than a separate "ImportRoot" empty, the fix is
        # applied directly on the armature (named after the character) and
        # everything is parented under it -- if there's no armature at all,
        # each mesh gets the rotation applied to itself instead.
        if armature_obj:
            armature_obj.rotation_euler = (math.radians(GLOBAL_ROTATE_X_DEGREES), 0, 0)
            for obj in created_meshes:
                if obj.parent is None:
                    obj.parent = armature_obj
        else:
            for obj in created_meshes:
                obj.rotation_euler = (math.radians(GLOBAL_ROTATE_X_DEGREES), 0, 0)

        log("Import finished: %d mesh(es), %d bone(s), %d material(s)." % (
            len(created_meshes), len(data.bone_nodes), len(materials)))
    except Exception as e:
        log("FATAL ERROR: %s" % e)
        import traceback
        log(traceback.format_exc())
        raise
    finally:
        bpy.context.preferences.edit.use_global_undo = prev_undo
        flush_log()
        print("Full log also written to the 'OctImportLog' text block -- "
              "open it from the Text Editor's datablock dropdown.")


if __name__ == "__main__":
    main()
