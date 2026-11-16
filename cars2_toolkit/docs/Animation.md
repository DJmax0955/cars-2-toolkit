# Animation clips (`anim.py`)

`anim.py` began as its own add-on ("Cars 2 Animation (.oct)") and still has its own `bl_info`. The toolkit
registers it as one of its modules. Its sections, in file order:

1. ClipDataBlock codec
2. curve fitting
3. container backends
4. built-in channel maps
5. bindings
6. bind block writing
7. Action API compatibility
8. the mouth relationship
9. rig conventions
10. import
11. export
12. operators and UI

Operators, under **File > Import/Export > Cars 2 Animation (.oct)** and the **Cars 2 Animation** panel in the
Cars 2 Character tab:

| Operator | `bl_idname` |
|---|---|
| Import Clip | `import_scene.cars2_oct` |
| Export Clip | `export_scene.cars2_oct` |
| Import Cars 3 Clip | `import_scene.cars3_oct` |
| Convert Cars 3 Clips to Cars 2 | `import_scene.cars3_oct_convert` |
| Add Cars 2 Sliders | `pose.cars2_add_sliders` |
| Remove Clip | `pose.cars2_remove_clip` |

## Clips in Cars 2: what they are and why they matter

### Why clips matter

Every motion a Cars 2 character makes beyond driving physics comes from a clip, including:

- body and suspension motion: idle bounce, braking shudder, drift lean, two-wheel driving, wall rides, bash
  reactions, weapon hits;
- stunts: jump tricks, 180s and 360s, landings, bunny hops;
- the race-start "quick start" revs;
- the face: mouth, cheeks, jaw and brows;
- the eyes and eyelids: looking around, blinks, eye darts;
- rattles: small additive shakes on top of everything else;
- the front-end (character select) idles, and cutscene choreography.

The car model is static geometry on a skeleton, so a character with a broken, missing or mis-bound clip has a
dead face, no tricks or frozen eyes, even though it still drives. A custom character or a new part (a spoiler,
a crane arm) only moves in the game if clips reach it. That is why the toolkit's rig edits always go back to
**clip channels**: Animate in Clips, borrowed slots and channel maps.

The game also decides *which* clip plays (by gameplay state) and *how* clips combine (layers, filters, additive
sets). The character's files describe that, and the toolkit has to keep it intact.

### The chain from game event to bone

```
gameplay state ("bunny hop")
  -> .bent ClipSet 'Body', Clip#BUNNYHOP          alias -> file  (characters/car/motions/shared_bunnyhop)
  -> clip .oct: SubNetwork Type 'Clip'           one ClipData node + LayerBinds (one per connection template)
  -> LayerBind 'car_body' -> bind block 0         which commands feed which template slots
  -> template slot 7 ('Body')                     a number; names appear in no file
  -> character .oct BaseDataNodeLinks 'Anchor-car_body', DataNodeLinks[7] -> data node Anchor/Body_Ctrl/Transform
  -> UberConstraint: skin bone 'Body' copies Body_Ctrl                    -> the mesh moves
```

Every arrow is a number lookup, not a name lookup. Change a slot order, a link list or a template in one place
and the motion lands on the wrong part. This is why `c2channels` grows templates in all three places at once,
and why `c2validate` checks slot kinds.

### 1. The `.bent`: clip sets, interfaces, filters and blenders

`<car>.bent` sits at the package root and names its scene (`SceneFilename 'characters/mcqueen/mcqueen'`).
McQueen's has these parts.

**ClipSetPool**: named libraries of clips. Each entry maps an **alias** to a file (no extension):

| ClipSet | Clips (McQueen) | Notes |
|---|---|---|
| `Body` | 123 | driving, tricks, reactions; `DefaultFilter 'Body-car_body'` |
| `Facial` | 44 | `emo_*` expressions; `DefaultFilter 'Facial-car_facial'` |
| `Rattles` | 20 | `Additive 1` |
| `Eyes` | 5 | eye darts, lower-left/right/centre looks |
| `BodyBlend` | 4 | `DefaultFilter 'BodyBlend-car_bodyblend'` |
| `LeanClipSet` | 2 | `Additive 1`: lean and drift lean |
| `ChoBlender` | 2 | `Additive 1` |
| `Eyelids` | 1 | `Shared_Blink` |
| `Mouth` | 1 | |
| `Choreography` | 1 | a `SurrogateClip#START` |

