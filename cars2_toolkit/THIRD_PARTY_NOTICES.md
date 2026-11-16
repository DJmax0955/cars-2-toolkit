# Third-party notices

The Cars 2 Toolkit is licensed under the GNU General Public License v3.0 or later (see `LICENSE`).
It includes code from, or adapted from, the projects below. All of their licenses are GPL-compatible.

## Cars 2 Scene Editor

- Project: https://github.com/MythicalBry/cars-2-scene-editor
- License: GNU General Public License v3.0 or later
- Used in:
  - `look/`: the shared material system and Cars 2 Game render engine (every file except `look/__init__.py`)
  - `core/arcade.py`, `core/cubemap.py`, `core/d3dbc.py`, `core/dds.py`, `core/fxglsl.py`, `core/fxgraph.py`,
    `core/vertexfmt.py`: format readers and the shader translator used by `look/`

These files are copies of the Scene Editor's and are kept identical to them, so both add-ons can share one material system.

## matryoshka

- Project: https://github.com/offsetting/matryoshka (Team Offsetting)
- License: MIT OR Apache-2.0; used here under the MIT License
- Used in: `core/octread.py` (a port of matryoshka's `.oct` decoder); `anim.py` (the clip node header layout follows matryoshka)

```
MIT License

Copyright (c) 2023 Team Offsetting

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## offsetting

- Project: https://github.com/offsetting/offsetting (Team Offsetting)
- License: MIT OR Apache-2.0; used here under the MIT License
- Used in: the toolkit does not include offsetting itself. It can run a separately installed `offsetting.exe`
  (`zip-c2` packing, `oct decode`), and it reads and writes offsetting's `.oct.json` / `.oct.textures` layouts
  (`anim.py`, `character.py`, `export_any.py`, `pack.py`, `rig.py`, `c3look/c3json.py`, `core/c3toc2.py`,
  `core/cars3_to_cars2.py`, `core/c3anim.py`, `core/port_car.py`).

```
MIT License

Copyright 2024 Marcel <https://github.com/MarcelCoding>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## cars-2-blender-io

- Project: https://github.com/offsetting/cars-2-blender-io
- License: MIT License
- Used in: `core/vertexfmt.py` (vectorised vertex decoding adapted from `import-model-maps.py`, by way of the Scene Editor)

```
MIT License

Copyright (c) 2025 Team Offsetting

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
