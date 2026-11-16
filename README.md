# Cars 2 Toolkit

A Blender add-on to import, edit and export **Cars 2: Arcade (Win32)** characters: models, control rig,
animation clips, FX markers, materials and textures. It can also put your own model on a Cars 2 rig,
and import Cars 3: Driven to Win characters and clips and convert them to Cars 2.

## Features

- **Import Cars 2 characters** (`.oct`) with meshes, textures, materials, eyes and FX markers, with or without the game's control rig.
- **Edit the rig.** Add bones, animation channels and constraints, move FX markers, and validate the result against the rules every stock rig follows.
- **Animation clips.** Import and export clips, eyelid and mouth sliders included. Clips also play on background characters and props.
- **Game materials.** Each material runs its effect's own game shader, eyes included. Edit materials and textures and export the changes.
- **Custom models.** Import a character template, weight your model to it, and export it as a Cars 2 character.
- **Cars 3: Driven to Win.** Import characters and clips from the PS4 version and convert them to Cars 2.
- **Byte-exact export.** Writes `<car>.oct` + `<car>_0.vbuf` / `<car>_0.ibuf`, plus `<car>.bent` only when its animation channels changed. An untouched character exports byte-identical.
- **Works with the [Cars 2 Scene Editor](https://github.com/MythicalBry/cars-2-scene-editor).** With both add-ons enabled, characters are lit by the map. The toolkit also works fully on its own.

## Install

1. Download **`cars2_toolkit.zip`** from the [Releases](../../releases/latest) page.
2. If you have the older **Cars 2 Animation (.oct)** add-on, disable it. The toolkit includes it, and two copies clash.
3. In Blender, go to **Edit > Preferences > Add-ons > Install from Disk**, pick the zip and enable **Cars 2 Toolkit**.

**Blender:** 4.1 and later (tested on 4.1, 5.0, 5.2 and 5.3).

**Optional:** [offsetting](https://github.com/offsetting/offsetting) (`offsetting.exe`), only for exporting onto an offsetting `.oct.json` and for *Pack as game .zip*. Set its path in the add-on preferences.

## Where to find it

| Where | What |
|---|---|
| **File > Import** | Cars 2 Character (.oct), Cars 3 Character (.oct), Cars 2 Animation (.oct), Cars 3 Animation (.oct) |
| **File > Export** | Cars 2 Character (.oct), Cars 2 Animation (.oct) |
| **3D View > Sidebar (N) > Cars 2 Character** | Cars 2 Animation, Cars 2 Rig, Custom Model, Cars 3 to Cars 2, Look, Material |
| **Properties > Material** | Cars 2 Material, Eye Lid Shadow |

## Quick start

1. **File > Import > Cars 2 Character (.oct)** and pick a character's `.oct`, for example `characters/mcqueen/mcqueen.oct`.
   The `_0.vbuf` / `_0.ibuf` next to it and the `.bent` two folders up are found automatically.
   Tick **Import Control Rig** if you want to animate or edit the rig.
2. Make your changes.
3. Press **Export Character** in the *Cars 2 Rig* panel.
4. Copy **all** the exported files over the originals in the character package. A new `.oct` must never be paired with the old `.vbuf` / `.ibuf`.

## Documentation

- [User manual](cars2_toolkit/README.md): every panel and option
- [Technical notes](cars2_toolkit/docs/README.md): file formats, the rig network, materials, the Cars 3 pipeline

## Disclaimer

This repository contains **no game files**. You need your own copy of the games.
Cars 2, Cars 3 and their game files belong to their respective owners. This project is not affiliated with
Disney, Pixar or Avalanche Software.

## License

GNU General Public License v3.0 or later. See [LICENSE](LICENSE).
Third-party code and its licenses: [cars2_toolkit/THIRD_PARTY_NOTICES.md](cars2_toolkit/THIRD_PARTY_NOTICES.md).
