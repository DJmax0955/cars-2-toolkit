# Cars 2 Toolkit (Blender add-on)

Import Cars 2 Arcade characters, edit their game rig, put custom models on a Cars 2 rig, and import/export animation clips, all inside Blender. Working with the game's `.oct` files needs no other tools; offsetting is only used for its `.oct.json` files and for packing game zips (see *Preferences*). Works on Blender 4.1 and later (tested on 4.1, 5.0, 5.2 and 5.3).

> **Disclaimer.** This add-on contains no game files: no models, textures, sounds or game code. You need your own
> copy of the game and work on your own extracted files. Cars 2, Cars 3 and their game files belong to their
> respective owners; this project is not affiliated with Disney, Pixar or Avalanche Software. Back up any game
> file before you replace it.

## Install

1. Disable the separate **Cars 2 Animation (.oct)** add-on if you have it. This add-on includes it, and two copies would clash.
2. Go to **Edit > Preferences > Add-ons > Install from Disk** and pick `cars2_toolkit.zip`, then enable **Cars 2 Toolkit**.

### Preferences

- **offsetting.exe** (optional): only needed to export onto an offsetting `.oct.json` base, and for **Pack as game .zip**. Get it from [Team Offsetting](https://github.com/offsetting/offsetting). Left empty, it is looked for in the game's `assets` folder.

## Import a character

Use **File > Import > Cars 2 Character (.oct)** and pick the character's scene file, e.g. `mcqueen_x/characters/mcqueen/mcqueen.oct`. Two ways, picked with **Import Control Rig**:

- **Off (default): the plain hierarchy.** Only the game bones (`Main` > `Body` > `FrontEnd` > `Jaw` ..., the wheel hubs and spin joints) as one simple hierarchy, at the game's places (bone head = `PivotPoint`). You also get the meshes skinned to them, the materials, textures and eye shader. This is the rig to model and weight on; export it with **Export as Cars 2 Character** (see *Custom models*). The FX markers come too, in their own group: `FX_Marker_<Name>` bones under an `FX_Markers` group bone (under `Body`), and in the `FX_Markers` bone collection. Move them and the export moves the markers. The group bone itself is ignored on export. Works on the `.oct` or offsetting's `.oct.json`.
- **Import FX Markers** (on by default): the FX marker bones (`FX_Marker_<Name>`: weapons, exhausts, headlights, batteries...) in both imports. Off: no marker bones. The .oct keeps its markers either way - Export Character writes the rig as it is, and a custom-rig export keeps the base's markers where they are.
- **On: the SubNetwork too** - the game rig from the `SubNetworkPool`: control bones, constraints, wheel spin, FX markers. Use it to animate (clips) and to edit the rig itself (*Cars 2 Rig* panel, **Export Character**). Needs the game `.oct`. Everything below in this section is about this import.

A model can name several buffer files (mcqueenfabulous: `_0` and `_1`; each stream picks its own by
VertexBufferReference / IndexBufferReference) - all are read; an export
with meshes writes them all into one `_0` pair. The vertex/index buffers are the files the .oct names in its buffer pools (a port built from another car may name that car's, e.g. boost -> `kabuto_0.vbuf`), with `<name>_0.vbuf/.ibuf` as the fallback; exports rename them to the exported file's own name. Mesh nodes that point at streams or scene components the file doesn't have - how hand-made ports switch a mesh off - are skipped with a note in the console.

Clips also play on the simple rigs of background characters and props (`materbg`, `policecar`, `generic01`..., controls `ControlMain_Ctrl`, `ControlBody_Ctrl`, `Eyes_Ctrl`): any `_Ctrl` control bone counts as a control rig (not only `Main_Ctrl` / `Body_Ctrl` / `FrontEnd_Ctrl`).

**Remove Clip** (the bin icon next to the clip's name in the *Cars 2 Animation* panel) deletes the clip from the .blend and puts the rig back in its rest pose: every bone at rest (user-added bones too), sliders at their no-clip values (eyelids open). The bin removes only that clip, without asking; to remove *all imported clips* at once, run Remove Clip from F3 search (it asks all / this clip). A clip another object still plays is kept. A cutscene actor is taken out of its placement and the empties left empty are deleted. Undo (Ctrl+Z) brings it all back.

Clips need the SubNetwork import: clips drive the controls, not the game bones. Import Clip on an armature without controls stops and says so.

- **Geometry:** `mcqueen_0.vbuf` and `mcqueen_0.ibuf` are found automatically, since they sit next to the `.oct`.
- **Animation data:** the `.bent` (the file that defines the animation channels) is found automatically two folders up.
- **Textures:** the textures stored inside the `.oct` are extracted to a `<name>.oct.textures` folder next to it, laid out like offsetting's extract (`characters/<car>/maps/<texture>.dds`), and loaded onto the materials. Files already in that folder are reused, not overwritten.

You get the armature, with control bones and constraints, plus the meshes and the eye shader (single and double eyes). The eyes open until a clip keys the eyelids. Many cars store a rest pose for their eyelid sliders (Mater, Finn, Luigi, Flo, Francesco...); the eyes show it on import (Remove Clip goes back to exactly that look). Cars whose eyelid sliders all rest at 0 (McQueen and his variants, Cruz, Sally, Holly, King...) would have both lids shut, so they get a neutral open pose instead: top lid flat at 45, bottom lid flat at -95. The values are `EYELID_DEFAULT` in `anim.py`. A clip that keys any eyelid leaves the lids it doesn't key at 0, as in the game. This changes the view only: Export Clip writes keyed sliders only. Textured eyelids (an eye material with a `diffuseMapA1`: Mater, King, Holley, Carla ...) are mapped as the game maps them (taken from `sdaemon.exe`). The texture's v is measured from each lid's own edge, so the lash line, with the flicks the female characters have at the corners, follows the lid as it moves.

The hierarchy is tidied:

- Rig bones that never move on their own (the game's pass-through group nodes) are left out, and their children move up to the nearest kept bone with the same rest. Motion is unchanged; McQueen goes from 155 to 119 bones.
- Bones are sorted into **Skin**, **Controls**, **FX Markers** and **Rig Internals** (hidden) bone collections.
- Each imported character gets its own collection.

Set `TIDY_RIG` / `MODEL_COLLECTION` in `c2_model.py` to `False` to turn these off. The **Cars 2 Character** tab in the 3D view sidebar holds the panels: *Cars 2 Animation* (clips, with *Sliders*), *Cars 2 Rig* (rig edits), *Custom Model*, *Cars 3 to Cars 2*, *Look* and *Material*. Material properties also get *Cars 2 Material* and *Eye Lid Shadow*.

## Edit the rig

Work the normal Blender way, then press the matching button in **Cars 2 Rig**. Each button records an *edit*, and the list of edits is shown in the panel; click the X to remove one. Game files are only written when you press **Export Character**.

| You do | Then press | What the game gets |
|---|---|---|
| Add a bone in Edit Mode and parent it to an existing rig bone | **Add Selected Bones to Rig** | A new rig node at exactly that place, following its parent |
| Make a rig bone active | **Animate in Clips** (tick the clip sets) | A new animation channel, so clips in those sets can key it |
| Add *Copy Location / Rotation / Scale / Transforms* constraints that target rig bones | **Convert Constraints** | A game constraint (the bone follows its targets) |
| Move or rotate a rig bone (an FX marker, `Body_Final_Proxy`, a `_Ctrl`) in **Edit Mode** or **Pose Mode**, or scale it in **Pose Mode** | nothing - **Validate** and **Export** record it | The node's new position / rotation / scale |

**Validate** checks the result against the rules every shipping Cars 2 rig follows. Details go to the `Cars2RigReport` text in the Text Editor.

**Export output: every character export writes the same thing** - one folder (default `export` next to the character you picked or imported, or the folder you choose) holding only `<car>.oct`, `<car>_0.vbuf` and `<car>_0.ibuf`. No `characters/...` subfolders, no `effects/`. Onto a `.oct.json` the result is a binary `.oct` too: the textures of its `<name>.oct.textures` folder are embedded and offsetting.exe encodes it (path from the add-on preferences, see *Preferences*). The `<car>.bent` is added to the same folder only when its animation channels changed (new animated bones / channel edits).

**Export Character** writes `<car>.oct` + `<car>_0.vbuf` / `<car>_0.ibuf` (with *Include meshes* off the character's own buffers are copied), and `<car>.bent` only if it changed. Two more options in the File > Export dialog: **Pack as game .zip** (packs the result into the character's game zip with offsetting.exe) and **Keep JSON + Textures**.

Copy **all** of them over the originals in the character package. With meshes included, the `.oct` describes the new buffers, so it must never be paired with the old `.vbuf`/`.ibuf`.

### Raising the body

Move **`Body_Ctrl`** up in Pose Mode (or Edit Mode) and export. It's animated by clips, so the move is written into the first node below it that clips don't animate (`Body_Final_ProxyGrp`), which raises the whole body the same way; the wheels stay put. With a clip loaded, Blender drops a manual pose on an animated bone when the frame changes, so export before scrubbing. The export pop-up and the `Cars2RigReport` text list exactly what changed.

### Moving FX markers and other rig bones

- **Edit Mode or Pose Mode.** Pose Mode shows the mesh following live. A bone's own pose (its location/rotation channels) is what's exported, not motion it inherits from an animation on its parents. Bones with constraints (the game drives them) and channels keyed by the active animation are skipped. Once recorded, a Pose Mode move is kept even if a clip import resets the pose; remove it with the X in the edit list.
- **Raising the body:** move `Body_Final_Proxy` (the `Body` bone follows it). Its node has no position field, so the move is written into its parent `Body_Final_ProxyGrp`, which has one; that's only done when it's equivalent (the parent isn't animated or driven, and has nothing else under it).
- **Bone names:** internal rig bones are named after their node in the rig network (`Body_Final_Proxy`, `Body_Final_ProxyGrp` ...), not `rig_<number>`.

- **Move / rotate:** in Edit Mode or Pose Mode. The node's `Translation` and rotation are written exactly (an export → re-import round trip reproduces the marker to within 0.000001).
- **Rotation fields:** a node with `Orientation#Rotor` gets a new rotor; one with `Orientation#OrientProxy` gets the change in its `LocalToLocalProxy` part; a marker with no rotation in the file gets an `Orientation#Rotor` (the same markers carry one on other characters).
- **Scale:** in Pose Mode (bones have no scale in Edit Mode). It multiplies the node's `Scale`, along the bone's own axes. Only markers that already have a `Scale` in the file are scaled: tested in-game, a `Scale` added to one that had none is ignored. Scale keyed by the active animation is skipped.
- `ADD_MISSING_ROTATION` / `ADD_MISSING_SCALE` at the top of `rig.py` change the last two rules.

## Materials: game materials, Game Look, editing

**A character's materials work exactly like a Cars 2 Scene Editor world's.** The toolkit carries a copy of
the Scene Editor's material system (`look/`). With the Scene Editor enabled, the toolkit uses the Scene Editor's
own copy instead, so both add-ons edit the same material data. Either way the add-on works on its own.

What an imported character gets:

* **Game materials.** Each material holds its MaterialPool entry: its effect (shader), its parameters, and its
  texture slots. Every parameter and slot the effect has is listed, with the effect's default for those the
  entry doesn't set. The character's own textures (its TexturePool) are listed on the armature. Slot indices
  point into that list, not into a world's.
* **Game Look:** the **Cars 2 Game** render engine in **Rendered** view draws every material with its effect's
  own shader (sun, light rig ambient, specular, reflections, glow, cut-outs, blending), the eye strip with the
  game's `renderedEyeTexture`, then the game's post-processing. Import switches the scene to it.
  * In a Scene Editor world, the world lights the character: the sun, fog and exposure, and the light probe
    where the car stands for its ambient light and its reflection cube. That's what the game does at run time.
  * With no world loaded, a neutral daylight stands in for a map: Tokyo B's sun, exposure and light-rig
    ambient, made colourless, no fog. The reflection is the character's own `envMapInstance` cube: every
    character carries `errorsky` there, the game's stand-in until a map's probe replaces it. So fresnel,
    reflectivity maps and the scroll map show and can be edited without a world. A material with nothing in
    that slot reflects a neutral cube as bright as a real probe's.
  * The engine's own environment scroll map (`misc/environmentScroll.dds` from the startup packages, bound to
    every material that samples `environmentScrollMap`) is used like in game.
  * **Film > Transparent** (Render Properties) renders the character on a transparent background: only what is
    drawn covers it (F12 renders and the viewport). On Blender 4.1 the overlays keep their depth too.
  * The renderer is shared with the Scene Editor, so it gets the same speed-ups: after undo / redo only meshes
    that really changed are read again, showing or hiding objects keeps the rest's draws, each material's
    values are worked out once for every part using it, and in the viewport parts smaller than a pixel are
    skipped (F12 draws everything).
* **Blender materials:** Material Preview and Eevee use simplified Blender materials. **Cars 2 Character > Look >
  Materials** switches them, as the Scene Editor's Viewport setting does (it is the same setting):
  * **Game Look (unlit):** texture as emission.
  * **Baked x Blender Lights:** texture lit by your Blender lights. Characters have no baked lighting.
  * **Textures Only.**
* **Eyes:** the rig drives the eye material (lids, look offset) live, from the eye sliders and the look
  interpolator over the Eyes control, as the game does every frame. The values only feed the eye texture. The
  material keeps its stored values, so an export writes them unchanged.

**Emissions** (Look panel, per character): the game switches glow maps on at run time (brake lights when
braking ...). 85 of the 86 character materials with an `emissiveMapA1` store `emissiveMultiplier` 0, so they show
nothing in a preview. **Emissions** shows them lit, at **Strength** (1 = an `emissiveMultiplier` of 1). This is
for the preview only: the materials and exports keep their values. It applies to Game Look and the Blender
materials, and is saved with the file. It follows material edits as you make them: a shader switch that adds a
glow map, or a texture picked for one, shows lit at once.

**Look panel** (Sidebar > Cars 2 Character > Look):
* **Rendered view:** Game Look (Cars 2 Game engine) or Blender (Eevee).
* **Materials:** the Blender materials' mode (above).
* The game render options: shadows, reflections, bloom, auto exposure.
* **Emissions** and its **Strength**, for the active character.
* The Game Assets Folder (the game's `assets`, holding `effects/effects`). An import finds it by itself: the scene's
  setting, else any folder above the character's file, else the last one that worked (remembered across
  sessions), else the standard `Documents/My Games/Cars 2 Arcade Essentials/assets`. The character keeps it, so
  a scene with no setting still draws it in Game Look.
* **Rebuild Character Materials:** builds the materials again from the source `.oct`, dropping material edits.

**Editing** (Material properties > **Cars 2 Material**, or Sidebar > Cars 2 Character > Material): the Scene Editor's
Material panel.
* **Change** (the effect): a search of the game's skinned (`_skin`) effects, with what each one does.
* **Texture slots:** thumbnails, **Pick** (every one of the character's textures; those of the kind the slot
  samples, 2D or cube map, come first, the others are marked), **Load Image** (a new texture of the character; a file named like one of its textures replaces that one in its own slot, and export keeps one TexturePool entry per name and drops textures nothing uses). A replaced texture shows at once in every material using it, with its size, format and mip count in the texture line; loading a file you edited and saved again picks up the new pixels without restarting Blender.
  A slot the effect needs that has no texture shows in red. Until it gets one, an empty glow map glows nothing
  and an empty cube slot reflects a neutral cube.
* **Parameters:** readable names, colour pickers, reset to the effect's default.
* **Blending / culling.**
* The character's texture list, with **Replace With** and the sRGB flag of each texture.

**Switching shaders: dropped and new texture slots.** Every shipped material lists exactly the texture slots and
parameters its effect uses: the 295 materials of the 58 characters, and 3,798 world materials. The exceptions are
two slots the game fills itself: `renderedEyeTexture` and `environmentScrollMap`.
* **Dropped:** slots and parameters the new effect doesn't use. A dialog lists them, with their textures, and
  asks before switching.
* **Kept:** they stay with the material (*Kept from earlier shaders*). Switching to an effect that uses them
  brings them back with the same textures and values.
* **New:** slots the new effect needs that nothing provides start empty.
* **Export is refused** while a slot the effect samples has no texture. The message names the materials and
  slots, and the console lists them all.

**Export Character** writes:
* each material's effect, its changed parameters (untouched ones keep their exact stored values: an unchanged
  character exports byte-identical), its texture slots, and leaves dropped slots and parameters out;
* replaced textures into their TexturePool entries, new ones as new entries (`characters\<car>\maps\<name>.png`).

No `.tfx` files are written: a newly picked effect must be one the game already ships (the effect list only offers
those).

## Meshes and new skin bones

With *Include meshes*, every mesh object under the character (parented to the armature, or deformed by it) is exported:

- **Edit freely.** Change existing meshes in any way, or add new mesh objects. Faces are split by material, and each material must be one of the character's own imported materials. Assign one to new faces; the export tells you if you didn't.
- **Weights.** Vertex groups named after bones are the weights, up to 4 per vertex, normalised the way the game stores them. Vertices with no weights ride on the body (you get a warning).
- **New bones carry geometry.** Add a bone, press **Add Selected Bones to Rig**, then weight vertices to it. It becomes a new skin bone with the game bone, its rig node and the binding all created. Make it animatable with **Animate in Clips**, or drive it with constraints/drivers.
- **Untouched vertices stay exact.** They export bit-for-bit as they came in: normals, tangents, UVs and weights. Moved or new vertices use Blender's normals and tangents. Tested on 8 characters, where an untouched import → export reproduced every triangle exactly.

Game limits the export enforces:

- at most 42 bones per mesh;
- at most 65,535 vertices per mesh (pools are split automatically);
- materials must come from the character.

### Animating new channels

After **Animate in Clips**, the clip exporter in *Cars 2 Animation* already knows the new channel. Key the bone (or the float property), export the clip, and point the character's `.bent` at it.

Clips that use a new channel must only be played by characters that have it. Don't put new channels into shared `characters/car/motions` clips.

## What isn't supported yet

- **New materials on Export Character.** New faces must use one of the character's existing materials (new textures are fine: *Load Image*). The custom-model and Cars 3 exports can add materials.
- **Mesh nodes under transformed scene nodes.** No shipping character has these; the export refuses them rather than guess.
- **Constraints with offsets, mix modes or partial influence**, aim constraints, and drivers with expressions. The buttons tell you exactly what they skipped and why.
- **Bone scale in rest poses.** The game rig ignores it, and you get a warning.

## How it works (for the curious)

Developer documentation (architecture, file formats, the rig network, clips, materials, tests) is in [docs/](docs/README.md).

`core/` is the tested toolkit (pure Python, no dependencies):

- It reads and writes `.oct`/`.bent` byte-exactly.
- It regenerates every number the game cross-references.
- It validates against the shipping rigs.

`c2_model.py` is the model importer; `anim.py` is the Cars 2 Animation add-on; `charmat.py` makes a character's
materials game materials and exports them; `look/` is the material system, copied from the Cars 2 Scene Editor
(its `tools/sync_look.py`; don't edit those files here). Recorded edits are plain data stored on the armature (`cars2_edits`), and `core/c2rigedit.py` applies them to the original files on export, so you can script it outside Blender too:

```python
import sys; sys.path.insert(0, 'cars2_toolkit/core')
import c2rigedit
written, log, errors, warnings = c2rigedit.export(scene_oct, bent, edits, out_dir)
```

## Importing a Cars 3 .oct (characters, weapons, props)

**File > Import > Cars 3 Character (.oct)**, listed right under the Cars 2 one. Pick any Cars 3 model; its `<name>_0.vbuf` and `<name>_0.ibuf` must be next to it. The importer looks at the file: skinned geometry means a character (below); anything else - weapons such as `loadout_roadmine`, pickups, props - is imported as its node tree.

### Weapons and props

- **One bone per scene node** (every Transform and Geometry node), with the node's name and the file's hierarchy, at the node's rest place. Names the file repeats get `.001`, `.002` ... (`pCube3`, `pCube3.001`). Each bone has a `c3_node` property: its index in the file's scene tree.
- **One mesh object per material**, named after the material (`Loadout_RoadMine_base_d`, `Loadout_RoadMine1`). Each part sits where the scene tree puts it and is bound wholly to its node's bone (a vertex group per node) - pose a bone and that part moves with everything under it (`MineSlider` moves the whole mine launcher). Each face's node is kept in the `c3_node` face attribute. Set `PROP_SPLIT_PARTS = True` at the top of the props section in `cars3.py` for one object per part instead.
- Same coordinates as a character (game space, armature rotated to stand up), but nothing is lifted: the origin stays where the file has it (for a loadout weapon, its mount point).
- Not included: textures, and the file's animation network.

### Characters

You get the same armature layout as a Cars 2 import:

- **Same bones, names and hierarchy.** Each Cars 3 rig node becomes the bone a Cars 2 import makes for that node (`FX_Marker_X` -> `FX_Marker_X_Ctrl`, `Body_VSM_Proxy` -> `Body_Baxle_Proxy`, face `X_CtrlGrp` -> `X_Proxy`, `Mouth_Ctrl` -> `FrontEnd_Ctrl`). Cars 3-only nodes and the pass-through groups a Cars 2 import leaves out are left out, their children moving up. A car without a `FrontEnd` skin bone gets a weightless one at its face root, so the face hangs where it does in Cars 2.
- **Same bone collections** (Skin, Controls, FX Markers, Rig Internals) and a collection named after the model.
- **Import options, the same as the Cars 2 import:** **Import Control Rig** (on: the controls, wheel spin, links to the skin, for animation; off: the skin bones only, as one plain hierarchy), **Extract Textures**, **Import FX Markers** (without the control rig they hang under an `FX_Markers` group bone below `Body`, named `FX_Marker_<Name>` - the Cars 2 plain layout; off: none) and the **LOD Models** section. Export Cars 3 as Cars 2 gives the same result either way: controls and markers you didn't import are read from the Cars 3 file.
- **Face controls too.** Cars 3 keeps some controls as data inside the skeleton's frames (McQueen's lip, cheek, teeth and tongue controls live in `FrontEnd` / `Tongue1`); they become the same `_Ctrl` bones as in Cars 2, under the same parents. Each is placed on its face bone (`LipUpperCenter_Ctrl` on `LipUpperCenter`), where every Cars 2 face control sits at rest - some Cars 3 files only keep the control's place as a computed value (Fritter's lip controls are stored as zeros).
- **Same coordinates:** game space with the armature rotated to stand up.
- **Meshes as the file stores them:** one object per primitive, named `<material>_LOD<n>` (McQueen has 22: body, tires, eyes, lights and glass in three LODs, plus two shadow casters). Each object carries `c3_primitive` (its index in the `.oct`) and `c3_lod` custom properties. Pick which sets to import in the dialog's **LOD Models** section (opens/closes; a plain list of checkboxes LOD 0 / LOD 1 / LOD 2, default LOD 0 only) - no deleting overlapping LOD meshes afterwards. Set `SPLIT_BY_PRIMITIVE = False` at the top of `cars3.py` to get the old one-object-per-material merge. The model is lifted so the car stands on the ground as a Cars 2 model does (Cars 3 files differ: Brick stands on the ground, McQueen is modelled around his axles).

**Textures (no other add-on needed):** the PS4 `.tbody` textures are converted to DDS with `ditex.py` (carried in `c3look/`) and put on the materials by the Cars 3 material decoder (in `c3look/`). They are taken from the `textures/` folder of the extracted character bundle (`<bundle>/textures/<hash>.tbody`, the `.oct` + `.mtb` in `<bundle>/characters/<name>/`), else from `bundles/actor-<name>/_root_.zip` of the extracted game folder. That folder (the one holding `assets/` and `bundles/`) is found above the `.oct`, so **open the character from inside your extracted Cars 3 game folder**. The game folder is needed either way: its shaders say which texture is the colour / normal / gloss map. The controls don't move the skin bones yet (Cars 3 links them through a different kind of rig network).

**Eyes:** the eye material shows the eyes the way Cars 3 draws them - the irises, the lids and the shadow under the top lid - in Material Preview / Eevee / Cycles (Game Look draws Cars 3 materials flat). The game draws a car's eyes into a texture each frame (`EyesComponent`), and that code is the Cars 2 eye code carried over, so the import uses the same eye graph as Cars 2 characters with each car's own Cars 3 values: eye size and spacing (`g_carEyes_eyeBalls_configuration`, McQueen 0.27 x 0.39, spacing 0.26), lid shapes, lid shadow colour/opacity/offset (McQueen black 0.75, 0.03 lower; Cruz 0.3, 0.1) and lid colour or lid texture (lashes: Cruz, Sally), read from the eye material in the `.mtb` by the shader's own parameter table. Both kinds: one windshield with a notch (McQueen, Cruz, Mater, Storm ...) and two windows (Mack, Sheriff, Doc Hudson, Fritter ...). With the control rig the eyes are live: the 30 eyelid sliders (`Eyes_Ctrl/TOP_L_Bow` ... on the armature, starting at the rig's own rest - McQueen's lids 67.6 / -72.2) drive the lids as the rig's FloatConnections do, and the new `Eyes_Ctrl` bone (Cars 3 keeps the look control as a value in `Eyes_Frame_Proxy`) drives the irises and the notch through the rig's own look interpolator - so Import Cars 3 Clip blinks, frowns and looks around. The values reach the material through `c3e_*` properties on the eye meshes (no drivers inside the material, playback stays fast). Sally's file has no eyelid connections (her lids stay as the material has them, as in the game). Eye materials such as Sheriff's get the right texture in every slot.

**Version 1.0 has no Cars 3 game shading.** The Cars 3 materials that shade like the game (the select stage's sun, light rigs, reflection cube and tone curve) are not part of this release. A Cars 3 import gets plain textured materials instead; the eye preview, the rig, clip conversion and Export Cars 3 as Cars 2 all work.

## Exporting a Cars 3 character as a Cars 2 character

**Export Cars 2 Character** (File > Export > Cars 2 Character, or **Export Cars 3 as Cars 2** in the **Cars 3 to Cars 2** panel of the Cars 2 Character tab) with the Cars 3 import selected: pick the Cars 2 character to build on. The same export handles imported Cars 2 characters and custom models.

1. Import the Cars 3 model and delete the meshes you don't want. Only what is in the scene is exported.
2. Run the export and **select the Cars 2 character** to build on in the file browser - normally the same car from Cars 2. Its `.vbuf`, `.ibuf` and `.bent` are found next to it / above it. The result goes to an `export` folder next to it (or the **Write to** folder in the options): `<car>.oct` + `<car>_0.vbuf` + `<car>_0.ibuf`. The base can be:
   - the game's **`.oct`** (e.g. `mcqueen_x/characters/mcqueen/mcqueen.oct`);
   - the **`.oct.json`** offsetting made of it (e.g. `mcqueen.oct.json` with its `mcqueen.oct.textures` folder): that file's materials, textures and rig exactly as they are - only the meshes replaced - encoded to the `.oct` with offsetting.exe. Use this to give the Cars 3 model Cars 2 materials and textures you've edited in the JSON.
3. Copy **all** the written files over the originals in the Cars 2 package (the `.oct` describes the new buffers, so it must never be paired with the old `.vbuf`/`.ibuf`).

The Cars 2 character keeps its rig, skeleton, materials and animation channels, so its clips keep working; the Cars 3 meshes replace its geometry.

- **Which meshes:** the full-detail (LOD 0) meshes; meshes you added yourself are always exported. Shadow casters are left out.
- **Materials** are matched by kind: body, tires, eyes, headlights, brake lights, glass (both Cars 3 glass materials become the Cars 2 `Glass`).
- **A Cars 3 material with no Cars 2 counterpart** (Fritter's `Cars3_Fritter_Hood`, say) is added as a **new material named after it**, a copy of the closest base material - names with "light" start as `Headlights` ("brake"/"tail": `BrakeLights`), "glass"/"window" as `Glass`, anything else as the body material - with a fresh Uuid, and its mesh gets **its own mesh node** (`bodyMesh_<material>`). So a Cars 3 model with more meshes than its base keeps them all. Edit the new materials (effect, textures, properties) in the `.oct.json` afterwards; the report lists each one and what it was copied from. New materials have no rig link (`Anchor|<material>`), so nothing in the rig drives them. Set `ADD_UNMATCHED_MATERIALS = False` in `core/c3toc2.py` to stop on them instead.
- **Move skeleton to Cars 3** (on by default): the Cars 2 character's bones, controls and FX markers are moved to where the Cars 3 model has them, and the meshes go in as they are. Every Cars 2 node with a same-named bone in the Cars 3 scene moves - game bones (`BoneToModel` / `BoneToStandard` / `PivotPoint` / `BoneEndPoint` / `LocalToParent`, each by the difference so its own convention holds), skeleton rig nodes, controls and proxies (position only - animations are stored relative to each control's rest rotation) and FX markers (position and rotation - a marker's rotation aims its effect), with the rest-table copies of clip-driven ones. A node without a position field is moved through its parent when that parent carries only it (`Body_Final_Proxy` through `Body_Final_ProxyGrp`), passing slider proxies that sit at zero on their group - so a face control (`Jaw_Ctrl`, animated from zero by the clips) is placed by moving its group (`Jaw_CtrlGrp`) and itself stays at zero, as the game expects. A clip channel with no rest-table entry never gets a position field: the game animates it from zero and would ignore one. **The rig's zero is at the front axle**, as on every Cars 2 character: hub joints and `FrontTires_Ctrl` / `BackTires_Ctrl` at rig height 0, the ground control one axle-height below (Sheriff 0.379, the six-wheeler 0.704) - the game puts that zero at the character's axle height above the road. A model on taller wheels (Mack's 0.573 on Sheriff) would keep its wheels above the zero and float by the difference, so after moving the skeleton the whole rig (everything under `Main` and under the control root) is shifted down by the front hubs' height; game-space bones and meshes don't change. This is for in-race packages only: the frontend (select screen) places characters another way and a shifted model sank into its floor, so a base inside a `frontend` folder keeps its own zero (the report says which applied). **FX markers the Cars 2 character doesn't have** (Fritter's second exhaust on Sheriff) are added the way Cars 2 builds markers: a Transform under `Body_Final_Proxy` at the Cars 3 place and rotation, connected to its slot in the clip template when Cars 2 has one - the `car_body` template's fixed marker slots (`Exhaust_Left1/2`, `Exhaust_Right1/2`, `Battery_*`, `Bumper`, `Headlight_*`, `MachineGun_*`, `SkateJack`, `SpareTire`, `WGPCam`) are how the game finds a marker. Cars 3-only markers (taillights, contrails, magnet tires ...) have no slot: they're added but nothing in Cars 2 uses them. Left where they are: nodes the rig computes every frame (slider-driven proxies, spin joints, constraint-driven skeleton nodes keep their computed place) and Cars 2 nodes the Cars 3 model has no bone for (e.g. Sheriff's battery/bumper markers - add a bone of that name, `FX_Marker_Bumper_Ctrl`, in the Cars 3 scene to place one). Off: the meshes are fitted onto the Cars 2 skeleton as it is (moved so the wheel hubs match).
- **Every mesh you keep is exported.** When the scene has several versions of a part, the full-detail one is used; a part that only exists at another LOD (Fritter's `Orange_Glass` and `TeamYellow_underbelly` are LOD 2 only) is exported from the LOD it has (noted in the report). Delete the meshes you don't want first.
- **Exporting onto an earlier export's output** works: a Cars 3 material whose exact name the base already has (`Cars3_Mack_Chrome`) reuses that material - and its mesh node, or a new node on it - instead of adding a duplicate.
- **Meshes that aren't part of the armature are reported**: a mesh that belongs to the character by name but was moved out of the armature (or lost its Armature modifier) gets a "NOT exported" line instead of being skipped silently.
- **One Cars 3 material per Cars 2 material:** when a second, different Cars 3 material matches a Cars 2 material that is already taken (Fritter's `Orange_Glass` and `Glass` both match Sheriff's `Glass`), it becomes its own copy of that material with its own mesh node, so each keeps its own texture.
- **Tangents on every mesh** (always - Export Cars 3 as Cars 2 and the Cars 2 Export Character): every exported mesh gets the Tangent + Normal + Binormal vertex stream (24 bytes), copied from a base mesh that has it (the body / tires), with tangents and binormals computed per mesh - not only the meshes whose Cars 2 material had it (glass, eyes and lights are Normal-only on Sheriff). Node settings (RenderCaps ...) are identical for both layouts in shipping files; the stream declares the format.
- **The base's vertex-stream layout is kept.** Shipping files come two ways: McQueen and Mater share one vertex-stream pair per vertex format between their mesh nodes; Sheriff and Victor Hugo give every mesh node its own pair (and its own index stream), in node order. The export follows whichever the Cars 2 base uses - for Sheriff: node #n gets vertex streams 2n, 2n+1 and index stream n. Shared pools stay under 65,535 vertices (16-bit indices).
- **Mesh nodes are named after your model**: `bodyMesh_` + the Cars 3 material the node holds (`bodyMesh_Cars3_Fritter_Tires1`), never the base's (`bodyMesh_Sheriff_Tires`). Mesh node names are referenced nowhere else in a Cars 2 file. Material names stay as the base has them: `Headlights`, `BrakeLights` and `eyeshader` are looked up by name (the rig's glow and eye-shader links), so they must not change.
- **Wheel rig built exactly like vanilla Cars 2**: the export builds each wheel's rig from the hubs of your Blender model (TireX_centerJoint in Edit Mode), the way vanilla McQueen has it (H = hub height): Main_CtrlGrp (0, -H, 0) puts the ground at y 0; FrontTires_Ctrl / BackTires_Ctrl (0, H, ...) sit at their hubs' height; X_bttm_Ctrl_Grp (x, -H, z) goes straight down to the ground under the hub; X_Center_Ctrl (0, H, 0) goes straight back up to the hub; TireX_centerJoint sits on the hub and the SpinJoint rides on it. That is why a data node shows the hub's height negated: the step down from the axle to the ground. The body and everything else on the ground chain keep their places. Run on vanilla McQueen at his own hubs it changes nothing. So a base's old misplacement doesn't stay in, and the tires don't hop when driving.
- **Tire joints bound at their hubs**: when the export moves the Cars 2 skeleton to the model's places, every moved bone gets its bind position (BoneToModel / BoneToStandard) at its new PivotPoint, as on every shipping character (pivot = bind for all bones). A base with the bind off the pivot (an earlier edit: mcqueendtw's SpinJoints were bound 1.7 cm above their hubs) skinned the tires off the point the rig spins them around, so they hopped. A bone that sits exactly on its parent in the base (every TireX_SpinJoint on its centerJoint) stays there.
- **Meshes pair with Cars 2 materials by name too**: a Cars 3 mesh goes onto the Cars 2 material whose name is in its material name or its object name, e.g. object `Cars3_McQueen_Eyeshader` -> `eyeshader`, `Cars3_McQueen_Body2` -> `McQueen_Body2` (the longest matching name wins). Every name match is listed in the export notes.
- **Shading and modifiers are exported as you see them**: the meshes go out with every enabled modifier applied except Armature (the game skins the mesh itself). So **Shade Flat**, **Shade Auto Smooth** / Smooth by Angle, Weighted Normal, Mirror and the like export the way the viewport shows them, with no need to apply them first. A modifier that drops the bone weights is reported.
- **Weights** carry over by bone name. McQueen's 35 Cars 3 skin bones all exist on the Cars 2 skeleton.
- **Adding a bone yourself:** select it (Edit or Pose Mode) and press **Add Selected Bones to Rig** in the Cars 3 to Cars 2 panel. It's checked straight away - it must be a skin bone (one meshes deform with, not a control or proxy) parented to a skin bone such as `Body`, `Main` or a wheel hub - and listed under **New bones**, with an X to take it off. Listed bones are added on export even before any vertices are weighted to them; one the Cars 2 base already has is just noted.
- **Things that swing (dynamic chains)**, like Fritter's license plates: the Cars 3 model's own chains are found on import, listed under **Dynamic chains** and exported as Cars 2 chains (the way Mater's tow hook swings). In Blender they swing when you import a clip. Technical details: [docs/Cars3.md](docs/Cars3.md).
- **Animated Cars 3 parts** (Fritter's blade): a Cars 3 control that clips animate and that moves a bone the Cars 2 character doesn't have (`BladeBase_Ctrl` -> `BladeBase`, `BladeExtension_Ctrl`, `Blade_Ctrl`) is built the way Cars 2 builds its face: the bone stays in the skeleton (its rig node under its skeleton parent - `BladeBase` under `FrontEnd`), a separate `X_Ctrl_Grp` -> control chain (at zero, a clip channel) follows what moves that parent (`FrontEnd_Ctrl`; a part under another part follows that part's control), and an orient + point constraint makes the bone copy its control (`Jaw` <- `Jaw_Ctrl`). The game places a bone from its skeleton parent and its own rig node, so a bone hung under a clip-driven control never moves (tested in-game). The game only animates the slots its own clip templates define (a slot appended past them is accepted but never animated), so each control **borrows a slot the template already has and the character leaves empty** - one the character's `.bent` has a channel for - and takes that slot's name (the `.bent` names the node a slot drives). Mater's tow-crane slots are tried first: Fritter's blade on Sheriff uses `TowCraneBase_Ctrl` (slot 109), `TowCrane_Ctrl` (108) and `TCable_Pulley_Ctrl` (102). The `.bent` is not changed (use the original). The free slots come from the character's `.bent` when it is next to / above the character, otherwise from the template catalog (the same channels - the `.bent` needs no change either way); the export remembers those slots on the Cars 3 model in Blender (no extra file), and **Import Cars 3 Clip** / **Convert Cars 3 Clips** send the parts' animation to them automatically instead of dropping it.
- **Bones the Cars 2 character doesn't have** (Mack's middle-axle wheels, say) are added, the way Add Selected Bones to Rig adds one: a rig node under its parent's node and a game bone with the same name, parent and place, linked to it - so the weights to them are kept. They are placed in the rig's own space: the base's rig keeps its skeleton below `Main` lower than the game bones (McQueen: by 0.44), and the new nodes follow the same rule. A new `..._SpinJoint` also gets the driven key the character's own wheels spin by - `RotateFront` for front wheels (`TireF...`), `RotateRear` for the rest - with the same curve and axis.
- **Position:** the two skeletons agree to about a centimetre, so the meshes are moved by the average offset of the four wheel hubs and the wheels spin about their own centres.
- **Textures are not converted.** Cars 3 UVs are used as they are, but the Cars 2 textures were painted for the Cars 2 UVs, so they will not line up until they are redone.
- Anything that was on the Cars 2 base but has no Cars 3 counterpart (e.g. a spoiler mesh) is gone from the result; its bone stays in the rig.

Details go to the `Cars3ToCars2Report` text. Outside Blender: `core/c3toc2.py` - `convert(cars3_json, cars2_oct_or_json, out_dir)`.

## Cars 3 models in Blender: controls move the model

Animation clips move the controls (`Body_Ctrl`, `Jaw_Ctrl`, the lip and wheel controls); the mesh is weighted to the skin bones. The Cars 3 import connects the two the way the game does: each skin bone follows its control - `X` <- `X_Ctrl`, `Body` <- `Body_Final_Proxy`, the wheel hubs <- `FLeft_Center_Ctrl` ... (the spin joints hang under the hubs). Each skin bone copies a hidden helper bone (`X_Follow`, in Rig Internals) that sits exactly on it and hangs under its control: nothing moves at rest, the bone turns about the control's pivot when the control moves, and Copy Transforms replaces the bone's own parenting so no motion is applied twice. Wheel spin: as on Cars 2 imports, each wheel's own spin joint turns about its axle from `Main_Ctrl/RotateFront` (front) or `RotateRear` (back), in degrees - Cars 3 clips spin the wheels too, since the converter turns their per-wheel `Spin` channels into those. Extra wheels: a spin joint hanging under another wheel's hub copies that hub's own spin joint (Copy Rotation, local space) - Mack's middle tires (`TireMidLeft_SpinJoint_OL` under `TireBLeft_centerJoint`) spin with his back tires, as his Cars 3 rig does; on export they're new spin joints that get the rear wheels' `RotateRear` spin key, built the way Cars 2's own six-wheeler (`bg_explodingminion`) builds its second rear axle: each extra wheel gets its own hub joint under `Main` (`TireMidLeft_centerJoint`, the spin joint under it), and a copy of the back wheel's control chain under the same `BackTires_Ctrl`, moved to the extra wheel (`MidLeft_bttm_Ctrl_Grp` -> `MidLeft_steer_Ctrl` -> `MidLeft_bttm_Ctrl` -> `MidLeft_Center_Ctrl`); the copies of the back wheel's clip-driven controls copy their rotation only, and the hub copies its Center control (orient + point). So the extra wheel tilts with the back wheel about its own centre and rides with the rear axle - Cars 2 clips pitch the back wheels (a launch squat, up to 9 deg in `shared_quickstart_go2`), and a wheel hung on the back hub 1.24 in front would swing like a lever and float. The copied chain also rides on the back wheel's suspension travel: it hangs from a follower on the back wheel's `bttm` control that keeps the axle's orientation (`MidLeft_bttm_Ctrl_Follow`, orient from `BackTires_Ctrl`) - the game's physics moves the four wheels it knows onto the road, and an extra wheel left on the axle would hang above it, so it moves up and down with the back wheel, still without its tilt. No new clip channels, no .bent change (any bone with `_SpinJoint` in its name is a spin joint; `TireF...` ones spin with the front). **Connect Controls to Skin** (Cars 3 to Cars 2 panel) adds this to a Cars 3 model that doesn't have it yet. Not connected yet: Cars 3-only parts without a Cars 2 control (Fritter's blade, the chain proxies) and the eyelid bones the Cars 3 eye sliders move.

## Clip rotations

Clips store rotations two ways, with two conventions. **Euler records** (animated rotations - a two-wheel stunt's body roll) are plain angles, as Maya wrote them. **Quaternion records** (constant rotations - a face pose's jaw) are stored conjugated, like the rig's own rotors. Import conjugates quaternion records and Export Clip writes constant rotations conjugated (`CLIP_QUATERNION_CONJUGATED` in `anim.py`), so clips round-trip exactly. (Inverting Euler records too would put `shared_2wheelleftstart` on the wrong wheels; unconjugated quaternion records swing jaw-open faces up into the head.)

## Cars 3 animation clips

The Cars 3 (Driven to Win) clip converter (`core/cars3_to_cars2.py`, with its port map, slot map and notes in `core/CARS3_ANIM_NOTES.md`) is built in. Cars 3 and Cars 2 store curves the same way; the converter translates the channel addressing (Cars 3's one 205-port template -> Cars 2's `car_body`, `car_facial`, `car_frontend_frontend` ... channels), the record layout, one field op and the header, and drops (and reports) Cars 3-only channels.

In the animation panel / File > Import:

- **Import Cars 3 Clip** - converts the clip, then imports it exactly like a Cars 2 clip (same frame rate, bone space, channel map and scene options), onto a Cars 2 character or a Cars 3 import. Options: **Mode** (Auto - frontend for `*_fe_*` / `select_*` clips, otherwise game; Game: `car_body` + `car_facial`; Frontend: `car_frontend_frontend`; Templates: any Cars 2 templates, e.g. `car_body,car_rattles` for Rattles / Eyes / Eyelids / BodyBlend / Lean / ChoBlender / Mouth clip sets), **Use Cars 2 names** and **Cars 2 base clip** (below). Scale keys are kept on face bones only and Cars 3 rest-pose statics are dropped; the built-in port map is complete, so there is nothing else to set.
- **Cars 2 base clip** (optional, in Import Cars 3 Clip) - Cars 2's setup for the Cars 2 components, with the Cars 3 clip on top: the Cars 2 clip for that slot (e.g. the character's own select idle) is loaded first, then the Cars 3 clip replaces only the channels it drives; everything Cars 3 doesn't animate keeps its Cars 2 animation. Timed by the Cars 3 clip; **Export Clip** writes it with the base clip's Cars 2 bindings. A base clip of a different kind (game vs frontend) is warned about.
- **Use Cars 2 names** (on by default) - converted clips are named the way Cars 2 names that kind of clip, the file and the clip inside it: `shrd_` -> `shared_`, `trk_` -> `truck_`. Character clips become class clips: Cars 3 names them after the character (`mack_quickstart_l02`), Cars 2 after the class that shares them (`truck_quickstart_l02`). The rules are in `core/clip_names.json` - add to it as you find more: `"classes"` lists the Cars 3 characters of each Cars 2 class (`"truck": ["mack"]` -> `mack_` becomes `truck_`), `"prefixes"` maps the start of a Cars 3 name to the Cars 2 start (`"shrd_": "shared_"`), `"names"` maps whole names (`"some_cars3_clip": "some_cars2_clip"`, checked first). The longest matching start wins. Lower case, without `.oct`.
- **Cutscenes** - a Cars 3 cutscene clip (`cars3_<actor>_clip.oct` from a cutscene zip, `assets/choreographies/cc/ccl_*.zip`) goes through the same **Import Cars 3 Clip**; nothing else to set. In a cutscene each car has a second clip, `cars3_<actor>_pn_clip.oct`, with its path through the set (location + rotation, template `Choreographies/<cutscene>/placement.ct`), and the actor clip's own root (Cars 3 port 0, which no Cars 2 clip has) moves it on top of that path. The import finds the path clip next to the clip (an unpacked cutscene) or, for a clip copied out of one, in the cutscene zip holding the same clip (searched in the `choreographies` folder above the clip, or the one of the game the Cars 3 character came from), and builds the chain the game uses: an empty per cutscene (`ccl_a_game_intro`, carrying the Y-up -> Z-up turn), `<Actor>_PN` (the path, keyed as stored), `<Actor>_Root` (the clip's root motion, only when it has one) and the armature under it. Import each car's clip onto its own character and they play the scene together (same cutscene empty). A clip that isn't from a cutscene puts the armature back where it was. Not imported yet: the cutscene cameras, props, lights and the set.
- **Convert Cars 3 Clips to Cars 2** - batch: pick several Cars 3 clips, get Cars 2 `.oct` files in a `cars2_clips` folder next to them, and a `Cars3ClipReport` text.

Outside Blender (`core/`): `cars3_to_cars2.py` (one clip, CLI), `clip_slot_map.py` (which Cars 3 clip fills each Cars 2 slot, per Cars 3 car - `slot_map.json` / `.csv` cover all 43 cars against McQueen's slots) and `port_car.py` (convert every slot of one car and write a `bent_patch.json`).

## Custom models: plain rigs

The way to put a fully custom model in the game. You rig it to a plain skeleton and the export builds the whole Cars 2 rig around it. You don't rig to the full control rig (130+ bones).

**The rig the export takes:** one armature with the base character's **game bones**, under their Cars 2 names and in the game's hierarchy. That is `Main` > `Body` > `FrontEnd` > `Jaw` ..., plus the wheel hubs (`TireFLeft_centerJoint` ...) and their spin joints. **Import Cars 2 Character** with *Import Control Rig* off makes exactly this, with the meshes and materials. A plain importer script's skeleton works too, or one built by hand or kept in a .blend to share. No control bones, no constraints. Then:

- **Model:** weight your meshes to the bones (vertex groups named after them). Give the faces the base's materials (`McQueen_Body`, `Glass`, `Headlights` ...). Other material names become new materials, copies of the closest one. A mesh with no material, named after the base's mesh node (`bodyMesh_McQueen_Body`), takes that node's material.
- **Orientation:** what stands upright in Blender exports upright, facing Blender's -Y (the way an imported Cars 2 character faces). A model built in Blender or brought in from an .fbx with its transforms applied (Z-up, armature rotation 0), a model in the toolkit's own layout (data Y-up, armature object turned +90 X, as every import makes it) and an FBX armature scaled 0.01 (centimetres) all export the same (`cars3_export.game_frame` + the armature's scale). The armature's location stays the game origin. A model facing another way than the base (from the wheel bones) is refused with a message.
- **Bones:** put them where your model needs them (Edit Mode). Bones of the base your rig lacks stay where the base has them. `Body` is required. `Main` may be missing: many importer scripts make Main the armature object instead of a bone, with `Body` and the hubs as root bones - Main then stays at the base's ground origin. Bones with Cars 2 game bone names count even with *Deform* unticked.
- **FX markers (optional):** bones named `FX_Marker_<Name>` (or `FX_Marker_<Name>_Ctrl`). A marker the base has moves to its bone and keeps the base's rotation. A new name becomes a new marker, with the bone's rotation. (Your model's own meshes, eyes included, are always used.)
- **Extras**, in the *Custom Model* panel (Cars 2 Character tab, with the rig selected):
  - **New bones** that ride along: add the bone (parented to a game bone) and weight vertices to it. **Add Selected Bones to Rig** adds one even without weights.
  - **Animate in Clips**: a new bone that clips move (a spoiler, a blade). It borrows an empty clip slot of the base (Mater's tow-crane slots first), like the Cars 3 parts.
  - **Make Dynamic Chain**: bones that swing as the car moves, like Mater's tow hook (see below).
  - **Extra wheels**: a `TireMidLeft_SpinJoint` (any `..._SpinJoint`) parented to a back hub (`TireBLeft_centerJoint`) becomes a six-wheeler axle, built like Cars 2's own.

**Export:** select the rig (or a mesh rigged to it; with nothing selected, the scene's only armature with a `Body` bone is used) and press **Export as Cars 2 Character** (panel, or File > Export > Cars 2 Character (.oct)). Pick the base character (`.oct` or offsetting's `.oct.json`); after the first export it is preselected. The result always goes to an `export` folder next to the base (no dialog options): `<car>.oct` + `<car>_0.vbuf` + `<car>_0.ibuf` (onto a `.oct.json` too - encoded with offsetting.exe).

How the rig follows your skeleton: every game bone goes where your bone is. Every control moves with the bone it drives: `Jaw_Ctrl` with `Jaw`, `Body_Final_Proxy` with `Body`, `FLeft_Center_Ctrl` with the front-left hub. Each control group moves with the nearest such control below it. The back-axle nodes (`Main_Baxle_Ctrl`, `Body_Baxle_Ctrl`) move with the back hubs. Ground controls (the wheel `bttm` controls) keep their height. `Main_Ctrl` and above don't move. Bone rotations are not used (positions only), except for markers as above. Then the same steps as Cars 3 -> Cars 2 run: the axle-at-zero shift (in-race bases only - not a base inside a `frontend` folder), new bones, chains, animated parts, extra wheels, tangents on every mesh, the base's vertex-stream layout.

**Need to change the rig itself** (constraints, driven keys, float controls)? Import the exported character with **File > Import > Cars 2 Character** and *Import Control Rig* on: that gives the full control rig and the *Cars 2 Rig* panel.

Note: an importer script that wraps UVs into 0-1 (`u % 1.0`) changes them. Tiled UVs come out wrong after export.

Details go to the `CustomRigReport` text. Outside Blender: `core/c3toc2.py` - `derive_rig_pose(tree, pose)`, then `build_geometry(...)` and `export_onto(...)` as for Cars 3.

**Character template** - for a model that is already Cars 2 rig-ready (an FBX with the game bone names, meshes/materials named like the character's): in the **Custom Model** panel press **Import Character Template** and pick the Cars 2 character's `.oct`. Its materials and textures are put on your meshes - each material slot gets the Cars 2 material its material name (or else its mesh name) carries, the same pairing the export uses (`McQueen_Body`, `eyeshader` ...); your model's own materials that are no longer used are removed. They are full game materials (Cars 2 Material panel: effect, settings, textures). On **File > Export > Cars 2 Character (.oct)** onto that same character, only what you changed in them is written (untouched: MaterialPool and TexturePool identical to the base), and the bone positions come from your rig. Materials no mesh uses are never checked (King, a McQueen port, keeps Headlights / BrakeLights / McQueen_Body with texture references past its texture list - the game never draws them). Problems the untouched base already has (King's right exhausts renamed `Null`, two same-named header entries) don't block an export either: they are kept exactly as in the file and listed in the report as *in the base already*. Only the texture slots a material's own entry lists can block the export: a slot its effect could sample but the entry leaves out (mcqueendtw's `eyeshader` has no specular / environment reflectivity map) ships empty like the game's own file and is not marked *needs a texture before export*. Matching also ignores case, spaces, `_`/`-` and a plural s (`Headlight_L` -> `Headlights`). A mesh with one material whose own name is a Cars 2 material's (`Barry_Chrome`, `bodyMesh_Barry_Chrome`, `..._Barry_Chrome`) gets that material even when it shares its material with other meshes (a chrome part split off Barry's body gets the `.oct`'s unused `Barry_Chrome`, not `Barry_Body`). Each mesh's active UV map is renamed `Uv1` - the name the game shaders read (an FBX's `UVMap` / `map1` showed no decals) - the export reads that same map. Every Cars 2 material of the template stays in the file, so a slot that matched nothing can be given one from the normal material dropdown; such slots are listed in the panel and the *CharacterTemplateReport* text, and otherwise export as new materials. Picked the wrong `.oct`? The **X** next to *Template:* in the panel removes it: your model's own materials go back in their slots with their own names (they are kept in the file meanwhile, as `<name>_fbx` where a Cars 2 material needed the name), the UV map names come back, and the template's materials and textures are deleted. While a template is on, the panel shows only its row with the X (no re-import button): remove it, then import another. (F3 Import Character Template over one still works and re-matches from your model's own materials.) Run it again to start over from the `.oct` (material edits are lost). Exporting onto a different base than the template, or onto a `.oct.json`, uses the base's own materials (warning).

## Custom bones on Cars 2 characters (Cars 2 Rig panel)

The same building blocks as Cars 3 -> Cars 2, for a Cars 2 character you edit (Import Cars 2 Character, Export Character). Add new bones with **Add Selected Bones to Rig** first (parented to a bone already in the rig, mesh weighted to them).

- **Rides along**: nothing more to do - a new game bone and rig node under its parent.
- **Moved by animations - Animate in Clips**: the game only animates the slots its clip templates define, so the bone **borrows an empty slot** of the template (Mater's tow-crane slots first) and is built like the face: the bone stays in the skeleton, a group at its place (`<Bone>_Clip_Grp`, following what moves its parent) -> a control named after the borrowed channel (e.g. `TowCraneBase_Ctrl`), and the bone copies the control (orient + point). In Blender the slot is your bone: import a clip and Export Clip animate it directly. No `.bent` needed (the template catalog lists the slots); a `.bent` is only needed for float controls, which are still appended past the template and not animated in-game yet.
- **Swings - Make Dynamic Chain**: for parts that swing as the car moves (license plates, a hook, an antenna):
  1. Add two or more bones in a line, root first, the root parented to `Body`.
  2. Weight the swinging part to the lower bones.
  3. Select the bones, press **Make Dynamic Chain**, export.

  It swings in the game only, not in Blender. Parts that hang down swing best.
- **Follows another bone - Convert Constraints**: Copy Location / Rotation constraints become Cars 2 constraints.
- **New FX markers**: a bone named `FX_Marker_<Name>_Ctrl`, added to the rig, is exported as the marker `FX_Marker_<Name>` (a Transform) and connected to its clip-template slot when Cars 2 has one (`Exhaust_Left1/2`, `Exhaust_Right1/2`, `Headlight_*`, `MachineGun_*`, `Battery_*`, `Bumper`, `SkateJack`, `SpareTire`, `WGPCam`) - that is how the game finds a marker.

Not yet on this route: extra wheels (use a plain rig - Custom models - or Cars 3 -> Cars 2), float channels in-game.



## Cars 3 rigs in detail

### Mater's tires and tow rig follow his clips

- **Tires:** wheel hubs are found by their `Tire<side>_centerJoint` start, so variants count too - Mater's skin uses `Tire<side>_centerJointSuspension`. Those hubs follow the wheel controls (`FLeft_Center_Ctrl` ...), the spin joints hang under them and get the spin drivers.
- **Tow cable and hook:** wired as the game builds them. The cable ends and the hook ride on the crane (`CableRigEndCenter_Proxy`, `CableRigWheelEnd<side>_Proxy` under `TowCrane_Ctrl`; `TowHook` on `TCable_End`), and the cable bases at the back aim at their ends (helper `<bone>_Aim` with a Damped Track, the game's aimConstraint1 nodes).

### Cars 3 clips play the model's own parts

Import Cars 3 Clip converts the clip to Cars 2 first, and Cars 2's clip templates have no slot for parts only some Cars 3 cars have - so a plain conversion drops them (Fritter's blade, Mater's side mirrors). On a Cars 3 model in Blender the import reads the model's own port table (which port drives `Anchor/Blade_Ctrl/Transform` ...), routes each dropped port whose bone the armature has into a template slot the armature has no bone for (Mater's crane slots first, as Export Cars 3 as Cars 2 does), and points that slot at the bone. The report lists them as "+ the model's own parts". Not used when you set Part ports yourself.


### Cars 3 chains swing as in the game

Fritter's side chains (with the license plates) are DynamicChain physics in the game, not clip channels. The import builds them in Blender from the model's own DynamicChain data (anchor `chain<Side>_Start` riding on Body, attach point, swinging particle with its NaturalFrequency / DampingFactor / mass / drag, link length, IterationRate, GlobalDamping and the DynamicChainExtentBox container), and every clip import simulates the chain over the clip's frames with the add-on's own solver - a damped spring pulling the swinging part towards its rest place, the link length kept and the part held inside the container box, using the settings stored in the model (standard position-based physics: Jakobsen, GDC 2001; Mueller et al., 2007) - and bakes it onto `<chain>_SimTarget`. The proxies that ride on the swinging particle follow `<chain>_Swing`; the other links turn towards it (`<link>_Aim`), as their aim constraints do in the game. The import report ends with "Cars 3 chains simulated: ...". Like the game, the swing starts from rest at the clip's first frame.

### Arvy's panel, moldings, muffler and axles

- **Every Cars 3 chain is found and wired from the rig itself.** The chain's anchor is read even when the node is named like `PanelBase to WorldBasis.Matrix4x4`, and the bones a chain moves are traced through the model's network: a bone the chain places rides on its swing (Arvy's `PanelEnd`, `MoldingLeftEnd`, `MoldingRightEnd`, `MufflerEnd` sit on the attach point and aim at the swinging particle), a bone it only turns aims at it.
- **Axles** (`axelLeft` / `axelRight`): each sits on its wheel hub and aims at the opposite hub, as the game's AxelAim aim constraints do, so the bar tilts with the suspension.

### The swinging parts move the model

Names in the Cars 3 network come from the header tree (as the port reader does), so each chain is anchored and attached to the bones the mesh is skinned to:
- anchors `PanelBase`, `MoldingLeftBase`, `MoldingRightBase`, `MufflerBase` (Fritter: `chainLeft_Base`, `chainRight_Base`);
- bones the chain places (attach point + aim at the swinging particle) ride the swing: Arvy's `...Start` (skinned) and `...End`;
- bones that take only the swing's turn (a `DilationOrientationConnection` of the aim - Fritter's chain links `chain..._Proxy01..010`) keep their place and copy its rotation.
Checked on the meshes: over the idle clips the part vertices move 7-11 cm against the body.

### Every Cars 3 shrd_ clip becomes a shared_ clip

- **`shrd_` -> `shared_` everywhere.** The clip name rules are part of the core converter, so every conversion path uses them: Import Cars 3 Clip, Convert Cars 3 Clips, the `cars3_to_cars2.py` command line and `port_car.py`.
- The rule is **built in**, so it still applies when `core/clip_names.json` is missing or broken. Upper or mixed case counts too (`SHRD_BM_InAir` -> `shared_BM_InAir`). Command line: `--cars3-names` keeps the Cars 3 names.
- **Simple Cars 3 clip dialogs.** Converted clips always go to a `cars2_clips` folder next to the Cars 3 clips (.oct only). Scale keys are kept on face bones only, and Cars 3 rest-pose statics are dropped.
- **Cars 3 parts just work.** Import the Cars 3 character, Export Cars 3 as Cars 2, then Convert Cars 3 Clips: the clips animate the character's extra parts (Fritter's blade) with nothing to pick. The export remembers which Cars 2 channels the parts got on the Cars 3 model in Blender (saved with the .blend), and the converter takes them from the character the clips belong to. If you convert the clips before exporting, the report says which parts were not animated. The built-in port map is complete, so there is nothing to pick.
- **Frontend clips get their Cars 2 names.** A character's frontend clip drops the character: `mcqueen_fe_select_idle` -> `select_idle`, `mack_fe_select_idle2` -> `select_idle2`, `river_fe_select_selected` -> `select_selected` (the file and the clip inside it, as every Cars 2 character's `motions` folder names them). The two Cars 3 spellings Cars 2 doesn't use become the Cars 2 name: `*_fe_selected` -> `select_selected`, `arvy_fe_select_idle1` -> `select_idle`. Shared frontend clips keep their `fe`, like Cars 2's `shared_fe_blocker`: `shrd_fe_horn` -> `shared_fe_horn`.
- **Export Cars 3 as Cars 2** is the button in the Cars 3 to Cars 2 panel; File > Export > Cars 2 Character does the same with a Cars 3 import selected. The export always takes the full-detail meshes (a part that only exists in a lower LOD is taken from there) and the model's own eyes.
- **Cars 2 panels.** The sliders (eyes, eyelids...) are a **Sliders** panel under Cars 2 Animation that opens and closes (closed at first; the header shows how many there are; Add Sliders is inside). Moved rig bones are recorded by Validate and Export.

## Credits and license

- Cars 2 Toolkit: Pentane project.
- The material system (`look/`) and its format readers are shared with the [Cars 2 Scene Editor](https://github.com/MythicalBry/cars-2-scene-editor) by MythicalBry.
- `.oct` reading follows [matryoshka](https://github.com/offsetting/matryoshka); `.oct.json` and zip packing interoperate with [offsetting](https://github.com/offsetting/offsetting); vertex decoding is adapted from [cars-2-blender-io](https://github.com/offsetting/cars-2-blender-io) (all Team Offsetting).

Licensed under the GNU General Public License v3.0 or later (`LICENSE`). Third-party code and its licenses: `THIRD_PARTY_NOTICES.md`.
