# Architecture

## Layout

```
cars2_toolkit/
  __init__.py        bl_info, the core/ path setup, register order
  character.py       Cars 2 character import, per-character state (properties on the armature)
  c2_model.py        the model importer: OctData, armature (skin + control rig), eyes, meshes
  rig.py             Cars 2 Rig panel: record edits, validate, Export Character
  geometry.py        Blender meshes -> c2geom input (mesh export)
  anim.py            Cars 2 Animation: clip import / export, sliders, Cars 3 clips
  charmat.py         character materials as game materials, eyes, emissions, material export
  c2selink.py        light probes when the character is in a Scene Editor map
  template.py        custom rigs: a plain skeleton exported as a full Cars 2 character
  cars3.py           Cars 3 character / prop import
  cars3_export.py    an imported Cars 3 character exported as a Cars 2 character
  cars3_textures.py  Cars 3 textures, via the Cars 3 material decoder in c3look/
  look/              the material system: a copy of the Scene Editor's (never edit by hand)
  core/              pure-Python codecs and builders (no bpy)
  docs/              these pages
```

### `core/`

| Module | Role |
|---|---|
| `c2oct.py` | Lossless Tupperware codec (`.oct` / `.bent` / `.mer` / `.tfx`); keeps every value's type |
| `octread.py` | Small read-only OCT reader (same JSON shape as `offsetting oct decode`); used by the Cars 3 clip tools |
| `c2headers.py` | The header namespace of a SubNetwork (path ↔ id) |
| `c2scene.py` | Editable Scene SubNetwork addressed by path; regenerates every index on `build()` |
| `c2ubercon.py` | UberConstraintDataBlock codec |
| `c2clipblock.py` | ClipDataBlock codec and driven-key builder |
| `c2channels.py` | New animation channels: grows a connection template in `.bent`, scene and clips together |
| `c2rigedit.py` | Applies recorded edits to the source `.oct` / `.bent`; `export()` writes the package |
| `c2validate.py` | Checks a Scene SubNetwork against the rules every shipped rig follows |
| `c2geom.py` | Character geometry: read, verify, rebuild the pools and `.vbuf` / `.ibuf` |
| `c2fx.py` | Character shader maths written out, and `crc` (texture name hashes, used by material export). Its `apply_materials` is the older material path that `c2rigedit` still accepts (`materials['patches']`) |
| `c3toc2.py` | Cars 3 model → Cars 2 character (geometry on the Cars 2 rig) |
| `cars3_to_cars2.py`, `c3anim.py`, `clip_slot_map.py`, `port_car.py` | Cars 3 clips → Cars 2 clips (see `core/CARS3_ANIM_NOTES.md`) |
| `arcade.py`, `cubemap.py`, `d3dbc.py`, `dds.py`, `fxglsl.py`, `fxgraph.py`, `vertexfmt.py` | Format readers of the material system, copied from the Scene Editor by `tools/sync_look.py` |

## Loading: why `__init__.py` clears `sys.modules`

The core modules are imported as **top-level** modules (`import c2geom`, not `from .core import c2geom`).
`core/` is put at the front of `sys.path`. These modules use short unique names (`c2*`, `c3*`) so they cannot
clash with other add-ons.

Blender does not reload top-level modules when an add-on is updated or re-enabled. On re-enable it also only
reloads the package's `__init__`. Without help, an old `c2oct` would keep running next to a new `rig.py`. Lists
made by one copy of `c2oct` would then look like plain lists to the other copy, which shows up as errors such as
"plain list for 'HeaderStrings'". So `__init__.py`:

1. pops every name in `_CORE_MODULES` from `sys.modules`;
2. pops every `cars2_toolkit.*` submodule;
3. removes any other `core` folder from `sys.path` that holds a `c2geom.py` (an older install);
4. puts this version's `core/` first.

**When you add a core module, add its name to `_CORE_MODULES`.**

## Register order

```python
_modules = (look, anim, character, cars3, cars3_export, template, rig, charmat, c2selink)
```

`look` comes first because other modules' properties and panels use the material data (`Material.c2`,
`Object.c2tex`). `look.register()` might not register its own copy: if the Scene Editor is enabled, the
toolkit uses the Scene Editor's copy (see [Scene Editor link](SceneEditorLink.md)). Unregister runs in reverse.

## State stored on Blender data

Everything the add-on needs to export again lives on the armature and materials, so it is saved in the
`.blend`. All JSON values are strings, because ID properties cannot hold arbitrary nesting reliably.

| Where | Key | Set by | Meaning |
|---|---|---|---|
| armature object | `cars2_scene` | `character.py` | absolute path of the source `.oct` |
| | `cars2_bent` | `character.py` | source `.bent` path (`''` if none) |
| | `cars2_bone_nodes` | `character.py` | JSON `{bone name: network node path}` |
| | `cars2_edits` | `rig.py` | JSON list of recorded edits (see [Rig](RigAndExport.md)) |
| | `cars2_mesh_nodes` | `geometry.py` | JSON `{material index: [scene-tree node indices]}` |
| | `cars2_slider_rest` | `c2_model.py` | slider rest values |
| | `cars2_eye_plan` | `charmat.py` | the rig plan's eye links and look interpolator (JSON) |
| | `c2_assets`, `c2_source`, `c2_stem`, `c2_effects_dir` | `charmat.py` | game assets folder, source path, stem, the character package's effects folder |
| | `cars2_se_light`, `cars2_se_refl` | `c2selink.py` | which probes light the character (panel text) |
| | `cars2tk_template_base` | `template.py` | the Cars 2 character a custom rig was last exported onto |
| | `cars2tk_cars3_source`, `cars2tk_cars3_prop`, `cars2tk_chains`, `cars2tk_c3_template` | Cars 3 modules | Cars 3 source, chains, last Cars 2 base |
| armature object | `Object.c2tex` (collection) | `look/props.py` | the character's TexturePool (C2Texture items) |
| | `cars2_emissions`, `cars2_emission_strength` | `charmat.py` | the Emissions preview toggle and strength |
| material | `cars2_material` | `geometry.py` | MaterialPool index |
| | `Material.c2` (pointer) | `look/props.py` | the MaterialPool entry: effect, parameters, texture slots, `owner` |
| bone | `c2_template_anim`, `c3_new_bone`, `c3_node` | template / Cars 3 | per-bone flags |
| mesh | `cars2_pos`, `cars2_normal`, `cars2_tangent`, `cars2_binormal`, `cars2_uv` attributes | `geometry.py` | original vertex data |

`character.is_character(obj)` means "an armature with `cars2_scene`". `character.active_character(context)`
returns the active object if it is one, else the active object's parent if that is one, else the only character
in the scene (or `None` when there are several).

## Conventions

- **Game space and Blender space.** Bones and meshes are built in game space (Y up, the game's units). The
  armature *object* is rotated by `GLOBAL_ROTATE_X_DEGREES` (90°) to stand up in Blender. All maths that goes
  back to the game therefore works in armature space and never has to undo a coordinate change.
- **Matrices.** The game stores matrices row-vector (DirectX layout: translation in elements 12–14). Rotations
  are stored as "rotors" `[x, y, z, w]`.
- **Undo.** Imports switch `use_global_undo` off while building (much faster), then switch it back on. Handlers
  that keep caches register for both `undo_post` and `redo_post`, because undo recreates data-blocks.
- **Logging.** `c2_model.log()` buffers messages and `flush_log()` prints them, so one import prints one block.
- **Version.** `bl_info['version']` → `VERSION`, which the export report prints.
