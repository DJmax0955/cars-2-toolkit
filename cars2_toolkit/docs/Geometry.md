# Geometry export

`geometry.py` reads the scene; `core/c2geom.py` writes the game data. Export Character runs them when
*Include meshes* is on.

## The game's layout (`c2geom` docstring, verified on every Arcade character)

- `VertexBufferPool` / `IndexBufferPool` hold one `Static` buffer each, stored in `<name>_0.vbuf` and
  `<name>_0.ibuf`. Indices are u16, so a vertex pool holds at most **65,535** vertices (`MAX_VERTS`).
- `VertexStream`: a typed slice of the `.vbuf` (`Length` vertices of `Width` bytes at `VertexBufferOffset`,
  `Elements` = name, type, offset), packed back to back. A mesh usually has two streams: `[Position, Uv1]` and
  `[Tangent, Normal, Binormal]`.
- `IndexStream`: `Length` u16 indices at `IndexBufferOffset`, as a triangle list.
- `SubGeometryLit`: one mesh, made of a MaterialReference, stream references, an index stream, and
  `SceneComponentReferences` that lead to its `SkinRemap`.
- `SkinRemap` is **the mesh's bone palette**. Vertex `BoneIndices` are palette slots, and `RemapData[slot]` is a
  BoneID. Palettes belong to the mesh: meshes sharing a vertex pool can use different palettes. The largest
  shipped palette has 42 bones, which is `PALETTE_LIMIT`.
- `Bone`: a scene-tree node with `BoneID`, `BoneCount` (only the skeleton root has it), and `BoneToModelMatrix`
  (row-vector layout).
- Weights are 4 normalised bytes that sum to exactly 255 (`quantize_weights`).
- Element type ids (as stored in the files): 1 float2, 2 float3, 3 float4, 24/25 u8×4,
  31 S10_10_10N, 39 S11_11_10N, 44 half2, 45 half4, 46 S1_6_3 (`vertexfmt.py`).

## Import side: keeping the originals (`geometry.tag_import`, `store_originals`)

At import:

- `tag_import` sets `mat['cars2_material']` (the MaterialPool index) and stores `cars2_mesh_nodes`
  (`{material index: [scene-tree node indices]}`) on the armature.
- `store_originals` copies each vertex's exact game Position / Normal / Tangent / Binormal / Uv1 into hidden
  point attributes (`cars2_pos`, `cars2_normal`, `cars2_tangent`, `cars2_binormal`, `cars2_uv`). This only works
  because the importer builds each object's vertices in the same order as `c2geom.extract_meshes`.

## Export side (`geometry.collect` → `c2geom.rebuild`)

`collect(arm, tree, problems, warnings)` goes through every mesh parented to the armature or deformed by it
(`character_meshes`), splits each one by material slot, and builds `c2geom.rebuild` input:

- **Material.** Every face must use one of the character's imported materials (one that has `cars2_material`).
  New materials are not supported, so a face with any other material is a problem.
- **Template node.** Each mesh borrows an existing `SubGeometry` node of the same material for its stream layout,
  RenderCaps and flags. An imported object (named after its material, with that material first) reuses its own
  scene-tree slot (`node`); other objects get new slots. Every mesh gets the Tangent / Normal / Binormal stream
  layout of a character mesh that has one.
- **Vertices.** Positions go into armature (game) space. If a vertex still matches its stored original
  (`_original`), **the game's own data is written bit for bit**. Otherwise normals come from Blender's corner
  normals, tangents from `calc_tangents` (which needs a UV map), the binormal from the bitangent sign, and v is
  flipped. Vertices are split per unique UV, normal and tangent.
- **Weights.** Vertex groups that name skin bones or rig nodes (`_weights`). An unweighted vertex rides on
  `Body` (or `Main`), with a warning, because the game needs a bone for every vertex.
- **New skin bones.** A weighted bone that is not a skin bone becomes a new one. It must already be in the game
  rig (Add Selected Bones to Rig), because the game moves a skin bone through its rig node. Its matrix is
  `bone.matrix_local` in row-vector layout, and its parent is the nearest skin bone above it.
- **Modifiers.** Any modifier other than Armature is reported as ignored ("apply them first").
  `mesh_with_modifiers`, which evaluates every modifier except Armature, is used by the Cars 3 export
  (`cars3_export._object_parts`).

`c2geom.rebuild(tree, meshes, new_bones)` then does the following:

1. Creates the new bones (a BoneToModelMatrix and a bone-local bounding box).
2. Checks that mesh nodes sit at identity (true for every shipped character), since vertices are written in
   model space.
3. **Groups meshes into vertex pools by layout**, at most 65,535 vertices each, following the base character's
   own habit. McQueen and Mater share one stream pair per format across mesh nodes. Sheriff and Victor Hugo give
   every mesh node its own pair, in node order.
4. **Builds palettes** per mesh. A mesh over 42 bones is an error ("split it by material"). Every scene
   component keeps its original slot: `CarEyes` (the eye renderer) must stay where it was (Mater #8, McQueen
   #1, Guido #2 ...); in-game tests showed that moving it breaks the eyes even with the ComponentLink renumbered.
   Rebuilt palettes reuse the original SkinRemap slots in order and only append past the end.
5. Encodes the streams (`encode_stream`) and indices into one rebuilt buffer, rewrites the pools, and drops nodes
   that are no longer used.

`c2geom.write(tree, vbuf, ibuf, oct_path)` saves `<car>.oct` with `<car>_0.vbuf` / `_0.ibuf` next to it.
`roundtrip_check` verifies that a decode followed by an encode of the original buffers reproduces them.

## Limits

- No new materials or textures from mesh export. Material *settings* and textures are handled by the material
  system; see [Materials](Materials.md).
- No mesh nodes under transformed scene nodes. No shipped character has these, so export refuses them rather
  than guess.
- Bone scale in rest poses is ignored by the game rig, and the user gets a warning.
