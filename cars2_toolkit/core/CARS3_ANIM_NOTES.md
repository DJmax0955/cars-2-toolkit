# Cars 3 -> Cars 2 animation port: mapping update

## Files
- `cars3_to_cars2.py` - converter (drop-in replacement). New: 12 port mappings, Tires_Ctrl fan-out,
  `--mode` for frontend and every other Cars 2 template, direct `.oct` input.
- `port_map.json` - `--dump-map` output, game and frontend rows with evidence tiers.
- `clip_slot_map.py` - Cars 2 clip slot -> Cars 3 clip, per Cars 3 car, with converter mode and a
  shared-curve confidence score. `slot_map.json` / `slot_map.csv` are its output for all 43 Cars 3
  cars against McQueen's Cars 2 slots (`mcqueen.bent` + `car_frontend.bent`).
- `port_car.py` - converts every convertible slot for one car and writes `bent_patch.json`
  (clip set -> alias -> new file) plus `report.txt`.
- `octread.py` - pure-Python OCT reader used by the above (no offsetting needed to read).

## New port mappings (evidence: same curve on this port in C3 and this channel in C2)
| C3 port | C2 channel | matches |
|---|---|---|
| 1 | car_body 0 BackEnd | 25 |
| 84 | car_facial 46 Eyes | 102 |
| 165-169 | car_facial 58-62 Mth_Corner_L/R, Mth_Expand_L/R, Mth_Offset | 13-16 each |
| 195 | car_body 34 TCable_Pulley | 11 |
| 203, 204 | car_body 35 TowCrane, 36 TowCraneBase | 3 each |
| 199 (Tires_Ctrl) | car_body 15 FrontTires + 1 BackTires | 57 + 9 |

Port 199 is only copied to a tire channel the clip does not already drive itself.
Visemes (ports 171-186, vsm_AA..vsm_Y) have no game-template target; in frontend mode
vsm_AA / vsm_IH / vsm_M are written.

## Still unmapped (dropped and reported)
13, 23, 87, 93 per-wheel spin; 148-162 Main_Ctrl custom attributes (includes 157/158, the
only channels of shrd_quickstart_go2a); 9-11, 15, 21, 38, 42-43, 85, 97-106, 115-122,
124-128, 189-194 - Cars 3-only rig nodes (mudflaps, barrels, extra FX markers, ...).
FX marker, Body_Baxle and Main_Baxle rows stay "rule": no clip pair confirms or contradicts them.

## Slots with no Cars 3 clip (keep the Cars 2 clip)
Frontend screen moves (FE_*), FRONTEND_ENTRY / _IDLE / EXIT, SHARED_OFFSCREEN_IDLE,
DRIVESTART, INTRO_REV, REACT_BASH_IMPACTEE_L/R, JMPTRICK_ROLL_L_REV / _END_REV,
QUICKSTART_RUMBLE*, 2WHEEL_WOBBLE, 2WHEEL*_BUFF, ChoBlender, emo_smile / emo_frown.
TURBO_LOOP, TURBOSTART, JMPLAND(_MED/_LRG) point at procedural `.banm` in Cars 3.
Luigi (3) and Sheriff (37) point at empty 16-byte placeholder clips in Cars 3.
