# File formats

All of these were checked against the Cars 2 Arcade data. Each codec's module docstring has the full byte
layout; this page explains how the pieces fit together.

## A character package

```
<car>.bent                            animation interfaces, clip sets, channel blenders (package root)
characters/<car>/<car>.oct            the character: textures, materials, scene tree, rig network
characters/<car>/<car>_0.vbuf         vertex data (one 'Static' buffer)
characters/<car>/<car>_0.ibuf         index data (u16)
characters/<car>/motions/*.oct        clips (shared ones live in characters/car/motions/shared_*)
effects/<effect>.tfx                  effects the package ships itself (the game's own are in assets/effects/effects)
```

The game ships these inside zips (`assets/characters/<car>.zip`, etc.); the toolkit works on extracted copies.
`rigedit.export()` writes the same layout into the folder you choose. `c2rigedit.find_bent()` finds a
character's `.bent` by searching up to three folders above the `.oct` for a `.bent` whose `SceneFilename` names
it. A few characters (McQueen Fabulous) split their geometry across several buffer files (`_0`, `_1`);
`c2geom.buffer_sets()` handles that.

## Tupperware (`.oct`, `.bent`, `.mer`, `.tfx`): `core/c2oct.py`

A typed tree with a string table. Node values are containers, strings, int32, float32, typed arrays or binary
blobs. Each node's header stores its *nesting level*, and the tree is rebuilt from those levels. `c2oct` keeps
every value's exact type (int versus float, empty typed arrays) and the game encoder's choices (byte widths of
lengths and ints), so **decode → encode is byte-identical** on every shipped Cars 2 file. The `offsetting oct`
JSON route is not byte-identical, which is why `c2oct` exists. `octread.py` is a simpler read-only reader with the
same JSON shape as `offsetting oct decode`.

In Python, a tree is an ordered `dict`. Keys are `'Key'` or `'Key#Name'`. A key repeated inside one container
gets a `~1`, `~2` suffix, which is dropped on encode. Pools are dicts such as
`{'Texture#0': {...}, 'Texture#1': {...}}`; their order is their index.

### What is in a character `.oct`

| Pool | Content |
|---|---|
| `TexturePool` | `Texture#i`: name, `Data` = a complete DDS file (so replacing a texture means swapping the blob) |
| `MaterialPool` | `Effect` (the `.tfx` name), `RenderCaps`, parameter values, `TextureEntries` (slot usage → TexturePool index) |
| `SceneTreeNodePool` | scene-tree nodes: `Bone` (BoneID, BoneToModelMatrix), `SubGeometryLit` (one mesh), `SkinRemap`, ... |
| `VertexBufferPool` / `IndexBufferPool` | one `Static` buffer each, stored in the `_0.vbuf` / `_0.ibuf` files |
| `VertexStream` / `IndexStream` | typed slices of the buffers (see [Geometry](Geometry.md)) |
| `SubNetworkPool/SubNetwork#0` | the rig: a `Type 'Scene'` network (below) |

## The Scene SubNetwork (the rig): `c2headers`, `c2scene`, `c2validate`

The rig is a dataflow network:

- **DataNodePool**: values (`Transform`, `Float`, `Vector`, `Position`, ...). A transform has a
  `BasisDataNodeRef` (its parent) and `Data` (`Translation`, `Orientation#Rotor` or `#OrientProxy`, `Scale`).
- **ProcessorNodePool**: operators that read data nodes (`DataNodeRefs` / `Input`) and write others (`Output`):
  `UberConstraint`, `ClipProcessor` (clips and driven keys), `RelationshipProcessor` (the mouth sliders), ...
  **Pool order is evaluation order**, and shipped pools list parents before children.
- **Headers**: every node's `Header` is an id in a path namespace (`HeaderStrings`, `HeaderStringIndices`,
  `HeaderLayout` as depth-first `(parent, child_count)` pairs). An id therefore stands for a path such as
  `Anchor/Main_Ctrl/RotateFront/ClipProcessor`. `c2headers.py` converts between the two.
- **Connection templates**: `BaseDataNodeLinks` map a template's slot numbers (for example `car_body` slot 7 =
  `Body`) to data nodes through `ConnectionMapUsedDataNodes`. Clips address channels by slot, never by name.
