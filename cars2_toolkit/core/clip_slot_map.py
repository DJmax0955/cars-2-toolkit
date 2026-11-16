#!/usr/bin/env python3
"""
clip_slot_map.py - which Cars 3 clip goes into which Cars 2 clip slot, per car.

Cars 2 plays clips by alias through a .bent ClipSet (Clip#BRAKE_STOP ->
characters/car/motions/shared_brake_stop). Cars 3 does the same through its own
.bent (File#BM_BRAKE_STOP -> characters/class_car/motions/shrd_bm_brake_stop.oct),
with a different alias vocabulary and a per-car file behind most aliases
(mack_*, guido_*, drd_* ...).

For every Cars 2 slot this resolves the Cars 3 alias (SLOT_RULES below),
then that alias to the file the given Cars 3 car actually uses, and scores
the pair by how many animation curves the two files share. It also emits the
--mode the converter needs: the templates of the Cars 2 clip currently in
the slot (a Rattles slot needs car_rattles, an Eyes slot car_eyes, ...).

    python clip_slot_map.py --c2-bent mcqueen.bent --c2-frontend-bent car_frontend.bent \
        --c3-root Cars_3_Characters/ --c2-root C2_Characters/ -o slot_map.json

Both roots are searched recursively for .bent and .oct files (extracted).
"""

import argparse, collections, csv, glob, json, os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cars3_to_cars2 as cv      # noqa: E402
import octread                   # noqa: E402

