# Materials and Game Look

A character's materials are **game materials** of the shared material system, the same one the Cars 2
Scene Editor uses for worlds. The system lives in `look/`, a copy of the Scene Editor's modules. Its full design
(renderer, shader conversion, Blender material modes, swap rules, undo handling, performance) is documented in
the Scene Editor's
[`docs/SharedMaterialSystem.md`](../../../cars2-scene-editor/cars2_scene_editor/docs/SharedMaterialSystem.md).
This page covers what is specific to characters: `charmat.py`.

## Data model

| Data | Where | Content |
|---|---|---|
| `Material.c2` | `look/props.py` | the MaterialPool entry: `effect`, `index` (pool index), `render_caps`, parameters (`props`), texture slots (`textures`: usage → texture index), `owner` |
| `mat.c2.owner` | | the character's armature: the material's textures come from the owner's table, not the world's |
| `Object.c2tex` | the armature | the character's own TexturePool (C2Texture items: name, image, DDS info, replacement / new flags); slot indices point into it |
| `arm['c2_assets']` | | the game assets folder found at import (holds `effects/effects`) |
| `arm['c2_effects_dir']` | | the character package's own `effects/` folder (effects it ships), or `''` |

`fxnodes.material_context(scene, mat)` resolves the context a material draws in. If the material has an owner,
that is the owner's context (`owner_context`, cached by armature pointer, table length and `c2tex_version`:
assets, local effects, texture images). Otherwise it is the world's.

## Import: `charmat.setup()`

Called by `character.import_character` (and by **Rebuild Character Materials**):

1. Records `c2_source`, `c2_stem`, `c2_effects_dir` (`package_effects`: an `effects/` folder with `.tfx` files,
   up to 3 folders above the `.oct`) and `c2_assets` (`find_assets`, below). If the scene has no Game Assets
   Folder, the one found is set as the scene's.
2. `matio.fill_texture_table(arm.c2tex, TexturePool, folder)` loads every texture of the character into the
   table. **All** of them, cube maps such as `chrome.dds` and `errorsky.dds` included, so the texture picker can
   offer every one.
3. For each MaterialPool entry: `matio.fill_material_props` fills `mat.c2`, sets `owner`, and
   `fxnodes.ensure_effect_entries` adds any parameter or slot the effect has but the file left out (characters
   omit `environmentScrollMap` in 90 materials and `renderedEyeTexture` in 44). Then `matnodes.build` builds the
   Blender material.
4. If no Scene Editor world is loaded, the render engine is set to Game Look.

`fxnodes.SUSPEND` is raised during setup so update callbacks don't rebuild material by material.

### Finding the game assets folder (`find_assets`)

The search order is:

1. the scene's **Game Assets Folder**, if it holds effects;
2. any folder above the character's file that holds `effects/effects`, or whose `assets/` does;
3. the last folder that worked, remembered in `<Blender config>/cars2_scene_editor/assets_dir.txt`
   (`remember_assets`);
4. `~/Documents/My Games/Cars 2 Arcade Essentials/assets` (the standard install).

Step 3 is what finds the folder for characters kept outside the game install, such as an extracted Finn in a
work folder. Tests must not preset `scene.c2.assets_dir`, because that hides bugs in the search.

## Game Look (Rendered view)

The **Cars 2 Game** render engine (`look/gamerender.py`) draws every material with its effect's own compiled
shaders, converted to GLSL (`fxglsl`). There is no approximation. The bindings that matter for characters:

- **`environmentScrollMap`.** The engine binds `misc/environmentScroll.dds` itself (a 64×64 DXT1 cube in
  `startup3.zip`, loaded by `sdaemon.exe` at start-up); materials never list it. The renderer loads the
  same file (`arcade.engine_file_bytes`).