- An alias such as `Clip#BUNNYHOP` is what game code asks for. The file behind it can be shared by every car
  (`characters/car/motions/shared_*`) or be the car's own (`characters/mcqueen/motions/emo_*`). **Replacing a
  clip for one car means pointing its alias at a new file**, which is what `port_car.py`'s `bent_patch.json` does,
  not overwriting a `shared_*` file every car uses.
- A clip entry can carry a `Control` block. Shipped values include `{'PostBehavior': 4}` (BUNNYHOP) and
  `{'PostBehavior': 2}` (WALLRIDELEFT), which control what happens when the clip ends. The toolkit keeps these
  as they are and has not decoded every value.

**InterfacePool**: one interface per animation layer the character runs at once. Each has a `ClipSet`, a
`ConnectionTemplate` (for example `car_body`), a `ConnectionMap` (`Anchor-car_body`, the scene's link list for
that template), and a `BlenderChannelMap` (one u16 per slot). Most also have a `MotionIndex` and a `Priority`:
Facial has priority 1; Mouth -2; Choreography and ChoBlender -1. Several interfaces drive the same controls: on McQueen, Body, BodyBlend, Lean, Rattles and
Choreography all reach `Body_Ctrl`, and Facial, Eyelids, Mouth, Rattles and Choreography all reach `Jaw_Ctrl`.
The game blends their results.

**FilterPool**: per-slot weights (`Filter 'Body-car_body'` = one float per `car_body` slot). A filter restricts
a clip set to part of a template. The Facial and Eyelids layers share one 68-channel output list, but the
Eyelids filter is 1 only on the brow and lid controls, so a blink never fights the face.

**Blenders**: one entry per animated data node (`Blender#Anchor/Jaw_Ctrl/Transform`). Each holds the channel
type (0 transform, 1 KinematicState, 2 Float, 3 Visibility), the owner interface, `ChannelFlags` (one byte per
interface that uses it, so `Body_Ctrl`'s 5 users give 5 bytes; bit 0 means that user's clip set is additive), and optional defaults in
`DefaultBuffer`. This is where several layers writing one control are combined.

### 2. The clip file

A clip is a small `.oct` whose scene network has `Type 'Clip'`. Here is
`characters/car/motions/emo_idle_lookdown.oct` in full:

```
SubNetworkPool/SubNetwork#0
  Type 'Clip'
  HeaderStrings ['ClipData-emo_idle_lookdown']          the clip's internal name
  DataNodePool/DataNode#0   Type 'ClipData', Static 1, ClipDataBlock <3604 bytes>
  LayerBindPool/
    LayerBind#0  Name 'car_body'   ConnectionTemplate 'characters/car/car_body.ct'   ClipDataCommandBlockOffset 0
    LayerBind#1  Name 'car_facial' ConnectionTemplate 'characters/car/car_facial.ct' ClipDataCommandBlockOffset 1
ExporterDate 'Nov 24 2010'
```

- **One file can animate several layers.** Each `LayerBind` says which connection template its part targets,
  and `ClipDataCommandBlockOffset` picks the bind block inside the ClipDataBlock. `emo_idle_lookdown` moves the
  body and the face together; `shared_brake_shudder` binds `car_body` only.
- `.ct` files named in `ConnectionTemplate` **do not exist** in the game. The path is only an identity. The
  slot layout is what the `.bent`, scene and clips agree on (see [File formats](FileFormats.md)).
- The block header's first float is flags (0 in the clips checked). The second is the **duration in
  seconds**: `emo_idle_lookdown` 3.333 s (100 frames at 30 fps), `shared_brake_shudder` 0.533 s,
  `shared_jmptrick_360b_rev` 1.0 s. Clips are authored on a 30 fps grid; the curves themselves are continuous
  Beziers.
