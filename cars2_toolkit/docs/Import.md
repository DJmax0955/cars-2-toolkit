# Character import

**File > Import > Cars 2 Character (.oct)** runs `CARS2TK_OT_import_character`, which calls
`character.import_character(context, filepath, import_textures, import_markers)`. The file browser also accepts `*.oct.json`
(an `offsetting` JSON dump); the menu just doesn't say so.

## Steps (`character.import_character`)

1. **Read the tree.** `octio.load(filepath)` loads the `.oct` with the lossless codec.
2. **Find the buffers.** `c2geom.buffer_files()` returns the `.vbuf` / `.ibuf` the `.oct` names (a port may name
   another car's). `c2geom.buffer_sets()` handles characters split over several buffer files. Import stops if
   any buffer is missing.
3. **Read the `.bent` first.** `rigedit.find_bent()` finds it, then `rigedit.channel_labels()` adds every channel
   it names to `anim.BUILTIN_CHANNEL_MAPS`. Custom controls then get named bones.
4. **Wrap the tree.** `c2_model.OctData(tree_to_json(tree))` gives the importer its JSON-shaped view
   (`c2_model` began as an importer for `offsetting` JSON and still reads that shape).
5. **Textures.** `extract_textures()` writes every embedded DDS to `<name>.oct.textures/...` (laid out like
   `offsetting`'s extract, mirroring `SourceFilePath`). If that folder is not writable it uses a temp folder.
   Files already there with the right size are kept.
6. **Build**, with global undo off:
   - `c2_model.load_materials()`: one Blender material per MaterialPool entry;
   - `c2_model.build_armature()`: skin bones and the control rig (below);
   - `c2_model.build_merged_mesh_objects()`: one mesh object per material (below);
   - `charmat.setup_eyes()` and `charmat.setup()`: the game materials (see [Materials](Materials.md));
   - the armature object is rotated 90° about X, and the meshes are parented to it;
   - everything goes into a collection named after the character (`MODEL_COLLECTION`).
   - with **Import FX Markers** off, `drop_fx_markers()` then deletes the `FX_Marker_*` bones and the empty
     `FX Markers` collection (the plain import, `template.import_plain(..., import_markers=False)`, simply
     doesn't add them). The .oct's marker nodes are untouched;
7. **Bone → node map.** From the rig plan's `node_of_bone`, plus a fallback that looks up
   `<root>/<bone>/Transform` paths. Stored as `cars2_bone_nodes` so rig edits can be written in the game's
   terms (node paths).
8. **Geometry bookkeeping.** `geometry.tag_import()` records which MaterialPool index each Blender material is
   and which scene-tree mesh nodes used it. `geometry.store_originals()` keeps each vertex's exact game data
   (see [Geometry](Geometry.md)).
9. **Per-character state.** Sets `cars2_scene`, `cars2_bent`, `cars2_bone_nodes` and `cars2_edits = '[]'`.
10. **Channel maps.** `refresh_channel_maps()` rebuilds the character with its edits and adds the channels of
    the edited `.bent` (and borrowed slots) to the animation maps.
11. **Scene Editor map.** `c2selink.sync()` lights the character from the map's probes, if a map is loaded.

Props with no `Bone` nodes import as meshes only (`build_armature` returns `None`).

## The armature: two layers (`c2_model.build_armature`, `plan_control_rig`)

This mirrors how the game itself is rigged.

**Skin bones** are the scene tree's `Bone` nodes the mesh is weighted to (`Main`, `Body`, `Jaw`,
`TireFLeft_centerJoint`, ...). Their rest comes from `BoneToModelMatrix`. The skin hierarchy is kept: it mirrors
the game's joint tree.

**Control bones** form the animation rig from the Scene SubNetwork (`Main_Ctrl`, `Body2_Ctrl`,
`FLeft_Center_Ctrl`, `Jaw_Proxy`, `Jaw_Ctrl`, ...). Clips never animate skin bones directly. They animate
controls, and each skin bone copies its control's world transform through an **UberConstraint**.
`plan_control_rig()` works out:

- `control_bones`: each control's world rest, computed from the data-node chain through
  `BasisDataNodeRef`;
- `node_of_bone`: the network node behind each bone;
- `constraints`: UberConstraint ops turned into Blender `COPY_LOCATION` / `COPY_ROTATION` / `COPY_SCALE` /
  `COPY_TRANSFORMS` / `DAMPED_TRACK` constraints (`_add_uber_constraints`, pose space);
- `spin_drivers`: wheel spin. Clips drive `Main_Ctrl/RotateFront` and `RotateRear` (degrees), which the rig
  turns into rotation of the `*_SpinJoint` bones. They are rebuilt as drivers `v * slope + intercept` on a
  custom property, using simple expressions so "Auto Run Python Scripts" is not needed. `WHEEL_SPIN_DIRECTION`
  flips them;
- `relationship`: the RelationshipDataBlock (mouth sliders; see [Animation](Animation.md));
- `offset`: the rig's offset from the skeleton, stored as `cars2_rig_offset`;
- the eye network (`eye_links`, `eye_rbf`), below.

Without the control layer, channels such as `Body2`, `Body3`, `FrontTires` and `FLeft_Center` would have no bone
to land on, and the lower lip wouldn't follow the jaw (in the game its control sits under `Jaw_Ctrl`).

`TIDY_RIG` leaves out pass-through group nodes that nothing moves (no clip channel, constraint, slider or
driver). Their children move up to the nearest kept bone with the same rest. On McQueen this drops 37 bones and
cuts the deepest nesting from 30 to 18.

Bones go into bone collections (Skin, Controls, FX Markers, and a hidden "Rig Internals" collection). Each
bone's imported rest is stored so Edit Mode moves can be detected on export.

## Meshes (`c2_model.build_merged_mesh_objects`)

`extract_node_geometry()` decodes each `SubGeometryLit`'s streams (`decode_stream_numpy`, vectorised) and
indices. It maps the `SkinRemap` palette to bone names for the vertex groups and drops unused vertices. Mesh
nodes that share a MaterialReference are merged into **one object per material**. The vertex order is each mesh
node's used vertices in index order, concatenated in scene-tree order. `c2geom.extract_meshes` produces the same
order, and `store_originals` relies on that. The first UV layer is named `Uv1`. Source normals are applied as
custom normals (`USE_SOURCE_NORMALS`). D3D's v (down) is flipped to Blender's v (up).

## Eyes

Cars 2 eyes are neither geometry nor shape keys. `bodyMesh_eyeshader` is a flat strip over the windshield. Each
frame the game draws the eye image (sclera, two eyeballs, outline band, lids) into a small texture,
`renderedEyeTexture`, from about 45 material parameters. The `car_eyes_*` / `car_double_eyes_*` effect then
samples that texture like any other map. The rig drives the parameters:

- Connection processors copy the clip's `Eyes_Ctrl/<lid control>` floats straight into `eyeLidTop_*` /
  `eyeLidBottom_*`. These are `eye_links`: (slider label, parameter, rest).
- A radial-basis interpolator turns the Eyes control's position (a 3×3 grid of keys from -1 to 1) into
  `eyeBallOffset_u/_v` and `eyeLidOffset_u`. This is `eye_rbf`.

`c2_model.build_eye_graph()` transcribes the game's eye renderer (`sdaemon.exe` 0x4B3EF0, helpers 0x4B7840 and
0x4B7570) as an expression graph (`Ex`). `build_look_graph()` builds the RBF, and `eval_numpy()` evaluates
either. In the add-on:

- `charmat.setup_eyes()` stores the plan as `cars2_eye_plan` and starts the eye sliders (custom properties on the
  armature) at their rest values.
- `charmat.eye_values(arm)` evaluates the sliders and RBF for the current pose. `charmat.update_eyes()` puts the
  result in `gamerender.EYE_VALUES[material]`. It runs from the depsgraph and frame handlers, and only a change
  triggers a redraw.
- `look/gameeyes.py` draws `renderedEyeTexture` from those values, one full-target pass per pixel.

The values are **never written to the material**. The game evaluates the eye rig every frame and never stores the
result, so an untouched character still exports byte-identical. (Writing them to the material once broke that.)

`c2_model.setup_eye_materials()` (the node-based eye shader, `BUILD_EYE_SHADER`) is only used by the standalone
`c2_model.main()` script path. The add-on's import uses the Game Look eyes above.

## Configuration flags (`c2_model.py`, top of file)

| Flag | Default | Effect |
|---|---|---|
| `USE_SOURCE_NORMALS` | True | apply the file's normals as custom normals |
| `GLOBAL_ROTATE_X_DEGREES` | 90 | stand the model up in Blender |
| `BUILD_CONTROL_RIG` | True | build the control layer |
| `TIDY_RIG` | True | drop pass-through rig nodes |
| `MODEL_COLLECTION` | True | one collection per character |
| `WHEEL_SPIN_DIRECTION` | 1.0 | flip wheel spin drivers |
| `BUILD_EYE_SHADER` | True | node eye shader (standalone script path only) |