# ---------------------------------------------------------------------------
# Cars 2 alias -> Cars 3 alias. Tried in order: explicit table, then the
# generic prefix rules in candidates(). Upper-cased on both sides.
# ---------------------------------------------------------------------------
SLOT_RULES = {
    # locomotion
    "IDLE": "BM_IDLE",
    "BUNNYHOP": "BM_JUMP",
    "TURBOSTART": "BM_TURBOSTART", "TURBOSTART2": "BM_TURBOSTART2",
    "TURBO_LOOP": "BM_TURBO_LOOP", "TURBOSTOP": "BM_TURBOSTOP",
    "BRAKE_STOP": "BM_BRAKE_STOP", "BRAKE_SHUDDER": "BM_BRAKE_SHUDDER",
    "JMPLAND": "BM_JUMPLAND_SML", "JMPLAND_MED": "BM_JUMPLAND_MED",
    "JMPLAND_LRG": "BM_JUMPLAND_LRG",
    "REVERSE_IDLE": "BKWRD_REVERSE_IDLE",
    "180_2REVERSE": "BKWRD_180_2REVERSE", "180_2FORWARD": "BKWRD_180_2FORWARD_01",
    "180_2FORWARD2": "BKWRD_180_2FORWARD_02", "180_2FORWARD_R": "BKWRD_180_2FORWARD_R",
    "BACKWARDS_2BUNNYHOP": "BKWRD_2BUNNYHOP_R", "BACKWARDS_2BUNNYHOP_L": "BKWRD_2BUNNYHOP_L",
    "2WHEELLEFT2BUNNYHOP": "2WHEELLEFT2JUMP", "2WHEELRIGHT2BUNNYHOP": "2WHEELRIGHT2JUMP",
    "WALLRIDELEFT_START": "WALLRIDE_LFT_START", "WALLRIDELEFT": "WALLRIDE_LFT",
    "WALLRIDELEFT_STOP": "WALLRIDE_LFT_STOP", "WALLRIDERIGHT_START": "WALLRIDE_RHT_START",
    "WALLRIDERIGHT": "WALLRIDE_RHT", "WALLRIDERIGHT_STOP": "WALLRIDE_RHT_STOP",
    "WALLRIDEIMPACT_L": "WALLRIDE_LFT_RCT_IMPCT_SML", "WALLRIDEIMPACT_R": "WALLRIDE_RHT_RCT_IMPCT_SML",
    # tricks
    "JMPTRICK_FAIL_LOOP_01": "BKWRD_JMPTRICK_REV_FAIL_LOOP_01",
    "JMPTRICK_360B": "JMPTRK_360_BCK", "JMPTRICK_360B_END": "JMPTRK_360_BCK_EASE",
    "JMPTRICK_360F": "JMPTRK_360_FWD", "JMPTRICK_360F_END": "JMPTRK_360_FWD_EASE",
    "JMPTRICK_ROLL_L": "JMPTRK_360_ROLL_LEFT", "JMPTRICK_ROLL_L_END": "JMPTRK_360_ROLL_LEFT_EASE",
    "JMPTRICK_ROLL_R": "JMPTRK_360_ROLL_RHT", "JMPTRICK_ROLL_R_END": "JMPTRK_360_ROLL_RHT_EASE",
    "JMPTRICK_ROLL_R_REV": "BKWRD_JMPTRK_360_ROLL_RHT",
    "JMPTRICK_ROLL_R_END_REV": "BKWRD_JMPTRK_360_ROLL_RHT_EASE",
    # bash / reacts
    "BUNNYHOP_SIDEL": "ATK_BASH_L", "BUNNYHOP_SIDER": "ATK_BASH_R",
    "BUNNYHOPL_LAND": "ATK_BASH_L_LAND", "BUNNYHOPR_LAND": "ATK_BASH_R_LAND",
    "REACT_BASH_IMPACTER_L": "ATK_BASH_L_IMPCT", "REACT_BASH_IMPACTER_R": "ATK_BASH_R_IMPCT",
    "REACT_BASH_IMPACTEETIP_L": "RCT_BASH_L_TIP", "REACT_BASH_IMPACTEETIP_R": "RCT_BASH_R_TIP",
    "REACT_SPINOUT_LFT": "RCT_WPN_SPINOUT_L", "REACT_SPINOUT_RHT": "RCT_WPN_SPINOUT_R",
    "SHARED_REACT_SPINOUT_LFT": "RCT_WPN_SPINOUT_L", "SHARED_REACT_SPINOUT_RHT": "RCT_WPN_SPINOUT_R",
    "REACT_WALL_CRASH_REV": "BKWRD_REACT_WALL_CRASH_REV",
    "IDLE_RATTLE": "RCT_RATTLE_IDLE",
    "BACKWARDS_LEFT_BUFF": "BKWRD_L_BUFF", "BACKWARDS_RIGHT_BUFF": "BKWRD_R_BUFF",
    "WEAPON_LOADOUT": "RCT_WEAPON_LOADOUT", "WEAPON_MACHINEGUNFIRE": "RCT_WEAPON_MACHINEGUNFIRE",
    # lean
    "DRIFT_LEAN_ADD": "BM_DRIFT_LEAN_ADD", "LEAN_ADDITIVE": "BM_LEAN_PROC",
    # face / eyes
    "EMO_IDLE_ZERO_MPH_01": "IDLE_ZEROMPH_01", "EMO_IDLE_ZERO_MPH_02": "IDLE_ZEROMPH_02",
    "EMO_IDLE_LOOKDOWN": "FACE_EMO_LOOKDOWN", "EMO_IDLE_LOOKLEFT": "FACE_EMO_LOOKLEFT",
    "EMO_IDLE_LOOKRIGHT": "FACE_EMO_LOOKRIGHT", "EMO_IDLE_LOOKUP": "FACE_EMO_LOOKUP",
    "SHARED_QUICKSTART_INTRO_LOOP": "QUICKSTART_INTRO_LOOP",
    "SHARED_BLINK": "FACE_BLINK",
    "EYE_DART_L": "FACE_EYE_DART_L", "EYE_DART_R": "FACE_EYE_DART_R",
    "SHARED_EYES_LOWER_CENTER": "FACE_EYES_LOWER_CENTER",
    "SHARED_EYES_LOWER_LEFT": "FACE_EYES_LOWER_LEFT",
    "SHARED_EYES_LOWER_RIGHT": "FACE_EYES_LOWER_RIGHT",
    # frontend
    "SELECT_IDLE": "FE_SELECT_IDLE", "SELECT_IDLE2": "FE_SELECT_IDLE2",
    "SELECT_SELECTED": "FE_SELECT_SELECTED",
}