- The same ClipDataBlock format also stores a rig's **driven keys**: a ClipProcessor in the character's own
  network (McQueen's scene has 4) whose curve is driven by a Float instead of time.

### 3. Inside the ClipDataBlock

```
header: f32 flags, f32 duration | u16 n_binds, u16 n_cmds | u32 pool_offset | u32 offsets[n_binds + n_cmds]
bind blocks (one per LayerBind)
commands (curves)
constant pool (f32)
```

- **Commands** are curves. One command animates several channels over shared key times, as Bezier segments.
  Values are quantised (`bias + scale * P` with i16 deltas, opcode 0) or stored as floats (opcodes 4 and 6;
  opcode 2 is used by driven keys). Pre- and post-extrapolation modes are CLAMP, LINEAR, LOOP, LOOP_OFFSET and
  PINGPONG; shipped clips use CLAMP.
- **Bind blocks** route command outputs to template slots. A bind block lists the commands it reads (their
  channels form a buffer), then records of the form `[target slot] + sub-ops`. A sub-op says what to write
  (location x/y/z, Euler rotation with its order, quaternion, scale, or a scalar). Each operand reads either the
  command buffer or the constant pool.
- **Static records** write constants once, so a clip can pin parts of a pose without spending curves on them.
  This is why the exporter splits "never moves" components into the pool.
- **Stored values are offsets from the control's rest** in its parent's axes, and quaternion records are
  conjugated. Get either wrong and the jaw swings into the head.

### 4. How the character receives it

In the character `.oct`, `BaseDataNodeLinksPool` holds one link list per template. McQueen has 12:
`Anchor-car_body` (37 links), `car_eyes` (4), `car_choreography` (113), `car_leanclipset`, `car_facial`,
`car_eyelids`, `car_rattles`, `car_mouth`, `car_bodyblend`, `car_choblender`, `car_frontend_frontend` and
`car_frontend_mouth`. `DataNodeLinks[slot]` is an index into `ConnectionMapUsedDataNodes`, which leads to the data
node the slot drives.

The game wires a clip player per interface when the character loads (slot numbers become output nodes then;
`BUILTIN_CHANNEL_MAPS` was captured from that moment). Clip values land on **control** nodes (`*_Ctrl`, sliders
such as `Mouth_Ctrl/Mth_Corner_L`). The rest of the network turns them into skin-bone motion:

- **UberConstraints** make skin bones copy their controls.
- **Connection** processors copy eyelid sliders into the eye material's parameters.
- **RadialBasis** processors turn the Eyes control into eyeball and lid offsets.
- the **RelationshipProcessor** turns the mouth sliders into proxy offsets.
- **Spin drivers** turn `Main_Ctrl/RotateFront` / `RotateRear` (degrees) into wheel spin.

McQueen's network has 31 UberConstraint, 30 Connection, 4 ClipProcessor, 3 RadialBasisApplyFloat,
1 RadialBasisPosition, 1 RelationshipProcessor and 1 KinematicStateDecompose processors.

The toolkit's import rebuilds the same chain in Blender: control bones, constraints, drivers, sliders as
custom properties, and Game Look eyes. So a clip imported into Blender moves the model the way the game would,
and an action exported from Blender drives the game's controls with the game's own conventions.

### 5. What this means when you edit

- **Replacing a clip.** Export with the clip you replace as the template. Its `LayerBind`s (templates) and
  name are kept, and its contents are rebuilt from the action. Copy it over the file the alias points to, or
  point the character's alias at a new file.
- **New motion on a new part.** The part needs a template slot. The game only animates slots its templates
  define, so Animate in Clips borrows a free slot; appended slots were tested and are not animated.
- **Layers.** A facial clip should bind `car_facial` (and maybe `car_body`). A body clip that keys the face
  fights the Facial layer, which has higher priority. Additive sets (Rattles, Lean, ChoBlender) add to whatever
  else plays, so their values should be offsets around zero.
- **Cars 3 clips** use the same curve format with different addressing. `cars3_to_cars2` re-addresses them, and
  `clip_slot_map` picks which Cars 2 alias each one should replace.

## Containers

A clip is a Tupperware `.oct` whose interesting part is one `ClipDataBlock` binary. `read_block_native` /
`write_block_native` find the block by its length prefix and replace it, resizing the container when needed.
(A plain search for the block can match up to three bytes early, because blocks start with a zero flags field.)
The `offsetting` executable is an optional fallback backend (`find_offsetting`).

## ClipDataBlock

See [File formats](FileFormats.md#clipdatablock-c2clipblockpy-also-decoded-in-animpy) for the layout. The facts
that matter for Blender:

- **A command is one Bezier curve per channel.** The four values per channel per key are successive deltas of
  the cubic Bezier control points, which is exactly a Blender F-curve segment. Key k's segment runs to key k+1,
  and the *last* key's segment runs to `t_end`: it is part of the clip, not extrapolation.
  - `endValue` is that final segment's P3 and `endSlope` its outgoing slope (checked on all 692 channels of the
    49 shipped facial and frontend clips).
  - Every shipped command uses CLAMP at both ends.
- **Deltas are summed in at least 32 bits.** Running sums reach ±49,986 in shipped data. P0 and every delta must
  fit an i16; intermediate control points need not.
- **Rotations are in radians.** A 180° trick spans exactly π, and a three-turn spinout 6π. An early version read
  an end slope of -2π as -360 and scaled by degrees.
- **The Euler order is stored per sub-op**, as Maya's `rotateOrder` (bits 6–8 of the 0x1000 sub-word). Shipped
  Cars 2 clips use ZXY throughout; converted clips (the Cars 3 bank) also use XYZ. The order is kept per bone on
  import and written back on export.

## Bindings: how a command reaches a bone

Commands don't drive bones directly. They write channel values into a shared buffer in the order the bind
block's command-index list gives. **Binding records** then read that buffer by slot and scatter values onto
output nodes (location / rotation / scale components, or scalars when flag `0x20` is set).

Each bind block has **two record streams**:

- the animated records, right after the command-index list, which `Evaluate` walks;
- the static records at `staticOffset`, which `SetClipData` walks to set the pose.

An earlier version read only the first stream, scattering curves onto the wrong bones. `FIELD_OPS` maps
sub-ops to (property, components).

## Channel maps: from slot numbers to bones

Clips name only their **connection template** (`car_body`, `car_facial`, `car_eyes`, `car_frontend_frontend`,
...), never channel names. Slot numbers are assigned when the engine wires a ClipProcessor to a rig and appear in
no file. `BUILTIN_CHANNEL_MAPS` was captured from the running game with a `DumpOutputNodes` hook that logs each
output node's full scene path (`|Actors|car/McQueen|...|Facial|OutputNodes|Anchor/Jaw_Ctrl/Transform`).
Facial and Eyelids share one 68-channel list, and their `.bent` filters differ.

At character import, `character.refresh_channel_maps()` adds every channel the character's `.bent` names,
including custom ones from rig edits and slots borrowed by Animate in Clips (see
[Rig](RigAndExport.md#edit-kinds-corec2rigeditpy)), so those import and export too.

## Rig conventions

- **Scalars and bones.** A label with a `/` (`Eyes_Ctrl/TOP_L_Bow`, `Mouth_Ctrl/Mth_Corner_L`,
  `Main_Ctrl/RotateFront`) is a scalar slider and lives on the armature as a custom property of that name.
  Everything else is a bone (`bone_candidates`).
- **Stored values are offsets from rest** in the parent's axes. With R the bone's rest rotation relative to its
  parent:

  ```
  basis location = R⁻¹ · stored location
  basis rotation = R⁻¹ · stored rotation · R
  ```

  `stored_to_basis` and `basis_to_stored` are exact inverses. Mouth bones also sit under a slider-driven
  `_Proxy` offset, which import composes in and export takes back out.
- **Quaternion records are stored conjugated**, like the rig's own rotors. Euler records are plain angles.
  Import conjugates, and export writes constant rotations conjugated, so clips round-trip exactly
  (`CLIP_QUATERNION_CONJUGATED`).
- **Eyelids with no keys.** Every lid slider rests at 0, which shuts the eyes. In the game an eyelid clip always
  runs on top, so that never shows. When no clip keys the lids, the import shows a neutral open pose instead
  (`EYELID_DEFAULT`: top lid flat at 45, `BTM_CNTR_Notch` at -84.5, the median of the 71 shipped single-eye
  `select_idle` clips). This only changes what is shown; Export Clip writes keyed sliders only.

## The mouth relationship (Mth_* and vsm_* sliders)

Each mouth bone copies its `X_Ctrl` node, which sits under an `X_Proxy` node. Clips animate the `X_Ctrl` nodes. A
**RelationshipProcessor** drives the `X_Proxy` nodes from 21 sliders: `Mth_Corner_L/R`, `Mth_Expand_L/R`,
`Mth_Offset` and 16 `vsm_*` visemes. Its RelationshipDataBlock, decoded from McQueen, has three parts:

- **section A**: per slider, a linear remap of the value onto time [0, 1/3];
- **section B**: per slider, its channels over that time (663 channels, all zero at neutral);
- **a bind program** that sums the channels into the processor's 22 outputs.

`_SliderProxy` evaluates it per frame, so expressions form in Blender as they do in the game.

## Import (`apply_clip_file`)

1. Find the target armature. If it has no control rig, stop: clips drive controls, not skin bones.
2. `reset_pose()`: return every bone and slider to rest, because Blender keeps whatever the last clip left on
   channels the new action doesn't touch. Bones the user added (without the importer's `oct_rest` mark) keep
   their pose, such as a raised spoiler.
3. Decode the block, resolve each bind record to a bone or slider through the channel maps, convert to bone
   space, and build F-curves that match the Bezier segments exactly. Options: frame rate (clips are authored on
   a 30 fps grid) and setting the scene range.

## Export (`CARS2_OT_export`)

The exporter **rebuilds the whole ClipDataBlock from the action**: commands, bind blocks and constant pool. It
borrows only the container from a *template clip* (normally the clip being replaced, or the one the action was
imported from): its `LayerBind` list (which connection templates it binds) and everything around the block. Any
timing, duration, new bones, static poses and sliders therefore export.

- A **bone is written** if the action has F-curves on it. Within it, a component is written if the original
  clip bound it or if it moves or sits away from rest.
- **Sliders** are written whenever they have F-curves.
- **Constant components** go to the pool as static records. The rest are fitted into commands (curve fitting,
  `_TOL_FLOOR` and `_TOL_RANGE` scaled by the Precision setting).

| Option | Choices |
|---|---|
| Sampling | Keyframes (F-curves directly), or Visual pose (steps the scene, includes IK, constraints and drivers) |
| Keys | Fit to curves (adaptive), or Every frame |
| Frame range | Action, or Scene |
| Precision | High, Normal, or Compact |

**Action API compatibility.** Blender 4.4 introduced slotted actions and 5.x removed `Action.fcurves`. The
helpers in that section work on both layouts (3.x to 5.x).

## Cars 3 clips and cutscenes

**Import Cars 3 Clip** converts in memory through `core/cars3_to_cars2.py` (via `c3anim`) and then imports like
a Cars 2 clip. The curve format is the same in both games. Channel addressing, record layout, one field op and
the header are translated; Cars 3-only channels are dropped and reported. **Convert Cars 3 Clips to Cars 2**
writes converted `.oct` files. `clip_slot_map.py` / `port_car.py` decide which Cars 3 clip fills which Cars 2 clip
slot per car; see `core/CARS3_ANIM_NOTES.md`.

A Cars 3 cutscene (`ccl_*.zip`) gives each actor a rig clip and a placement clip (`*_pn_clip.oct`: location,
Euler rotation, visibility). The import builds empties for the cutscene and each actor's placement, with the
armature under them, as the game does.
