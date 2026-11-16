"""The Cars 2 Scene Editor's material system, run by the toolkit when the Scene
Editor isn't enabled (v2.0).

Every other file in this folder is a copy of the Scene Editor's (its
tools/sync_look.py writes them; never edit them here): the game material
data (Material.c2: effect, parameters, texture slots), Game Look (the Cars 2
Game render engine in Rendered view, the effects' shaders as Eevee nodes),
the Blender material modes, the material operators (Pick Effect with its
dropped / kept / required texture slots, Pick Texture, Load Image ...) and
the Material panel's contents.

Both add-ons register these under the same names, so one copy runs at a time
(looklink.py): with the Scene Editor enabled the toolkit uses its modules, and
a material edited in either add-on's panel is the same data.
"""
import importlib
import sys

import bpy

from . import looklink

NAME = 'toolkit'
# registration order: data first, the engine last (as the Scene Editor does)
_REGISTER = ('props', 'lookui', 'fxnodes', 'matops', 'gamerender')
_state = {'registered': False}


def _reclaim():
    """v2.0.9: Blender empties bpy.app.driver_namespace on every file load
    (File > Open, File > New), which holds the record of who runs the material
    system - this copy stayed registered but looked 'not running'. Put the
    record back when it's gone and this copy is the one registered."""
    if looklink.owner() is not None:
        return
    if _state['registered']:
        looklink.claim(NAME, __name__, _release)
    elif _scene_editor_runs():
        # v2.0.20: the same for the Scene Editor's copy. looklink keeps the record across file loads now, but a
        # Scene Editor older than 0.29.6 doesn't: its copy is still registered, only the record is gone. Put
        # back in the namespace only (not looklink's kept copy, which an old Scene Editor would never clear)
        bpy.app.driver_namespace[looklink.KEY] = {'name': 'scene_editor', 'package': 'cars2_scene_editor',
                                                  'release': None}
    else:
        # nobody runs it: an old Scene Editor was disabled after a file load had dropped our 'waiting'
        # entry, so it couldn't hand over - take over now
        _take()


def _scene_editor_runs():
    """Is the Scene Editor's copy of the material system registered (whatever the records say)? Called only
    while this copy is not registered, so a registered Material.c2 with the Scene Editor loaded is its copy."""
    return ('cars2_scene_editor' in bpy.context.preferences.addons
            and 'cars2_scene_editor.props' in sys.modules
            and 'c2' in bpy.types.Material.bl_rna.properties)


@bpy.app.handlers.persistent
def _on_load(*_args):
    _reclaim()


def api():
    """The running copy's modules (fxnodes, gamerender, matops, matio, lookui ...)."""
    _reclaim()
    return looklink.modules()


def _take():
    """Register this copy (the Scene Editor's isn't registered)."""
    looklink.stop_waiting(NAME)
    if _state['registered']:
        return
    for name in _REGISTER:
        importlib.import_module('.' + name, __name__).register()
    _state['registered'] = True
    looklink.claim(NAME, __name__, _release)


def _release():
    """The Scene Editor is being enabled: unregister this copy, wait for it to go."""
    if _state['registered']:
        for name in reversed(_REGISTER):
            importlib.import_module('.' + name, __name__).unregister()
        _state['registered'] = False
    o = looklink.owner()
    if o is not None and o['name'] == NAME:
        del bpy.app.driver_namespace[looklink.KEY]
        looklink.forget(NAME)
    looklink.wait(NAME, _take)


def register():
    o = looklink.owner()
    if o is not None and o['name'] != NAME:
        looklink.wait(NAME, _take)          # the Scene Editor's copy runs
    else:
        _take()
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    looklink.stop_waiting(NAME)
    if _state['registered']:
        for name in reversed(_REGISTER):
            importlib.import_module('.' + name, __name__).unregister()
        _state['registered'] = False
        looklink.drop(NAME)