# Body-clip-set slots whose Cars 2 alias is emo_Idle_LookX play the body
# look, not the face look.
BODY_OVERRIDES = {
    "EMO_IDLE_LOOKDOWN": "BM_IDLE_LOOKDOWN", "EMO_IDLE_LOOKLEFT": "BM_IDLE_LOOKLEFT",
    "EMO_IDLE_LOOKRIGHT": "BM_IDLE_LOOKRIGHT", "EMO_IDLE_LOOKUP": "BM_IDLE_LOOKUP",
    "EMO_IDLE_ZERO_MPH_01": "IDLE_ZEROMPH_01", "EMO_IDLE_ZERO_MPH_02": "IDLE_ZEROMPH_02",
}


def candidates(alias, clipset):
    a = alias.upper()
    out = []
    if clipset == "Body" and a in BODY_OVERRIDES:
        out.append(BODY_OVERRIDES[a])
    if a in SLOT_RULES:
        out.append(SLOT_RULES[a])
    out.append(a)
    if a.startswith("REACT_"):
        out.append("RCT_" + a[6:])
    if a.startswith("EMO_"):
        out.append("FACE_" + a)
    out += ["BM_" + a, "BKWRD_" + a, "FACE_" + a]
    seen, res = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            res.append(c)
    return res


# ---------------------------------------------------------------------------
# Curve signatures (same decoder as the port-map verification)
# ---------------------------------------------------------------------------

def _decode_command(b):
    w = struct.unpack_from("<I", b, 0)[0]
    n_ch, n_keys, op = (w & 0xFFC00) >> 10, w >> 20, (w & 0x3C0) >> 6
    t0, t1 = struct.unpack_from("<ff", b, 4)
    span = t1 - t0
    curves = []
    for c in range(n_ch):
        vals = []
        if op == 0:
            bias, scale = struct.unpack_from("<ff", b, 12 + 16 * c + 8)
            kt = 12 + n_ch * 16
            sm = kt + n_keys * 2
            for k in range(n_keys):
                vals.append(bias + scale * struct.unpack_from("<h", b, sm + (k * n_ch + c) * 8)[0])
        elif op in (4, 6):
            kt = 12 + n_ch * 8
            if op == 4:
                sm = kt + n_keys * 2 + (2 if n_keys & 1 else 0)
            else:
                sm = kt + n_keys * 4
            for k in range(n_keys):
                vals.append(struct.unpack_from("<f", b, sm + (k * n_ch + c) * 16)[0])
        else:
            return []
        if len(vals) >= 3 and max(vals) - min(vals) > 1e-4:
            curves.append((len(vals), round(span, 2), tuple(round(v, 2) for v in vals)))
    return curves


def signatures(path):
    try:
        raw = open(path, "rb").read()
        block = octread.clip_block(raw)
        if block is None:
            return set()
        clip = cv.split_block(block)
    except Exception:
        return set()
    sigs = set()
    for c in clip["commands"]:
        try:
            sigs.update(_decode_command(c))
        except struct.error:
            pass
    return sigs


# ---------------------------------------------------------------------------

def index_files(root, ext):
    """(by game path, by basename) indexes of every extracted file."""
    by_path, by_name = collections.defaultdict(list), collections.defaultdict(list)
    for p in sorted(glob.glob(os.path.join(root, "**", "*" + ext), recursive=True)):
        by_path[game_path(p)].append(p)
        by_name[os.path.basename(p).lower()].append(p)
    return by_path, by_name


def game_path(p):
    """'characters/<char>/motions/x.oct' part of an extracted path (lower case)."""
    p = p.replace("\\", "/").lower()
    i = p.rfind("characters/")
    return p[i:] if i >= 0 else os.path.basename(p)


def resolve(index_by_path, index_by_name, game_file):
    """Exact game path first; the basename is only a fallback."""
    hit = index_by_path.get(game_file.lower())
    if hit:
        return hit[0]
    hit = index_by_name.get(os.path.basename(game_file).lower())
    return hit[0] if hit else None


def c2_slots(bent_path):
    d = octread.load(bent_path)
    slots = []
    for cs_key, cs in d.get("ClipSetPool", {}).items():
        cs_name = cs_key.split("#", 1)[1]
        if cs_name == "Choreography":
            continue
        for clip_key, clip in cs.get("ClipPool", {}).items():
            slots.append((cs_name, clip_key.split("#", 1)[1], clip["Filename"]))
    return slots


def c3_files(bent_path):
    d = octread.load(bent_path)
    return {k.split("#", 1)[1].upper(): v["Filename"] for k, v in d.get("Files", {}).items()}


