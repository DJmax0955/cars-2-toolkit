# Cars 3 pipeline and custom rigs

Cars 3: Driven to Win uses the same Octane formats as Cars 2 (Tupperware `.oct`, the same clip curve format,
similar rig naming) with different addressing and materials. The toolkit uses that overlap three ways: it
imports Cars 3 models, it puts Cars 3 meshes on a Cars 2 character, and it converts Cars 3 clips.

## Importing a Cars 3 `.oct` (`cars3.py`)

**File > Import > Cars 3 Character (.oct)** (`cars2tk.import_cars3`; an `.oct.json` works too). It reads the
model and its `<name>_0.vbuf` / `_0.ibuf` (`read_cars3`). Skinned geometry means a character; anything else
(weapons such as `loadout_roadmine`, pickups, props) goes through `import_prop` as its node tree, with
`c3_node` on bones and faces.

For a character, `import_cars3()` builds an armature **laid out like a Cars 2 import**, so the toolkit's tools
see the same thing:

- **Skin skeleton** (`skin_binds`, `build_armature`), with its hierarchy from the Cars 3 rig (`RB_Main → Main →
  Body → face bones / wheel hubs`).
- **Control rig** under `Main_CtrlGrp`. Each Cars 3 node gets the name the Cars 2 importer gives the same node
  (`cars2_name`): `FX_Marker_X → FX_Marker_X_Ctrl`, `Body_VSM_Proxy → Body_Baxle_Proxy`, face
  `X_CtrlGrp → X_Proxy`. Cars 3-only nodes and pass-through groups are left out, and their children move up.
- **FX markers**, parented to their frame.
- **Meshes**, one object per primitive (named after material and LOD), weighted to skin bones (`build_meshes`;
  `ground_shift` puts the wheels on the ground).
- **Textures** (`cars3_textures.py`) use the Cars 3 material decoder carried in `c3look/`
  (`.mtb` records → texture hashes → `.tbody` → DDS; AG normal maps to RGB; gloss to roughness). Images are
  packed into the `.blend`. Material **names** are kept, because the export pairs parts by them.

The controls are structure only. Cars 3 links them to the skin through a different network, so **Connect
Controls to Skin** (`connect_controls`: Follow constraints and spin links) wires them up for posing in Blender.

## Cars 3 character → Cars 2 character (`cars3_export.py` + `core/c3toc2.py`)

**Export Cars 3 as Cars 2** in the **Cars 3 to Cars 2** panel (N-panel only; not in File > Export). You pick the Cars 2 character
to build on (its `.oct`; the `.bent` and buffers are found next to it).

**The Cars 2 character stays the base.** Its rig, skin skeleton, materials, vertex layouts, animation channels
and every cross-referenced number are kept. Only the geometry is replaced, through `c2geom.rebuild`, the same
path as the Blender mesh export ([Geometry](Geometry.md)). This works because:

- **The skeletons match.** Cars 3 and Cars 2 name their skin bones alike (`Body`, `FrontEnd`, `Jaw`, `Lip*`,
  `Cheek*`, `Brow*`, `Tire*_SpinJoint` ...), so weights carry over by name.
- **The positions match.** The skeletons sit within about a centimetre of each other. With *Move skeleton to
  Cars 3* off, the meshes are moved by the average wheel-hub offset (`alignment_offset`). With it on (the
  default), the Cars 2 bones, controls and markers move to the Cars 3 positions (`plan_retarget`,
  `derive_rig_pose`, `place_wheel_rig`, `apply_retarget_moves`).
- **Materials are matched by kind** (`material_kind`, `pick_material`): body, tires, eyes, headlights, brake
  lights, glass.

Other steps:

- **Extra wheels** (`plan_extra_wheels`, `native_extra_wheels`).
- **New bones** marked in the panel (`c3_new_bone`, `plan_new_bones`).
- **Animated parts** take free template slots (`free_part_slots`, `assign_part_slots`, `add_part_channels`), the
  same borrowing as Animate in Clips.
- **Dynamic chains**: `find_cars3_chains` reads them from the Cars 3 rig, `add_dynamic_chain` builds them, and
  `link_chains_lazy` places them.
- **LOD**: export one level of detail.

Textures do not carry over: Cars 3 UVs are kept, but the Cars 2 textures were painted for Cars 2 UVs.
`c3toc2.convert()` runs the whole thing from a script with no Blender.

## Custom rigs (`template.py`)

**File > Export > Cars 2 Character (.oct)**, or the **Custom Model** panel. Any armature whose bones carry
a base character's game bone names (`Main`, `Body`, `FrontEnd`, `Jaw`, the lips, the wheel hubs ...) in the
game's hierarchy, with a model weighted to it, can export as a full Cars 2 character. That covers a plain
importer script's output or a hand-built rig. FX markers are bones named `FX_Marker_<Name>` (or `..._Ctrl`). The
export rebuilds the base character's whole game rig (controls, constraints, clip channels, wheel setup) around
it, using the Cars 3 pipeline (`derive_rig_pose` plus the export path). Bones marked **Animate in Clips** borrow a
clip slot (`c2_template_anim`). To change the rig afterwards, import the exported character normally.