- **`renderedEyeTexture`** is drawn each frame by `look/gameeyes.py` from the eye values (see
  [Import](Import.md#eyes)).
- **The reflection cube (`envMapInstance` / `environmentMap`).** Without a world, a character uses its slot's own
  cube (for example `errorsky.dds` or `chrome.dds`). An empty slot uses `NEUTRAL_ENV`, an RGBM stand-in. In a
  Scene Editor map the probe cube overrides it (`ENV_OVERRIDE`, see [Scene Editor link](SceneEditorLink.md)).
- **Lighting without a world.** `NEUTRAL_LIGHT` / `NEUTRAL_SH` come from Tokyo's sun and PrimarySceneRig,
  averaged to grey.
- **Character reflection maths.** `(rgb * a * 6)² * g_EnvMapExposure * scroll`, from the `a_*_skin` shaders.

The engine slots (`ENGINE_TEXTURES = ('renderedEyeTexture', 'environmentScrollMap')`) are shown as "(none) - the
game binds its own" and are never required on export.

### Shared renderer behaviour worth knowing

The renderer in `look/gamerender.py` / `look/gamepost.py` is the Scene Editor's (synced by
`tools/sync_look.py`; edit it there, never here). What it does beyond drawing:

- **Film > Transparent**: `render()` clears to alpha 0 and `gamepost.blit(..., transparent=True)` keeps alpha 0
  where nothing was drawn.
- **Blender 4.1**: without `GPUShaderCreateInfo.depth_write()` the post shaders use the older `GPUShader`
  constructor (`gamepost.legacy_quad_shader`), so the depth handed to the overlays survives.
- **Caches**: meshes are kept across undo / redo while their `_exact` fingerprint matches; an object only shown
  or hidden keeps its mesh (checked, not re-read); `mat_cache` holds each material's bindings and values once;
  `rebuild(soft=True)` keeps unchanged objects' commands. The Scene Editor's `docs/BlenderPerformance.md` and
  `docs/GameRendererCode.md` describe them.

## The Look and Cars 2 Material panels

- **Look** (`CARS2TK_PT_look`, sidebar):
  - Rendered view: Game Look / Blender (`cars2tk.set_engine`);
  - the Blender materials' mode (Game Look unlit / Blender lights / textures only), shared with the Scene Editor's
    Viewport setting;
  - shadows, reflections, bloom, auto exposure;
  - **Emissions** with Strength;
  - Game Assets folder.
- **Material** (sidebar) and **Cars 2 Material** (Properties > Material): the Scene Editor's material panel
  (`look/lookui.py`): Pick Effect, texture slots, parameters with reset, blending and culling, and the texture
  list.

## Editing

Every operator is the Scene Editor's, run against the owner's table:

- **Pick Effect** (`C2_OT_switch_effect`) shows a confirmation that lists the slots and parameters the new
  effect drops. Dropped values are cached on the material (`c2_dropped`) and restored on switching back. New
  slots arrive empty. `gamerender.effect_changed` throws away the material's GPU batches, because they carry the
  old effect's vertex attributes.
- **Pick Texture / Load Image** write to `arm.c2tex`. The picker lists every texture, with the slot's own kind
  first (2D or cube) and the others marked "(cube map)" / "(2D)". **Load Image** with a file named like a texture
  the character already has (case ignored) replaces that texture in its own slot instead of adding a second entry
  of the same name; otherwise it adds a new texture. A replacement:
  - re-reads an image Blender already loaded from that file (`img.reload()`), so a texture edited and saved over
    the same file shows its new pixels; `bpy.data.images.load(check_existing=True)` alone returned the old copy;
  - rebuilds every material using the texture (`fxnodes.on_owner_texture_replaced`, which also drops the Cars 2
    Game render's GPU texture cache), not only the one whose slot was clicked;
  - finds images by file path (`matio.image_at`), not file name: a replacement named like the extracted
    original loads as `<name>.dds.001`, and the name lookup showed the old thumbnail;
  - writes the size / format / mips line (`matio.refresh_info`) as the game will get it: a `.dds` from its
    header (e.g. `1024x1024 DXT1, 11 mips`), another image as the uncompressed DDS export writes
    (`RGB32`, full mip chain), marked `new` or `replaced`. Editing **Replace With** by hand refreshes it too.
- **Live updates.** Material updates reach the renderer through the depsgraph, and `Renderer._refresh` rebuilds
  any command whose part now has a different effect (Reflect technique for mirror passes). A still viewport only
  redraws edited objects, so this matters: `tests/bl_tk_still.py` covers it.
- **Undo.** Undo recreates data-blocks. `fxnodes.reset_after_undo` and `gamerender.reset_after_undo` run on
  `undo_post` / `redo_post`, `Textures.revalidate` re-resolves images, and the draw callback heals itself on
  `ReferenceError`.

## Preview-only values

Some values change only what is drawn. They never touch `mat.c2` and never reach an export:

| Value | Store | Why |
|---|---|---|
| eye values | `gamerender.EYE_VALUES[mat]` | the game evaluates the eye rig every frame and stores nothing |
| Emissions | `fxnodes.PREVIEW_VALUES[mat]` (`emissiveMultiplier`) | 85 of the 86 character glow materials store 0; the game turns glows on at run time (brake lights when braking ...) |
| probe cube | `gamerender.ENV_OVERRIDE[arm]` | the map lights the car where it stands |

**Emissions** (`apply_emissions`) set `emissiveMultiplier` to Strength for glows stored at 0, and scale glows
stored lit by Strength. They re-apply when a material changes (a new glow slot or texture picked: the
`_on_depsgraph` handler) and after loading a file (`_on_load`, through a timer).

## Export: `charmat.missing()` and `charmat.exporter()`

- `missing(scene, arm)` lists every `material: slot` the effect samples that has no texture
  (`fxnodes.missing_in`). Export Character refuses while the list is non-empty.
- `exporter(scene, arm)` returns `apply(tree)`, which `c2rigedit.build_with_geometry` calls on the tree it
  exports:
  1. `matio.add_new_textures` / `apply_replacements` write new and replaced textures into the TexturePool
     (names hashed with `c2fx.crc`; new DDS uncompressed).
  2. For each material: `Effect`, `RenderCaps` and `matio.write_props` (parameters and slots, **only what
     changed**, leaving dropped entries out).
  3. A newly picked effect's `.tfx` is returned (the package's own copy, else the game's) and written to
     `effects/<effect>.tfx` in the export, because a character package ships the effects it uses.
  4. `compact_textures` leaves one TexturePool entry per texture name and none that nothing uses.
     Entries with the same name (case ignored) are merged into the first slot, which keeps its name and takes
     the image of the newest same-named entry a material uses. Entries no material references are removed, and
     every `TextureReference` is renumbered. Material `TextureEntries` are the only references into a
     character's TexturePool, and none of the 171 shipped character `.oct` files has an unused or duplicate
     texture, so vanilla characters export unchanged. Files that already carry duplicates from older toolkit
     versions are cleaned on their next export; the export report lists each merge and removal.

An untouched character exports byte-identical (`tests/bl_tk_materials.py`).

## Custom effects

Effects you put in the game's `assets/effects/effects` (or the package's `effects/`) are read like shipped ones:

- `d3dbc` decodes them, `fxglsl` converts them, and `fxgraph` / `matnodes` build the Blender material;
- they appear in Pick Effect's list, which filters to skinned effects for characters (`matops.effect_list(...,
  skin=True)`) and is cached in a user cache file;
- the Scene Editor's `docs/EffectNameGrammar.md` explains what an effect's name says it does.