def is_stub(path):
    """Some Cars 3 bents (Luigi, Sheriff) point at 2010-era placeholder
    clips: a 16-byte ClipDataBlock with no binds and no commands."""
    try:
        block = octread.clip_block(open(path, "rb").read())
        return block is None or not cv.split_block(block)["binds"]
    except Exception:
        return True


def clip_templates(path):
    try:
        return [t for t in octread.clip_templates(open(path, "rb").read())
                if t in cv.CARS2_CHANNEL_MAPS]
    except Exception:
        return []


def slot_mode(clipset, c2_path):
    """Converter --mode for a slot. Body and Facial interfaces each pick the
    bind for their own template out of a multi-bind clip (Cars 2 ships
    shared_quickstart_intro_loop with car_body+car_facial in both sets), so
    those always get both. Every other set uses the templates of the Cars 2
    clip currently in the slot."""
    if clipset in ("Body", "Facial"):
        return "game"
    if clipset == "FrontEnd":
        return "frontend"
    return ",".join(clip_templates(c2_path)) if c2_path else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--c2-bent", required=True, help="Cars 2 car .bent (slot list), e.g. mcqueen.bent")
    ap.add_argument("--c2-frontend-bent", help="Cars 2 car_frontend.bent (frontend slot list)")
    ap.add_argument("--c3-root", required=True)
    ap.add_argument("--c2-root", required=True)
    ap.add_argument("-o", "--output", default="slot_map.json")
    ap.add_argument("--csv", help="also write a flat CSV")
    args = ap.parse_args()

    c3_oct, c2_oct = index_files(args.c3_root, ".oct"), index_files(args.c2_root, ".oct")
    slots = c2_slots(args.c2_bent)
    if args.c2_frontend_bent:
        slots += [s for s in c2_slots(args.c2_frontend_bent) if s[0] == "FrontEnd"]

    sig_cache = {}

    def sig(path):
        if path not in sig_cache:
            sig_cache[path] = signatures(path)
        return sig_cache[path]

    c2_info = {}
    for cs, alias, fn in slots:
        c2_info[fn] = resolve(c2_oct[0], c2_oct[1], fn + ".oct")

    bents = sorted(p for p in glob.glob(os.path.join(args.c3_root, "**", "*.bent"), recursive=True)
                   if "crowd" not in p)
    cars = collections.OrderedDict()
    for p in bents:
        stem = os.path.splitext(os.path.basename(p))[0]
        car = stem.replace("cars3_fe_", "cars3_")
        try:
            files = c3_files(p)
        except Exception:
            continue
        if files:
            cars.setdefault(car, {}).update(files)

    result, rows = collections.OrderedDict(), []
    for car, files in cars.items():
        car_rows = []
        for cs, alias, fn in slots:
            c2_path = c2_info.get(fn)
            mode = slot_mode(cs, c2_path)
            chosen, status, score = None, "no Cars 3 equivalent", 0
            for cand in candidates(alias, cs):
                if cand in files:
                    chosen = cand
                    break
            c3_file = files.get(chosen) if chosen else None
            if c3_file:
                c3_path = (c3_oct[0].get(c3_file.lower()) or [None])[0]
                if c3_file.lower().endswith(".banm"):
                    status = "procedural .banm (not convertible)"
                elif c3_path and is_stub(c3_path):
                    status = "empty stub clip in Cars 3 (no channels)"
                elif c3_path:
                    status = "ok"
                    if c2_path:
                        score = len(sig(c2_path) & sig(c3_path))
                else:
                    status = "file not in zip"
            row = {"car": car, "clipset": cs, "c2_alias": alias,
                   "c2_file": fn, "c3_alias": chosen, "c3_file": c3_file,
                   "status": status, "shared_curves": score, "mode": mode}
            car_rows.append(row)
            rows.append(row)
        result[car] = car_rows

    with open(args.output, "w") as h:
        json.dump(result, h, indent=1)
    if args.csv:
        with open(args.csv, "w", newline="") as h:
            w = csv.DictWriter(h, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    ok = sum(r["status"] == "ok" for r in rows)
    print("%d cars, %d slot rows, %d convertible" % (len(result), len(rows), ok))


if __name__ == "__main__":
    main()
