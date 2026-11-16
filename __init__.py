bl_info = {
    "name": "Cars 2 Toolkit",
    "author": "Pentane project",
    "version": (1, 0, 0),
    "blender": (4, 1, 0),
    "location": "File > Import > Cars 2 Character (.oct), Cars 3 Character (.oct); File > Export > Cars 2 Character (.oct); 3D View > Sidebar > Cars 2 Character",
    "description": "Import Cars 2 characters from .oct, edit their game rig "
                   "(bones, constraints, driven keys, animation channels) and "
                   "import/export animation clips",
    "category": "Import-Export",
}

import os
import sys

import bpy

# Core toolkit (pure Python, no dependencies) - modules are prefixed c2* so
# they cannot clash with other add-ons.
_CORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core')
# The core modules load as top-level modules (c2geom, c2scene, ...), which
# Blender does not reload when the add-on is updated or re-enabled: the first
# version loaded in a session would keep running until a restart. Drop any
# loaded copies (and any older core folder on the path) so this version's
# code is the one used.
_CORE_MODULES = ('c2oct', 'c2headers', 'c2scene', 'c2ubercon', 'c2clipblock', 'c2channels',
                 'c2validate', 'c2rigedit', 'c2geom', 'c3toc2', 'c3anim', 'cars3_to_cars2',
                 'octread', 'clip_slot_map', 'port_car', 'c2fx',
                 # v2.0: the format readers of the material system (look/), copies of the Scene Editor's
                 'arcade', 'cubemap', 'd3dbc', 'dds', 'fxglsl', 'fxgraph', 'vertexfmt')
for _m in _CORE_MODULES:
    sys.modules.pop(_m, None)
# ...and this add-on's own files too: when Blender re-enables an add-on it
# reloads only this __init__, so rig.py / character.py ... would keep the old
# core modules while new code loads fresh ones - two copies of c2oct, and
# lists made by one look like plain lists to the other ("plain list for
# 'HeaderStrings'"). Everything loads fresh, together.
for _m in [m for m in sys.modules if m.startswith(__name__ + '.')]:
    del sys.modules[_m]
sys.path[:] = [p for p in sys.path
               if not (os.path.basename(p) == 'core' and os.path.isfile(os.path.join(p, 'c2geom.py'))
                       and os.path.abspath(p) != _CORE)]
if _CORE in sys.path:
    sys.path.remove(_CORE)
sys.path.insert(0, _CORE)
VERSION = '.'.join(str(x) for x in bl_info['version'])   # public build: no Cars 3 game shading (c3exact)

from . import anim          # noqa: E402  Cars 2 Animation (.oct) clip import/export
from . import character     # noqa: E402  character import + per-character state
from . import rig           # noqa: E402  rig editing panel + export (incl. meshes)
from . import cars3         # noqa: E402  Cars 3 character import (Cars 2 armature layout)
from . import cars3_export  # noqa: E402  Cars 3 character -> Cars 2 character export
from . import template      # noqa: E402  custom models rigged to a plain Cars 2 skeleton
from . import look          # noqa: E402  the Scene Editor's material system (Game Look, editing; v2.0)
from . import charmat       # noqa: E402  character materials as game materials, eyes, export (v2.0)
from . import c2selink      # noqa: E402  characters lit by a Cars 2 Scene Editor map's probes (v1.97)
from . import eyelook       # noqa: E402  Cars 2 eyes in Material Preview / Eevee / Cycles + lid shadow panel
from . import export_any    # noqa: E402  Export Cars 2 Character: one export for every kind of character

# the material system first: the other modules' data and panels use it
_modules = (look, anim, character, cars3, cars3_export, template, rig, charmat, c2selink, eyelook, export_any)


class CARS2TK_AP_preferences(bpy.types.AddonPreferences):
    bl_idname = __name__
    offsetting_path: bpy.props.StringProperty(
        name='offsetting.exe', subtype='FILE_PATH',
        description='The offsetting tool that packs Cars 2 octane zips (Export Character > Pack as game .zip). '
                    'Empty: looked for in the assets folder of the game')

    def draw(self, context):
        self.layout.prop(self, 'offsetting_path')
        if not self.offsetting_path:
            self.layout.label(text='Empty: found in the assets folder of the game when it is there', icon='INFO')


def register():
    bpy.utils.register_class(CARS2TK_AP_preferences)
    for m in _modules:
        m.register()


def unregister():
    for m in reversed(_modules):
        m.unregister()
    bpy.utils.unregister_class(CARS2TK_AP_preferences)
