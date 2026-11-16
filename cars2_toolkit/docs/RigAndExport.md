# Rig editing and export

## The model: record edits, apply them to the originals

The user works the normal Blender way: add and parent bones, add Copy Location / Rotation / Scale / Transforms
constraints, add a driver with a keyed curve. Then they press the matching button in the **Cars 2 Rig** panel
(`rig.py`). Each press **records an edit**, a plain dict added to the JSON list in `arm['cars2_edits']`
(`rig.add_edit`, `character.get_edits` / `set_edits`).

Nothing touches game files until Export. Export reloads the *original* `.oct` / `.bent` (paths in `cars2_scene`
/ `cars2_bent`), applies the edits in order (`core/c2rigedit.apply`), rebuilds every cross-referenced number
(`c2scene.SceneNetwork.build`) and validates the result (`c2validate`, `c2channels`).

This design keeps three things working:

- An untouched character exports byte-identical.
- `.blend` files survive add-on updates. Edits are data, so a newer exporter re-applies them.
- `core/c2rigedit` can be scripted without Blender:

  ```python
  import sys; sys.path.insert(0, 'cars2_toolkit/core')
  import c2rigedit
  written, log, errors, warnings = c2rigedit.export(scene_oct, bent, edits, out_dir)
  ```

## Edit kinds (`core/c2rigedit.py`)

| Edit | Recorded by | Applied as |
|---|---|---|
| `add_node {path, parent, translation, rotor}` | **Add Selected Bones to Rig** (`CARS2TK_OT_add_bones`) | A `Dilation` data node under `parent`, inserted parent-first. A path `Anchor/FX_Marker_X...` becomes an FX marker instead: a `Transform` named `FX_Marker_X`, linked to its clip-template slot when Cars 2 has one, because that is how the game finds a marker |
| `add_float {path, value}` | (no button; older .blend edits) | A `Float` data node (a slider), e.g. `Main_Ctrl/WingAngle` |
| `channel {path, templates}` | **Animate in Clips** | Transforms: the game only animates slots its templates define (tested in-game), so the part **borrows a free slot**: a group plus a control named after the borrowed channel are added, the bone copies the control, and `net._borrowed` records it for the animation maps. Floats: `bent.add_channel` plus `channels.extend_scene` grow the template in `.bent` and scene together |
| `chain {...}` | **Make Dynamic Chain** | `c3toc2.add_dynamic_chain`, built like Mater's tow cable; the game swings it from the car's motion |
| `constraint {owner, targets, ops}` | **Convert Constraints** | An UberConstraint (`point` / `orient` / `scale`, one input per target; several targets average) |
| `driven_key {driver, outputs, points}` | (no button; older .blend edits) | A ClipProcessor with a driven-key ClipDataBlock (`c2clipblock` builder) reading a Float |
| `node_transform {path, translation, rotor / llp_rotor, scale}` | run on Validate and Export | New values written into an existing node's own fields |

Supported in Blender: constraints with no offsets, mix modes or partial influence, and drivers with a single
variable on a float control and a keyed curve. Anything else is reported as skipped with the reason.

### Moved rig bones (`rig.record_moved`)

FX markers, `Body_Final_Proxy`, controls and similar bones count as moved when:

- their rest was moved or rotated in Edit Mode; or
- they are moved or rotated in Pose Mode on their own channels, except for constrained bones (the game drives
  those) and channels keyed by the active clip; or
- they are scaled in Pose Mode (only markers that already have a Scale).

All `node_transform` edits are recomputed from the scene each time, against the unedited source network
(`SceneNetwork.load(cars2_scene)`). Pose Mode moves are kept once recorded, even if the pose is later reset.

- `ADD_MISSING_ROTATION = True`: a rotated node with no rotation field gets `Orientation#Rotor`, as the same
  markers carry on other characters.
- `ADD_MISSING_SCALE = False`: in-game tests (Sheriff's exhaust) showed the game ignores a Scale on a marker
  that didn't have one.

### New bones

`user_added_bones()` treats a bone as user-added if it has an `add_node` edit or lacks the importer's mark (a
Shift+D duplicate copies the mark, so the edit is what counts). `bake_pose_to_rest` (no button)
 makes a new bone's pose its rest and moves the weighted vertices with it. A new bone that
carries weights becomes a new skin bone on mesh export, so it must be in the rig first (see
[Geometry](Geometry.md)).

## Export (`CARS2TK_OT_export_character`)

1. `record_moved(arm)`, then `rig_summary(arm)` for the report.
2. If *Include meshes* is on: `geometry.prepare(arm, problems, warnings)`. Any problem cancels the export.
3. **Materials:** `charmat.missing()` lists every texture slot the effect samples that has no texture. If any
   are missing the export is refused, with the full list printed to the console. Otherwise
   `mats = {'apply': charmat.exporter(scene, arm), 'car': stem}`.
4. `rigedit.export(scene, bent, edits, out, geom, mats)`, which runs `build_with_geometry()`:
   - `build()`: load, `apply()` the edits, `net.build()`, `validate_subnetwork`, `validate_associations`, and
     the `.bent` invariants plus `validate_pair` (scene ↔ `.bent`);
   - if there are meshes, `c2geom.rebuild(tree, meshes, new_bones)` and `c3toc2.link_chains_lazy`, then
     validate again;
   - if there are materials, `materials['apply'](tree)` writes the MaterialPool and TexturePool and returns the
     `.tfx` bytes of newly picked effects.
5. Writes one flat folder: `<car>.oct` + `<car>_0.vbuf` / `<car>_0.ibuf` (rebuilt with meshes, else the
   character's own buffer files copied); `<car>.bent` only when it differs from the original. No `effects/` - the
   returned `.tfx` bytes are dropped (characters use the stock effects).
6. Writes the report to the `Cars2RigReport` text block (files written, rig changes, notes, steps) and shows a
   popup.

Any error stops the export before anything is written. Warnings are reported, but the export goes ahead.

## Validate (`CARS2TK_OT_validate`)

Runs the same build and validation without writing, and shows the errors and warnings
(see [File formats](FileFormats.md#the-scene-subnetwork-the-rig-c2headers-c2scene-c2validate)).

## How new things get their numbers (`core/c2scene.py`)

`SceneNetwork` addresses nodes by path and regenerates all of these on `build()`:

- header ids and string tables;
- `ConnectionMapUsedDataNodes` and link indices;
- rest-position tables for newly linked transforms;
- `NetworkBuffer` / offsets for new buffered processors.

New data nodes are inserted ahead of their users with every reference shifted. That is safe because the opaque
blocks index their own processor's lists (see [SubNetworkPool](SubNetworkPool.md)). New
processors are put in a valid evaluation order (`_order_new_processors`), because pool order is evaluation order.

```python
net = SceneNetwork.load('mcqueen.oct')
net.add_data('Anchor/Main_Ctrl/MyDial', 'Float', data=0.0)
net.connect('Anchor/Main_Ctrl/MyDial', 'Anchor/Main_Ctrl/RotateFront')
net.save('mcqueen.oct')
```

## Adding a new edit kind

1. Record it in `rig.py`: an operator that builds the dict and calls `add_edit` (pass `replace_key` to replace
   an older edit for the same target).
2. Apply it in `c2rigedit.apply()` using `SceneNetwork` methods. Never write raw indices.
3. If the result can break the game, make sure `c2validate` catches it.
4. Describe it in `rig_summary()` and `short()` so the user sees it in the edit list.
5. Test it outside Blender: build, validate, and check that a no-op edit list stays byte-identical.
