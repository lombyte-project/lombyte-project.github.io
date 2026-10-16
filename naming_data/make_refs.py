#!/usr/bin/env python3
"""Write refs.json.gz: which game code touches each data address, from the retail programs.

The Desk needs it to tell whether a global can move into the one C file that reads it. The
programs are not in any repository, so this runs once on a machine that has them, and only
the facts it finds (sections, addresses and the units or functions that touch them; no bytes)
are kept here. They never change: the same code always touches the same data.

usage: make_refs.py GAME_CHECKOUT OVERLAYS_DIR   (OVERLAYS_DIR: config/us/overlays or Tools extracted/overlays)
"""
import bisect
import gzip
import importlib.util
import json
import pathlib
import re
import sys

game = pathlib.Path(sys.argv[1]).resolve()
overlays = pathlib.Path(sys.argv[2]).resolve()
spec = importlib.util.spec_from_file_location("data_refs", game / "scripts" / "data-refs.py")
dr = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(game / "scripts"))
sys.modules["data_refs"] = dr
spec.loader.exec_module(dr)


def rows_of(code, data, gp, owner_for):
    rows = {}
    for access in dr.scan_elf(code, data, gp):
        sec = dr.section_name(access.target, data)
        if sec is None:
            continue
        rows.setdefault(access.target, set()).add(owner_for(access.insn))
    return rows


def pack(rows, data):
    names = sorted({o for owners in rows.values() for o in owners})
    ix = {n: i for i, n in enumerate(names)}
    return {"sections": [[name, addr, size] for name, addr, size in data],
            "owners": names,
            "refs": {format(a, "x"): sorted(ix[o] for o in rows[a]) for a in sorted(rows)}}


out = {}
gp_match = dr.GP_VALUE_RE.search(dr.CONFIG_PATH.read_text())
gp = int(gp_match.group(1), 16) if gp_match else dr.DEFAULT_GP
code, data = dr.read_sections(dr.ELF_PATH)
starts, owners = dr.owners_table(dr.CONFIG_PATH)


def boot_owner(addr):
    i = bisect.bisect_right(starts, addr) - 1
    return owners[i][0] if i >= 0 else "?"


out["boot"] = pack(rows_of(code, data, gp, boot_owner), data)
core = [s for s in data if s[0].startswith("core.")]
for level in range(19):
    if not (overlays / f"level_{level:02d}").exists():
        continue
    lcode, ldata = dr.level_image(overlays, level)
    # rows include the executable's core.* data the level code touches; sections list the level's own
    rows = rows_of(lcode, ldata + core, dr.DEFAULT_GP, dr.level_owner_of(level))
    out[f"{level:02d}"] = pack(rows, ldata)

dest = pathlib.Path(__file__).resolve().parent / "refs.json.gz"
dest.write_bytes(gzip.compress(json.dumps(out, separators=(",", ":"), sort_keys=True).encode(), mtime=0))
print(f"wrote {dest} ({dest.stat().st_size} bytes): " + ", ".join(f"{k} {len(v['refs'])}" for k, v in out.items()))
