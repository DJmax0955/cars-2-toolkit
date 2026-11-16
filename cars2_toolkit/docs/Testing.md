# Testing

The tests live in `cars2-character-toolkit/tests/`, outside the add-on, so they are not shipped in the zip.
They run Blender on a **copy** of the add-on and on extracted characters. Never point them at the game's own
assets folder for output, and never write into it.

## Blender scripts

Run them as `blender [-b] --python <test> -- <args>`. `-b` means background; the "real window" tests need a UI
because they draw the viewport and take screenshots.

| Test | Mode | What it proves |
|---|---|---|
| `bl_tk_materials.py` | `-b` | Toolkit alone: game materials set up, and an untouched character **exports byte-identical**; a shader swap and swap back returns to the original |
| `bl_tk_shared.py` | `-b` | Toolkit plus Scene Editor: one material system copy runs, both edit the same data, and copies hand over when either add-on is enabled or disabled (18 checks) |
| `bl_tk_look.py` | window | Game Look and Blender materials render (screenshots); optional world argument loads the Scene Editor and a map first |
| `bl_tk_panel.py` | window | the Look and Cars 2 Material panels draw (screenshot) |
| `bl_tk_swap.py` | window | assets folder found with nothing preset, and a body shader swap shows at once |
| `bl_tk_newslot.py` | window | a swap that adds a glow slot, a texture picked for it, Emissions on: each step shows at once |
| `bl_tk_still.py` | window | **still viewport**: switch to a glow + normal shader, pick textures, switch to glass; every draw command must use the material's current effect (`C2T_FX` env var picks the shader) |
| `char_slot_survey.py` | `-b` | for every material of every game character, the slots its effect samples against the `TextureEntries` it lists |

Arguments are in each script's docstring: usually the folder holding `cars2_toolkit` (and `cars2_scene_editor`),
then a character `.oct`, then an output folder or screenshot prefix.

## Lessons the tests encode

- **Don't preset `scene.c2.assets_dir`.** It hid the bug where Finn's assets folder wasn't found.
- **Test material edits with a still viewport.** Playback rebuilds every frame and hides stale-effect bugs.
- **Wait about 8 s before Game Look screenshots.** Auto exposure adapts frame by frame, so early shots look
  blown out. Alternatively, switch `game_auto_exposure` off as `bl_tk_still.py` does.
- **Script undo** needs `ed.undo_push` / `ed.undo` with a full window, screen, area and region override.
- **Byte identity is the regression check** for anything touching export. Compare the exported `.oct` with the
  source, not just "it loads".

## Outside Blender

`core/` is pure Python, so most format work is tested directly:

```python
import sys; sys.path.insert(0, 'cars2_toolkit/core')
import c2oct, c2scene, c2validate
t = c2oct.load('mcqueen.oct'); assert c2oct.encode(t) == open('mcqueen.oct', 'rb').read()   # round trip
```

Run `python core/c2validate.py file.oct` on any rig. `c2rigedit.export()` and `c3toc2.convert()` run whole
exports without Blender.

## Before shipping a change

1. If the change touches `look/` or the synced readers, make it in the Scene Editor and run
   `tools/sync_look.py`. Check with `--check`.
2. Run `bl_tk_materials.py` (byte identity) and `bl_tk_shared.py`.
3. For renderer or material changes, run `bl_tk_still.py` and `bl_tk_newslot.py` and look at the screenshots.
4. Bump `bl_info['version']` in `__init__.py`. The export report prints it, which makes bug reports traceable.
