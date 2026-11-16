# Cars 2 Toolkit: developer documentation

These pages explain how the add-on is built and how it maps onto the game. They are for anyone who wants to change
the code or understand what the game does with a character. The user guide is the add-on's own
[README](../README.md).

The game is **Cars 2: Arcade** (the "Win32" build derived from the Xbox 360 version), whose engine is Avalanche
Software's Octane. Every format fact here was checked against the Arcade data and, where noted, against
`sdaemon.exe` in Ghidra.

| Page | What it covers |
|---|---|
| [Architecture](Architecture.md) | Add-on layout, load order, the `core/` path trick, data stored on Blender objects, conventions |
| [File formats](FileFormats.md) | `.oct` / `.bent` (Tupperware), the Scene SubNetwork, `.vbuf` / `.ibuf`, clip blocks, constraints, `.tfx`, DDS |
| [Character import](Import.md) | `character.py` + `c2_model.py`: skin bones, control rig, eyes, meshes, textures, materials |
| [SubNetworkPool: the rig network](SubNetworkPool.md) | What the network is, every field, how a frame flows through it, and why the toolkit can safely add to it |
| [Rig editing and export](RigAndExport.md) | Recorded edits, `c2rigedit` / `c2scene` / `c2validate`, the export operator, adding channels |
| [Geometry export](Geometry.md) | `geometry.py` + `c2geom`: meshes back to the game's buffers, byte-exact untouched vertices |
| [Animation clips](Animation.md) | `anim.py`: the ClipDataBlock codec, bindings, channel maps, sliders, import and export |
| [Materials and Game Look](Materials.md) | `charmat.py` + the shared material system (`look/`), eyes, emissions, export of materials |
| [Scene Editor link](SceneEditorLink.md) | One shared copy of the material system (`looklink`), map lighting (`c2selink.py`) |
| [Cars 3 pipeline](Cars3.md) | Cars 3 character / prop import, Cars 3 → Cars 2 export, Cars 3 clips, custom rigs (`template.py`) |
| [Testing](Testing.md) | The test scripts, how to run them, what each one proves |

## The one-paragraph picture

A Cars 2 character is an `.oct` file (a Tupperware tree: textures, materials, mesh nodes, the rig network), a
`.bent` file (its animation interfaces and clip sets) and a `_0.vbuf` / `_0.ibuf` pair (vertex and index data).
The toolkit imports all of this into a Blender armature plus meshes. **It never rewrites the game files from
Blender data alone.** Every change the user makes is recorded as plain data (rig edits as JSON on the armature,
mesh data, material settings on `Material.c2`). On export, the *original* files are loaded again, the recorded
changes are applied, everything the engine cross-references by number is regenerated, and the result is
validated. An untouched character exports byte-identical, and the tests depend on that.

## Rules the code follows

- **Byte-exact round trips.** Every codec in `core/` decodes and re-encodes the shipped data byte for byte. New
  code has to keep this: a change that alters an untouched export is a bug.
- **No game patches.** Everything is done through data the stock game already reads.
- **Vanilla by default.** New data copies the conventions the shipped files use. `c2validate` encodes those
  conventions.
- **`core/` has no Blender dependency.** It is pure Python (NumPy where needed) and can be scripted or tested
  outside Blender.
- **Preview-only values are never exported.** Live eye values, the Emissions toggle and probe cubes only affect
  drawing (see [Materials](Materials.md)).
- **Speed matters on low-end machines.** Avoid per-frame scans of `bpy.data.objects` or `scene.objects.get`
  (an O(N) lookup); cache and invalidate instead.
