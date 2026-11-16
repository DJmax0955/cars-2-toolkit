# Scene Editor link

Two things connect the toolkit and the Cars 2 Scene Editor: a **single shared copy** of the material system,
and **map lighting** for characters placed in a Scene Editor world.

## One copy of the material system (`look/looklink.py`)

Both add-ons ship the material system. The Scene Editor's copy is the master, and the toolkit's `look/` is
written by the Scene Editor's `tools/sync_look.py`, together with the format readers in `core/` and
`data/effect_list.json`. Both register the same Blender names (`Material.c2`, `Object.c2tex`, the `c2.*`
operators, the Cars 2 Game engine), so only one copy may be registered at a time:

- `bpy.app.driver_namespace['cars2_look']` records the running copy. `cars2_look_waiting` lists add-ons waiting
  to take over.
- **Scene Editor registers:** if the toolkit's copy is running, the Scene Editor unregisters it (the toolkit
  goes on the waiting list) and registers its own.
- **Scene Editor unregisters:** a waiting toolkit copy takes over.
- **Toolkit registers** (`look/__init__.py`): if a copy is already running, it uses that copy and waits.
  Otherwise it registers its own.

Toolkit code never imports `look` modules directly. It calls `look.api()` (`charmat.api()`), which returns
`looklink.modules()`, the running copy's modules. This means:

- With **both add-ons** enabled, a material edited in either panel is the same data, and the Scene Editor's
  Blender + baked lights mode applies to characters too.
- With **only the toolkit**, everything works on its own: Game Look, editing, export.
- Switching add-ons on and off at run time hands over cleanly (`tests/bl_tk_shared.py`).

File loads empty `bpy.app.driver_namespace`, so `looklink` keeps its records a second time in `sys.modules` and
restores them; see the Scene Editor's SharedMaterialSystem.md, "One copy runs".

**Never edit files in `look/` or the synced `core/` readers.** Change them in the Scene Editor, then run:

```bash
python tools/sync_look.py
```

To check for drift:

```bash
python tools/sync_look.py --check
```

`look/__init__.py` is the only toolkit-owned file in `look/`.

## Characters in a map (`c2selink.py`)

When a Scene Editor world is loaded, its renderer draws the character with the map's sun, light rig, shadows,
fog and exposure, because the character's materials are game materials. What `c2selink` adds is the light the
game gives a car **where it stands** (from `sdaemon.exe`):

- **objectSH.** Inside a Volume light probe, the probe's DirectSH grid is sampled trilinearly at the car's
  position. In the fade margin it is blended with the default rig, and several probes are averaged. Outside
  any probe the PrimarySceneRig is used. The Scene Editor keeps objectSH in one shared engine block, so a small
  hook (`_hook`) gives the character's draws their own copy of the block.
- **Reflection cube.** The first Volume probe the car is inside that has a cube, else the scene probe, goes
  into `gamerender.ENV_OVERRIDE[arm]`. The material keeps its own slot, so export is unchanged.
- **Updates.** `sync(scene)` runs on import, on depsgraph updates for moved characters, on frame change and on
  load and undo. Probes are cached (`_probes`) and the code avoids `scene.objects.get`. Scanning all objects every
  frame ran at 5 fps in tokyo_a; this runs at about 58 ms per frame there.
- **Panel text.** `cars2_se_light` / `cars2_se_refl` on the armature say which probes light it.

Nothing here writes into the world, the character's materials or its source files, and the Scene Editor's world
exporter skips characters.
