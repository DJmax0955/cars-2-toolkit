#!/usr/bin/env python3
"""
port_car.py - convert every slot of one Cars 3 car for use in Cars 2.

Reads slot_map.json (from clip_slot_map.py), converts each convertible
Cars 3 clip with the --mode that slot needs, and writes:

  <outdir>/characters/<c2_char>/motions/<cars3 clip>.json   offsetting JSON, ready for
                                                            `offsetting oct encode`
  <outdir>/bent_patch.json    ClipSet -> alias -> new Filename, for the Cars 2 .bent
  <outdir>/report.txt         per-slot result, dropped channels, skipped slots

New files go under the Cars 2 character's own motions folder instead of
overwriting characters/car/motions/shared_*, which every car shares.
Slots with no Cars 3 equivalent (frontend screen moves, entry/exit,
.banm procedurals, ...) are left pointing at their Cars 2 clip.

    python port_car.py slot_map.json --car cars3_mack --c2-char mcqueen \\
        --c3-root Cars_3_Characters/ --outdir ported/mack
"""

import argparse, collections, glob, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cars3_to_cars2 as cv  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("slot_map")
    ap.add_argument("--car", required=True, help="Cars 3 car key in slot_map.json, e.g. cars3_mack")
    ap.add_argument("--c2-char", required=True, help="Cars 2 character folder the clips will live in")
    ap.add_argument("--c3-root", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--scale", choices=("face", "keep", "drop"), default="face")
    ap.add_argument("--map", help="port map overrides (same as cars3_to_cars2.py --map)")
    ap.add_argument("--min-shared", type=int, default=0,
                    help="only port slots whose clips share at least N curves (default 0 = all)")
    args = ap.parse_args()

    rows = json.load(open(args.slot_map))[args.car]
    import clip_slot_map as csm
    by_path, _by_name = csm.index_files(args.c3_root, ".oct")

    rel_dir = "characters/%s/motions" % args.c2_char
    out_dir = os.path.join(args.outdir, rel_dir)
    os.makedirs(out_dir, exist_ok=True)
    patch = collections.OrderedDict()
    lines, done = [], {}
    for r in rows:
        tag = "%-12s %-28s" % (r["clipset"], r["c2_alias"])
        if r["status"] != "ok":
            lines.append("%s KEEP C2   %s" % (tag, r["status"]))
            continue
        if r["shared_curves"] < args.min_shared:
            lines.append("%s KEEP C2   only %d shared curves" % (tag, r["shared_curves"]))
            continue
        base = os.path.basename(r["c3_file"])
        stem = cv.cars2_clip_name(os.path.splitext(base)[0])     # shrd_ -> shared_ (v2.0.56)
        key = (stem, r["mode"])
        # one clip can fill several slots; convert once per mode
        if key not in done:
            name = stem if not any(k[0] == stem for k in done) else "%s__%s" % (
                stem, r["mode"].replace(",", "+"))
            dst = os.path.join(out_dir, name + ".json")
            try:
                _n, templates, report = cv.convert_file(
                    by_path[r["c3_file"].lower()][0], dst, args.map, args.scale, r["mode"])
                done[key] = (name, "+".join(templates) + " | " + cv.describe(report, None))
            except (ValueError, KeyError, IndexError) as exc:
                done[key] = (None, "FAILED: %s" % exc)
        name, info = done[key]
        if name is None:
            lines.append("%s KEEP C2   %s" % (tag, info))
            continue
        patch.setdefault(r["clipset"], collections.OrderedDict())[r["c2_alias"]] = "%s/%s" % (rel_dir, name)
        lines.append("%s -> %-40s %s" % (tag, name, info))

    with open(os.path.join(args.outdir, "bent_patch.json"), "w") as h:
        json.dump(patch, h, indent=1)
    with open(os.path.join(args.outdir, "report.txt"), "w") as h:
        h.write("\n".join(lines) + "\n")
    n = sum(len(v) for v in patch.values())
    print("%s: %d slots ported, %d clip files, %d slots kept on Cars 2 clips -> %s"
          % (args.car, n, len([v for v in done.values() if v[0]]), len(rows) - n, args.outdir))


if __name__ == "__main__":
    main()