**Orientation (`cars3_export.game_frame`).** The game is Y-up; Blender is Z-up. Every toolkit import
keeps its data in game space and stands it up with a +90° X rotation on the armature *object*. Reading bones
(`matrix_local`) and meshes in armature space as game space is right for those imports, but a model built in
Blender (an `.fbx`, transforms applied: Z-up data, no object rotation) would come out turned 90° about X, standing on its nose, rig and all. `game_frame(arm)` is
`Rx(-90°) · (the armature's world rotation)`, read from the objects' own transforms (`matrix_basis` up the parent
chain, never the possibly stale `matrix_world`), with entries snapped to exact 0 / ±1. For the toolkit's own
imports it is exactly the identity, so their exports are unchanged by it. Every export read goes through
it: `template_pose`, `template_skeleton`, `collect_parts` (meshes), and the Cars 3 `skin_bone_positions`,
`cars3_pose` and `cars3_skeleton`. Location is not applied. The custom-model export (`template.game_space`) also applies the armature's world scale (a centimetre rig scaled 0.01 came out 100x too big) and refuses a model whose wheel bones face another way than the base's.

## Cars 3 clips

See [Animation](Animation.md#cars-3-clips-and-cutscenes) and `core/CARS3_ANIM_NOTES.md`:

- `cars3_to_cars2.py`: the converter, with port mappings and modes;
- `clip_slot_map.py`: Cars 2 slot → Cars 3 clip per car, with a confidence score;
- `port_car.py`: converts every slot of a car and writes `bent_patch.json`;
- `c3anim.py`: the in-memory wrapper the add-on uses.

### Character template

`template.import_template(context, arm, base)` (operator `cars2tk.import_template`, Custom Model panel): the
base's MaterialPool becomes game materials owned by the custom rig (`charmat.setup`, TexturePool in
`arm.c2tex`, `arm['cars2tk_template_materials']` = the base). Slots are paired with `c3toc2.name_match`
(material name, then object name). `template.object_names_material` goes first for a one-slot mesh: an object named like a base material (`Barry_Chrome`, `bodyMesh_Barry_Chrome`, ending `_Barry_Chrome`) gets it, so meshes sharing one material are told apart. Importing the same template again re-matches a slot whose entry was only that old material-first match (a hand pick stays). A base material with no mesh node of its own (Barry's unused `Barry_Chrome`) gets a new mesh node on export; `geometry.collect` (the normal Export Character) no longer lets such a mesh take another material's node. On export, `export_template` passes `charmat.exporter()` to
`c3toc2.export_onto(..., materials=)`; `export_oct_base` applies it after `_rig_and_meshes` (base MaterialPool
indices are unchanged there - new materials are only appended). Only for the same `.oct` base.

## Dynamic chains in detail

Cars 2 swings Mater's tow hook with a `DynamicChain`: the game simulates a line of bones every frame from how the car moves, so it reacts to every animation by itself. A Cars 3 model's own chains are found on import and listed under **Dynamic chains** in the Cars 3 to Cars 2 panel, ported the way Cars 3 builds them: a Cars 3 chain swings one point around a fixed anchor frame, and only the proxy bones fed from that point move (Fritter: `chainLeft_Proxy01`), while the other proxies stay in place but **turn with the swing** (their orientation is the chain's aim) - so each Cars 3 chain is ported with one of the two setups that worked in-game, chosen by its shape: a chain whose bones hang mostly downwards (Fritter's right chain, `chainRight_Proxy01` 2.40 high -> `Proxy02` 1.82) becomes a plain chain over its own bones, root first, like Mater's tow cable; a chain lying sideways (her left chain, 2.7 along her side) becomes the anchor (`chainLeft_Start`, a weightless bone made on import) -> the swinging proxy (`chainLeft_Proxy01`), with the other proxies turning with it (below). (A separate pendulum particle, or a mount above the chain, made both chains worse in-game.) and each turning proxy gets the swinging bone's rest rotation plus an orient constraint from it (added after the chain), so it turns in place by the same angle; **Make Dynamic Chain** makes one from selected skin bones (root first: by the number at the end of their names, else along their parent line), with Mater's settings - or, when it replaces a chain found in the Cars 3 model, with that chain's settings. On export each chain's bones are added in a parent-to-child line and get a DynamicChain with its settings block, lazy group node and scene links laid out exactly as on Mater (rebuilt with Mater's settings, the block is byte-identical to his). Settings: Cars 3 chains keep their own (Fritter: frequency 3.14, damping 0.95, inverse mass 1.0); each link springs back towards its rest place relative to its parent, with gravity. Each chain bone is turned to face along the chain as on Mater (the next link on its local +Z, X kept as close to the original as possible); the rig node and game bone get the same frame, so the mesh doesn't move - the game aims each link at the next, and bones facing another way twist. Not carried over from Cars 3: the chain's container box. (An imported Cars 3 model's chains are simulated in Blender on every clip import; chains made with Make Dynamic Chain only swing in the game.)