- **Rest positions**: `RestPositionIndices` / `RestPositionData` for linked transforms.
- **NetworkBuffer** / `NetworkBufferOffsets`: the opaque per-processor data blocks (constraint ops, clip
  blocks).

`c2scene.SceneNetwork` addresses everything by **path** and regenerates all the numbers on `build()`. New data
nodes are appended, or inserted ahead of their users with every reference shifted. That is safe because the
opaque blocks index their own processor's lists, not global node numbers (see [SubNetworkPool](SubNetworkPool.md)).
An unedited network rebuilds byte-identically.

`c2validate.validate_subnetwork()` encodes what every shipped rig obeys. **Errors** would certainly break loading
or evaluation:

- unknown SubNetwork or node type;
- broken header tree, duplicate paths, two nodes sharing a header;
- missing fields that every shipped node of that type has;
- out-of-range references, a basis that is not a transform;
- two processors writing one data node;
- unreadable constraint or driven-key blocks, ops reading inputs the processor lacks, constraints on
  non-transforms;
- driven keys driven by a non-Float;
- clip binds pointing at missing commands or outputs, wrong record kinds, operands past the buffer or pool;
- a template slot bound to the wrong channel or node type;
- bad rest-position tables, buffer offsets past the buffer.

**Warnings** flag things that are legal but never seen in shipped rigs, such as a node read before it is
written, a type no shipped rig uses, or a basis later in the pool. `validate_associations()` checks the scene
tree's associations against the network. `catalog` / `schema` come from `core/ct_catalog.json` and
`core/schema.json`, which were mined from all shipped rigs.

## `.bent`: interfaces and channels (`c2channels.py`)

The `.bent` holds a character's animation interfaces (Body, Facial, Eyelids, Eyes ...). Each interface has a
`ConnectionTemplate` and a `BlenderChannelMap` (one u16 per slot: `blender << 6 | use#`). It also holds
`Blenders` (per-channel name, type, owner and flags), `Filter '<ClipSet>-<template>'` (one weight per slot) and
the **ClipSets** (`Clip#BRAKE_STOP` → `characters/car/motions/shared_brake_stop`).

The game has no `.ct` files and the executable holds no template data, so a template exists only as a slot index
space that three places agree on: the `.bent` channel map, the scene's `BaseDataNodeLinks` and the clips'
`LayerBind` records. **To add a channel, append the same slot index in all three**, which is what `c2channels` does
(`bent_invariants()` and `validate_pair()` check it).

## UberConstraintDataBlock (`c2ubercon.py`)

A list of ops: `point`, `orient`, `scale`, `point_offset`, `orient_offset` and `aim` (vector or object up). Each
op reads one of the processor's inputs. Repeating an op with inputs 0, 1, ... gives a multi-target (averaged)
constraint, as used for Guido's sleeves and Finn's cheeks. Skin bones copy their control's world transform
through these constraints.

## ClipDataBlock (`c2clipblock.py`; also decoded in `anim.py`)

Used both by animation clips and by rig driven keys:

- **Commands** are Bezier curves, one per channel. Values are `bias + scale * P`, where P is a running sum of
  i16 deltas (opcode 0), or stored as floats (opcodes 2, 4 and 6).
- **Bind blocks** scatter the command output buffer onto output nodes. Each bind block has two record streams:
  animated (walked by `Evaluate`) and static (walked by `SetClipData`, which sets the rest pose).
- A **constant pool** of floats holds static values.

See [Animation](Animation.md) for the details that matter when converting to Blender curves.

## Effects (`.tfx`)

An effect is a Tupperware file holding compiled D3D9 shaders (vs_3_0 / ps_3_0, with D3DX debug info that names
every variable), the parameter table and sampler bindings. `d3dbc.py` decodes the bytecode, `fxglsl.py` turns it
into GLSL for the Game Look renderer, and `fxgraph.py` executes it symbolically for the Blender-node
translation. The game's effects are in `assets/effects/effects/*.tfx`; a character package can ship its own in
`effects/`. Effect names encode their features; the Scene Editor's `docs/EffectNameGrammar.md` decodes them.

## DDS (`dds.py`)

Texture `Data` blobs are whole DDS files. The Arcade engine keeps its sRGB flag in `caps3` (byte 116). New
textures are written uncompressed A8R8G8B8 with box-filtered mips (2D and cube), since there is no DXT encoder.
Several cube maps are RGBM-encoded (`rgb * a * 6`, then squared in the shader).
