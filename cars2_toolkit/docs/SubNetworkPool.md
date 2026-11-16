# SubNetworkPool: the rig network

The `SubNetworkPool` is the least understood part of a Cars 2 `.oct`, and the part the toolkit edits most.
This page says what it is, how the game uses it, how each field works, and why adding to it is safe when done the
toolkit's way. All examples come from McQueen (`characters/mcqueen/mcqueen.oct`) unless noted. The rules come
from every shipped Arcade rig (`core/schema.json`, `core/ct_catalog.json`) and from in-game tests where noted.

## What it is, in one sentence

A **SubNetwork is a small dataflow program** that the engine runs every frame: data nodes hold values
(transforms, floats, curves) and processor nodes compute new values from them (constraints, clip playback,
interpolators). It is how Octane moves anything that isn't physics.

Without it, a character's mesh would sit on a skeleton that nothing moves. With it:

- clips land on controls (`*_Ctrl`);
- constraints make skin bones follow controls;
- the eye material gets its lid and look values;
- the mouth sliders shape the face;
- the wheels spin from `RotateFront` / `RotateRear`;
- physics hands the car's motion to the rig (`KinematicState`).

The scene tree (`SceneTreeNodePool`) says *what* the character is (bones, meshes); the SubNetwork says *how it
moves*.

## Where it sits

```
<car>.oct
  SubNetworkPool/SubNetwork#0      Name 'Scene', Type 'Scene'    <- the character's rig
  SceneTreeNodePool                Bone / SubGeometryLit / Transform / Geometry / Scene nodes
  AssociationPool                  binds scene-tree nodes and materials to network nodes, BY NAME
  CollisionShapePool, PhysicsBodyPool, DisplayLayerPool, TexturePool, MaterialPool, buffer and stream pools
```

The engine knows two SubNetwork types (`schema.json` → `engine.SubNetwork`):

