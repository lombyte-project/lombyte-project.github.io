#!/usr/bin/env python3
"""Write xrefs.json.gz: where the retail programs call or take the address of code.

For each program (boot and every level) it keeps three lists of address pairs, no bytes:
calls (jal or j: instruction, target), ptrs (an address formed in a register by lui/addiu:
instruction, target) and words (a data word holding a code address: data address, target).
gen.py turns them into callers per function with the current names. Like refs.json.gz this
runs once on a machine that has the programs.

usage: make_xrefs.py GAME_CHECKOUT OVERLAYS_DIR
"""
import gzip
import importlib.util
import json
import pathlib
import sys

game = pathlib.Path(sys.argv[1]).resolve()
overlays = pathlib.Path(sys.argv[2]).resolve()
spec = importlib.util.spec_from_file_location("data_refs", game / "scripts" / "data-refs.py")
dr = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(game / "scripts"))
sys.modules["data_refs"] = dr
spec.loader.exec_module(dr)
from elftools.elf.elffile import ELFFile  # noqa: E402


def program(code, blobs, gp, outside=()):
    """code: [(name, addr, bytes)]; blobs: [(addr, bytes)] of data to search for code words."""
    ranges = [(a, a + len(b)) for _, a, b in code] + list(outside)

    def in_code(v):
        return any(s <= v < e for s, e in ranges)

    calls, ptrs, words = [], [], []
    for _name, base, body in code:
        items = list(dr.disassemble(body, base))
        for it in items:
            if isinstance(it, (dr.Quad, dr.Opaque)) or it.mnemonic not in ("jal", "j"):
                continue
            for op in it.operands:
                if op.type == dr.MIPS_OP_IMM and in_code(op.imm & dr.MASK32):
                    calls.append([it.address, op.imm & dr.MASK32, 1 if it.mnemonic == "j" else 0])
        for acc in dr.scan(items, gp, in_code):
            if acc.kind == "addr":
                ptrs.append([acc.insn, acc.target])
    for base, body in blobs:
        for off in range(0, len(body) - 3, 4):
            v = int.from_bytes(body[off:off + 4], "little")
            if in_code(v):
                words.append([base + off, v])
    return {"calls": calls, "ptrs": ptrs, "words": words}


gp_match = dr.GP_VALUE_RE.search(dr.CONFIG_PATH.read_text())
gp = int(gp_match.group(1), 16) if gp_match else dr.DEFAULT_GP
code, _data = dr.read_sections(dr.ELF_PATH)
blobs = []
with dr.ELF_PATH.open("rb") as fh:
    for s in ELFFile(fh).iter_sections():
        fl = s["sh_flags"]
        if fl & dr.SHF_ALLOC and not fl & dr.SHF_EXECINSTR and s["sh_addr"] and s["sh_type"] != "SHT_NOBITS":
            blobs.append((s["sh_addr"], s.data()))
out = {"boot": program(code, blobs, gp)}
boot_code = [(a, a + len(b)) for _, a, b in code]
for level in range(19):
    folder = overlays / f"level_{level:02d}"
    if not folder.exists():
        continue
    lcode, _ldata = dr.level_image(overlays, level)
    man = json.loads((folder / "manifest.json").read_text())
    lblobs = [(r["address"], (folder / f"{r['name']}.bin").read_bytes()) for r in man["records"]
              if r["name"] not in ("text", "bss") and (folder / f"{r['name']}.bin").exists()]
    out[f"{level:02d}"] = program(lcode, lblobs, dr.DEFAULT_GP, boot_code)

dest = pathlib.Path(__file__).resolve().parent / "xrefs.json.gz"
dest.write_bytes(gzip.compress(json.dumps(out, separators=(",", ":"), sort_keys=True).encode(), mtime=0))
print(f"wrote {dest} ({dest.stat().st_size} bytes): " +
      ", ".join(f"{k} {len(v['calls'])}/{len(v['ptrs'])}/{len(v['words'])}" for k, v in out.items()))