| Type | Where | What it holds |
|---|---|---|
| `Scene` | every character `.oct` (one, `SubNetwork#0`) | the whole rig |
| `Clip` | every clip `.oct` in `motions/` | one `ClipData` node plus `LayerBind`s (see [Animation](Animation.md#2-the-clip-file)) |

The rest of this page is about the `Scene` network.

## The fields

McQueen's `SubNetwork#0`:

| Field | McQueen | Role |
|---|---|---|
| `Name`, `Type` | `'Scene'`, `'Scene'` | network kind |
| `HeaderStrings` | 336 strings | every name part used in node paths (`Anchor`, `Transform`, `Main_Ctrl` ...) |
| `HeaderStringIndices` | 618 ints | name of header id k+1 = `HeaderStrings[HeaderStringIndices[k]]` |
| `HeaderLayout` | 520 ints | the path tree, as depth-first `(parent id, child count)` pairs |
| `DataNodePool` | 293 nodes | values |
| `ProcessorNodePool` | 71 nodes | operators, **in evaluation order** |
| `ConnectionMapUsedDataNodes` | 115 ints | the data nodes animation can reach |
| `RestPositionIndices` / `RestPositionData` | 460 / 216 bytes | rest values for those nodes |
| `BaseDataNodeLinksPool` | 12 lists | one per connection template: slot → entry in `ConnectionMapUsedDataNodes` |
| `NetworkBuffer` | 12 bytes | shared scratch memory for processors that need it |

### Headers: every node has a path

Nodes are not named directly. Each node's `Header` is an **id** in a tree of names, so it stands for a path.
`HeaderLayout` starts `[0, 1, 1, 197, ...]`: the virtual root 0 has 1 child (id 1 = `Anchor`), and id 1 has 197
children, and so on depth-first. The ids are handed out in that order.

```
Header 203 -> Anchor/Main_CtrlGrp/Transform
Header 381 -> Anchor/UberConstraint_sub0_DefaultAnchor/Body/UberConstraintProcessor
```

`core/c2headers.py` turns ids into paths and back. Paths are also how the **rest of the file finds the network**:
an `AssociationPool` entry `Anchor|Main` (type Bone) needs a transform data node `Anchor/Main/Transform`, and
`Anchor|eyeshader` (type Material) reads float parameters under `Anchor/eyeshader/...`
(`c2validate.validate_associations`). **Renaming a node breaks those bindings silently**, which is one reason the
toolkit addresses everything by path and never renames shipped nodes.

### Data nodes (values)

Every data node has `Header` and `Type`. Most have `Data`, and transforms have `BasisDataNodeRef` (their
parent). McQueen's 293 data nodes:

| Type | Count | Example | Notes |
|---|---|---|---|
| `Dilation` | 110 | `Anchor/controls/Transform` (often `Data {}`) | a transform node; group and pass-through nodes, and what the toolkit adds for new nodes |
| `Float` | 90 | `Anchor/Mouth_Ctrl/vsm_AA = 0.0` | sliders, wheel-spin inputs, **material parameters** (`Anchor/eyeshader/eyeBallOffset_v`) |
| `Transform` | 46 | `Anchor/Main_CtrlGrp/Transform`, `Translation [0, -0.44, 0]` | `Translation`, `Orientation#Rotor` or `#OrientProxy`, `Scale` |
| `UberConstraintData` | 31 | `.../Body/UberConstraintData`, 24-byte block | constraint programs (`c2ubercon`) |
| `FloatArray` | 5 | `Eyes_Ctrl_PB/StandardDeviations` | RBF parameters |
| `ClipData` | 4 | `Anchor/Main_Ctrl/RotateFront/ClipData` | driven-key curves (`c2clipblock`) |
| `Bool` | 3 | `Anchor/Lazy_MouthConstraintGroup = 1` | "lazy" group switches (below) |
| `KinematicState` | 1 | `Anchor/KinematicState` | the car's physics state, written by the game |
| `RelationshipData` | 1 | `Mouth_Ctrl/Mth_Offset/RelationshipData`, 26 KB | the mouth slider program |
| `PositionArray` | 1 | `Eyes_Ctrl_PB/RadialBasisPosition_ControllerKeys` | 3×3 key grid, -1 to 1 |
| `Position` | 1 | `Anchor/Eyes_Ctrl/Transform.Translation` | a view of one component of a transform |

`Static 1` marks nodes whose value never changes at run time (data blocks, RBF tables). `Length` gives the
element count of array nodes. The engine registers more data types than characters use
(`AnimCurve`, `EulerXYZ`, `EulerZXY`, `Matrix`, `Rotor`, `Scale`, `Vector`, `EventList` ...); `c2validate` warns
on any a shipped rig never uses.

**Rotations are stored as rotors** `[x, y, z, w]`, conjugated relative to the rotation they apply. Transforms
are local to their basis node, and the chain of `BasisDataNodeRef` gives the world rest (`plan_control_rig`
walks it).

### Processor nodes (operators)

A processor names its inputs and outputs by data-node index, in one of two styles:

- `DataNodeRefs` plus `DataNodeAttributes`, a parallel list of roles. For example `Connection`:
  `[Eyes_Ctrl/BTM_CNTR_Notch, eyeshader/eyeLidBottom_notchPos_v]` with roles `['Input', 'Output']`.
- Named fields: `Input`, `Output`, and type-specific references (`UberConstraintDataInput`, `ClipData`,
  `RelationshipData`, `LazyDataNode`).

McQueen's 71 processors:

| Type | Count | What it does (McQueen example) |
|---|---|---|
| `UberConstraintProcessor` | 31 | `Body/Transform` copies `Body_Final_Proxy/Transform`, using the ops in its UberConstraintData; this is how skin bones follow controls |
| `Connection` | 30 | copies a value: eyelid slider → eye material parameter |
| `ClipProcessor` | 4 | plays a ClipData curve **driven by a Float** (a driven key): `Main_Ctrl/RotateFront` → `TireFLeft_SpinJoint`, `TireFRight_SpinJoint` |
| `RadialBasisApplyFloat` | 3 | RBF weights → `eyeshader/eyeBallOffset_v` (and u, lid offset) |
| `RadialBasisPosition` | 1 | Eyes control position + keys + deviations → RBF weights |
| `RelationshipProcessor` | 1 | 21 mouth sliders → 22 `*_Proxy` transforms, gated by `Lazy_MouthConstraintGroup` |
| `KinematicStateDecompose` | 1 | `Anchor/KinematicState` → `Anchor/Transform`: puts the physics body's motion into the rig |

The engine registers more processor types (`DynamicChain`, `IKFKConstraint`, `RadialBasisOrientation`,
`AnimCurve3EulerZXYToRotor`, `EventList`, `FrameSelector` ...). `DynamicChain` is used by Mater's tow cable and
by the toolkit's Make Dynamic Chain.

**Pool order is evaluation order.** A processor that reads a node another processor writes must come after it,
and **a data node may have only one writer**. `c2validate` checks both (the first as a warning, the second as an
error), and `c2rigedit._order_new_processors` places new processors correctly.

Processors that need scratch memory list `NetworkBufferNames` / `NetworkBufferOffsets` (34 of McQueen's do,
every UberConstraint among them) into the shared `NetworkBuffer`.

**Lazy groups.** `LazyDataNode` points at a `Bool` group node (`Lazy_MouthConstraintGroup` is used by 21
processors, `Lazy_EyeConstraintGroup` by 5). Bone associations in `AssociationPool` can name a group in
`LazyDataNodeName`. From how they are used, these group processors so the engine can evaluate them only when
needed. The toolkit passes `lazy=` when it adds constraints to those groups and checks that every named group
exists, but it never changes how they evaluate.

### Animation access: connection maps, links and rest positions

This is the part that connects the network to clips and the `.bent`:

1. **`ConnectionMapUsedDataNodes`**: a list of data-node indices (115 on McQueen) that any animation layer can
   drive. Entry 0 is `Anchor/KinematicState` (index 3).
2. **`BaseDataNodeLinksPool`**: one entry per connection template, named like the `.bent` interface's
   `ConnectionMap`:

   ```
   BaseDataNodeLinks#0  Name 'Anchor-car_body'  ConnectionTemplate 'characters/car/car_body.ct'  DataNodeLinks [-1, 64, 65, 66, 7, ...]
   ```

   `DataNodeLinks[slot]` is an index into `ConnectionMapUsedDataNodes`, so slot 1 of `car_body` (`BackTires`)
   reaches `Anchor/BackTires_Ctrl/Transform`. **-1 means the slot exists in the template but this character
   has nothing there**; McQueen has no `BackEnd` control. McQueen has 12 link lists: `car_body` (37 slots),
   `car_eyes` (4), `car_choreography` (113), `car_leanclipset`, `car_facial`, `car_eyelids`, `car_rattles`,
   `car_mouth`, `car_bodyblend`, `car_choblender`, `car_frontend_frontend` and `car_frontend_mouth`.
3. **`RestPositionIndices`** has two u16 per entry of `ConnectionMapUsedDataNodes`: an offset and a float count
   into `RestPositionData`, or `(0xFFFF, 0xFFFF)` for none. For example `(0, 3)` is 3 floats at offset 0: a
   rest translation. These give blended layers a value to return to.

The slot numbers are the **only** thing clips use (see [Animation](Animation.md)). A template exists only where
three files agree: this link list, the `.bent` `BlenderChannelMap` / `Filter`, and the clips' `LayerBind`s. No
`.ct` file exists in the game.

## How a frame flows through McQueen's network

1. Physics writes `Anchor/KinematicState`. `KinematicStateDecompose` turns it into `Anchor/Transform`, the
   car's root.
2. Each animation interface (from the `.bent`) plays its clips and writes the slots of its template, through
   `BaseDataNodeLinks` → `ConnectionMapUsedDataNodes`, into control nodes and sliders. Layers that share a node
   are blended (the `.bent` `Blenders`).
3. Processors run in pool order:
   - driven keys turn `RotateFront` / `RotateRear` into wheel spin;
   - the RelationshipProcessor turns mouth sliders into `*_Proxy` offsets;
   - Connections copy eyelid sliders into `eyeshader/*` parameters;
   - the RBF turns the Eyes control into eyeball and lid offsets;
   - UberConstraints make each skin bone copy its final control.
4. The renderer reads the skin bones (scene-tree `Bone`s bound by the `Bone` associations) to skin the mesh.
   It reads `eyeshader/*` floats (bound by the `Material` association) to draw `renderedEyeTexture`.

The toolkit's import rebuilds the same flow in Blender (see [Import](Import.md)): control bones,
UberConstraints as Blender constraints, driven keys as drivers, sliders as custom properties, the RBF and
relationship evaluated in Python. That is why clips play the same way in Blender as in the game.

## Why it can be modified and added to

Hand-editing a SubNetwork almost always breaks it. The reasons are numeric, not semantic:

- Every reference is an **index**: node indices in processors, `BasisDataNodeRef`, links into
  `ConnectionMapUsedDataNodes`, header ids, buffer offsets.
- Opaque binary blocks (UberConstraint, ClipData, Relationship) hold indices of their own, which have to stay
  consistent with the processor that owns them.
- **Order carries meaning**: evaluation order, parents before children, depth-first header ids.
- Rest tables and the network buffer are **packed** to match the lists they index.

Insert one node in the middle and half the numbers in the file point at the wrong thing. The game won't
say why: the character fails to load, a part flies off, or the face goes dead.

The toolkit makes edits safe by never touching a number by hand:

1. **Paths instead of numbers.** `c2scene.SceneNetwork` loads the network into nodes addressed by path. Edits
   name paths (`Anchor/Spoiler/Transform`, parent `Anchor/Body_Ctrl/Transform`).
2. **Opaque blocks are safe to renumber around.** The UberConstraint and ClipData blocks index their own
   processor's `Input` / `Output` lists, never global data-node numbers. Identical RelationshipData blocks ship
   in different characters with different global numbering, which shows the same holds for them. So
   `add_data` can append a node, and `insert_data` can place a node before its users (shipped pools list
   parents first) and shift every reference to match: basis refs, all processor reference fields, template
   links, the connection map and rest data.
3. **Everything numeric is regenerated** on `build()`:
   - header strings, ids and layout;
   - `ConnectionMapUsedDataNodes` and every `DataNodeLinks` index;
   - rest entries for newly linked transforms;
   - `NetworkBuffer` and offsets for new buffered processors.
4. **Codecs for the opaque blocks.** `c2ubercon` and `c2clipblock` write new constraint and driven-key blocks
   byte-exactly in the game's layout. Decoding and re-encoding every shipped block reproduces it.
5. **Round-trip proof.** An unedited network rebuilds **byte-identically** on every shipped Arcade scene, so the
   rebuild itself adds nothing.
6. **Conventions enforced.** `c2validate` checks a new network against what every shipped rig obeys:
   - registered types and their required fields;
   - references in range;
   - one writer per node, readers after writers;
   - constraint ops matching inputs;
   - slot kinds matching their template (`ct_catalog.json`, mined from all shipped rigs);
   - rest and buffer tables matching their lists.

   Validation also covers associations against the scene tree, and the `.bent` against the network
   (`c2channels.validate_pair`).
7. **The engine is generic.** It evaluates whatever registered node types it finds, in pool order. A new
   Transform, Float, UberConstraint, ClipProcessor or DynamicChain that follows the rules is evaluated like a
   shipped one. No game code change is needed.

### What has been added successfully

These edits were recorded in `cars2_edits` and applied by `c2rigedit`:

| Addition | Network change |
|---|---|
| New bones and parts | `Dilation` data nodes under an existing node (Add Selected Bones to Rig) |
| New FX markers | `Transform` nodes named `FX_Marker_<Name>`, linked to the template slot Cars 2 already has for that name; the game finds markers through that slot |
| Moved markers and controls | new values in a node's own `Translation` / `Rotor` / `Scale` fields |
| Float controls | `Float` data nodes (Add Float Control) |
| Constraints | `UberConstraintProcessor` plus `UberConstraintData` |
| Driven keys | `ClipProcessor` plus `ClipData`, driven by a Float |
| Dynamic chains | `DynamicChain`, built like Mater's tow cable |
| Clip-animated parts | a borrowed template slot: a group plus a control node linked into that slot's `BaseDataNodeLinks`, with the part constrained to it |
| New skin bones | scene-tree `Bone`s for mesh export, whose rig node must exist first |

### Limits found in-game

- **Template slots appended past a template's end are not animated.** The game only animates the slots its
  templates define, which is why new parts *borrow* a free slot (-1 entries and unused slots) instead of
  appending one. Float channels past the template are recorded, but they are not animated in-game yet.
- **A `Scale` added to a marker that had none is ignored** (tested on Sheriff's exhaust), so the toolkit doesn't
  write one (`ADD_MISSING_SCALE = False`).
- **Scene-tree component order matters.** Moving the `CarEyes` component breaks the eyes, even when the
  ComponentLink association is renumbered (see [Geometry](Geometry.md)).
- **The template's identity is its name.** The `.ct` paths in `ConnectionTemplate` must match the
  `.bent` and the clips exactly, even though no such file exists.

## Working with it from Python

```python
import sys; sys.path.insert(0, 'cars2_toolkit/core')
from c2scene import SceneNetwork
import c2validate

net = SceneNetwork.load('mcqueen.oct')
print(net.path_of(net.index('Anchor/Body_Ctrl/Transform')))
net.add_data('Anchor/Main_Ctrl/MyDial', 'Float', data=0.0)        # a new slider
net.connect('Anchor/Main_Ctrl/MyDial', 'Anchor/Main_Ctrl/RotateFront')
sn = net.build()                                                   # every index regenerated
errors, warnings = c2validate.validate_subnetwork(sn)
net.save('mcqueen_edited.oct')
```

To learn a rig, decode it and read it by path. `plan_control_rig` in `c2_model.py` is a worked example that
walks bases, constraints, driven keys, the RBF and the relationship. `python core/c2validate.py file.oct` prints
any rule a file breaks.

## Open questions

These have not been worked out yet:

- the exact runtime meaning of `MotionIndex` and `Priority` on `.bent` interfaces, and `PostBehavior` on clip
  `Control` blocks (kept as shipped);
- how lazy groups are scheduled internally (kept as shipped; new processors join existing groups);
- the processor types no character uses (`IKFKConstraint`, `FrameSelector`, `EventList` ...). They are
  registered in the engine but untested in edits, so `c2validate` warns on them.
