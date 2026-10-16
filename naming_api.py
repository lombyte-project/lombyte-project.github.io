#!/usr/bin/env python3
"""Write namingdesk.json: the Naming Desk's "Has an idea" and "Ready to move" lists as tasks for an AI.

Reads the same bundles the page reads (naming-data/t), so the page and the file agree. Each task
says what to change, how sure the evidence is, where the name is written and which steps
(`recipes`, from the game's docs/code-cleanup.md) apply. Names already sent as an open `naming` issue are marked.

usage: naming_api.py SITE_DIR   (writes SITE_DIR/namingdesk.json)
"""
import base64
import datetime
import gzip
import json
import os
import pathlib
import re
import sys
import urllib.request

SITE = "https://lombyte-project.github.io/"
RESIDENT_END = 0x15EF00  # levels load their own code and data from here up (game docs/overlays.md)
KW = set("if else for while do return switch case break continue default goto sizeof static extern const "
         "volatile struct union typedef enum inline register".split())
TY = set("void int char short long float double unsigned signed u8 u16 u32 u64 s8 s16 s32 s64 f32 f64 b32 "
         "vs8 vu8 vs16 vu16 vs32 vu32 vs64 vu64".split())
PLACEHOLDER = re.compile(r"^(fun|func|d|sub)_(l\d\d_)?[0-9a-f]", re.I)

# How much each kind of evidence proves the new name (0..1). An AI applies a task on its own
# only from HIGH up; lower ones need a check in the game first.
HIGH, MEDIUM = 0.8, 0.5
EVIDENCE = {
    "named_elsewhere": (0.95, "Another file already gives this symbol a C name; use the same one."),
    "named_differently": (0.6, "The header, or most files, call this symbol another name; check it fits here too."),
    "boot_match": (0.9, "Same code as a named boot function."),
    "moby_class_table": (0.8, "The level's moby class table points here: <class>_update."),
    "recovered_names": (0.7, "Name proposal from recovered names, not checked in the game."),
    "cheat_clue": (0.3, "A public cheat code writes here. A clue for a name, not a name."),
    "one_reader": (0.9, "Every access in the game code is in one C file, so it can move there."),
}
# Steps from the game's docs/code-cleanup.md; a task lists the keys it needs in `recipe`.
RECIPES = {
    "rename": ["Rename `{old}` to `{new}` everywhere `git grep -nw {old} -- src include` finds it.",
               "Keep the linker symbol: every declaration gets `__asm__(\"{asm}\")`, so the build does not change.",
               "Leave `INCLUDE_ASM` lines as they are: they name the assembly file."],
    "rename_tool": ["`python3 scripts/rename-function-proposals.py --address 0x<last 8 digits of {asm}> --diff` "
                    "shows the change from recovered_names.json; run it again with `--apply`."],
    "declare_once": ["Declare it once, in a header, and drop the other extern lines."],
    "move_data": ["Check that no assembly reads it: `python3 scripts/data-refs.py`. If more code reads it, keep it in assembly.",
                  "`python3 scripts/data-refs.py --emit 0x<last 8 digits of {asm}> --into {move_to}` writes the definition after "
                  "the file's functions and its entry in `configure.py`.",
                  "Drop it from `config/us/symbol_addrs.txt` if it is listed there."],
    "move_level": ["Check that no assembly reads it: `python3 scripts/data-refs.py --level {level_number} --overlays DIR`. "
                   "If more code reads it, keep it in assembly.",
                   "`python3 scripts/data-refs.py --level {level_number} --overlays DIR --emit 0x<last 8 digits of {asm}> "
                   "--into {move_to}` writes the definition after the file's functions and its row in "
                   "`config/overlays/us/data.tsv`.",
                   "Drop it from `config/us/symbol_addrs.txt` if it is listed there."],
    "move_other": ["Check that no assembly reads it: `python3 scripts/data-refs.py`. If more code reads it, keep it in assembly.",
                   "Define it in {move_to} after the file's functions with the values `python3 scripts/data-refs.py` prints, "
                   "and add its entry to the matching `*_OVERLAYS` table in `configure.py` (`--emit` covers `.data` only).",
                   "Drop it from `config/us/symbol_addrs.txt` if it is listed there."],
    "check_build": ["`make elf` must end with PASS: the output stays byte-identical."],
    "check_build_levels": ["`make elf` and `make overlays` must end with PASS: the output stays byte-identical."],
    "investigate": ["No name yet: check in PCSX2 what the cheat changes, then suggest a name on the Naming Desk."],
}
RAW_TYPES = {"u8", "s8", "unsigned char", "signed char", "u128", "s128"}
GUIDE = [
    "Read the game repo's docs/code-cleanup.md first; its rules win over this file.",
    "This file is a snapshot of the commit in `source.commit`. Check each task on the latest main with "
    "`git grep -nw <old> -- src include` before editing: places may have moved or been renamed already.",
    "Apply on your own only tasks with `level: high` and no `suggested` issue. For medium and low, "
    "say what you would change and why, and leave the code as it is.",
    "`ready_to_move`: move the data into its only reader (`move_to`); a move keeps the name unless "
    "the task also gives a new one.",
    "Do each task's `recipe` steps in order (see `recipes`). If the build does not end with PASS, undo that task.",
    "Use the game's tools as they are. When `data-refs.py --emit` stops (no declaration, no complete size, "
    "a type it cannot write), skip that task: never give an array a size yourself and never write code or raw "
    "bytes as a C array.",
    "One commit per task, title `decomp: rename <old> to <new>` or `decomp: move <old> into <file>`.",
]


def load(path):
    raw = base64.b64decode(path.read_text().strip())
    return json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)


def hx(n, w=8):
    return "0x" + format(n, "X").zfill(w)


def open_issues(repo):
    """Names already sent as open `naming` issues: {old: (issue, new)}. None if GitHub can't be read."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/issues?labels=naming&state=open&per_page=100",
                                 headers={"Accept": "application/vnd.github+json"})
    if os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            issues = json.load(r)
    except Exception as err:  # the file is still useful without it
        print(f"naming_api: open issues not read ({err})", file=sys.stderr)
        return None
    sent = {}
    for issue in issues:
        for block in re.findall(r"```yaml\s*\n([\s\S]*?)```", issue.get("body") or ""):
            y = dict(re.findall(r"^(\w+):\s*(.+)$", block, re.M))
            if y.get("old"):
                old = y["struct"] + "." + y["old"] if y.get("kind") == "field" and y.get("struct") else y["old"]
                sent[old.strip()] = (issue["number"], y.get("new", "").strip())
    return sent


def evidence_of(source):
    if "moby class table" in source:
        return "moby_class_table"
    if "same code as a named boot function" in source:
        return "boot_match"
    return "recovered_names"


def build(site_dir):
    t = site_dir / "naming-data" / "t"
    idx, q = load(t / "_index.txt"), load(t / "_queue.txt")
    alias, addr, defs = idx["alias"], idx["addr"], idx["defs"]
    taken = set(defs) | set(idx["structs"]) | set(addr) | set(idx["macros"])

    def address_of(w):
        a, al = addr.get(w), alias.get(w)
        if a is None and al:
            a = addr.get(al)
        for n in (w, al):
            m = n and re.match(r"^(?:FUN_(?:L\d\d_)?|func_|D_|jtbl_|lbl_)([0-9A-Fa-f]{8})$", n)
            if a is None and m:
                a = int(m.group(1), 16)
        return a

    places = {}
    for a, _size, name, lv in idx["fmap"]:
        if lv:
            places.setdefault(name, []).append((lv, a))

    def where(w):
        pl = sorted(places.get(w) or places.get(alias.get(w)) or [])
        if pl:
            return {"levels": {lv: hx(a) for lv, a in pl}}
        a, m = address_of(w), re.match(r"^(?:FUN|D)_L(\d\d)_", alias.get(w, w))
        if a is None:
            return None
        if m:
            return {"levels": {m.group(1): hx(a)}}
        return {"resident" if a < RESIDENT_END else "boot": hx(a)}

    # The C name the rest of the code gives a symbol (gen.py: the header's, else the most used one).
    rname = {k.lower(): v for k, v in idx.get("rname", {}).items()}

    def real_name(w):
        if re.match(r"^(fun|func|d|sub)_", w, re.I):
            return rname.get(w.lower())
        r = rname.get(alias.get(w, "").lower())  # a name only some files use (vend_rand for scale_game_frames)
        return r if r and r != w and not re.search(r"_(gp|lui|macro|const)$", w) else None

    def free(w, new):
        if not re.match(r"^[A-Za-z_]\w*$", new) or new == w or new in KW or new in TY:
            return False
        same = new in alias and address_of(new) is not None and address_of(new) == address_of(w)
        return same or new not in taken

    data = q.get("data", {})

    def movable(d, w):
        if not d or d[1] or "bss" in d[0] or d[4] != 1 or len(d) < 6 or d[5] != "ok":
            return False
        g = idx["gtype"].get(w) or idx["gtype"].get(alias.get(w))
        if g and re.sub(r"\s+", " ", g[0]).strip() in RAW_TYPES:
            return False  # raw bytes and quadwords stay in assembly (code-cleanup.md); --emit cannot write u128
        m = re.match(r"^level (\d\d)", d[0])
        return d[3][0].startswith(f"src/overlays/l{m.group(1)}/") if m else not d[3][0].startswith("src/overlays/")

    fns = set(idx["fnames"]) | set(idx["afn"])
    tasks = []
    for kind, w, used in q["queue"]:
        if kind == "d":
            continue  # fields have no ideas yet
        d = data.get(w) or data.get(alias.get(w)) if kind == "g" else None
        ev, new = [], None
        rn = real_name(w)
        if rn and free(w, rn):
            new, ev = rn, [("named_differently", f"The rest of the code calls it `{rn}`.") if kind == "n" else
                           ("named_elsewhere", f"`{rn}` is already its C name in another file.")]
        idea = kind == "f" and (q["sugg"].get(w) or q["sugg"].get(alias.get(w)))
        if idea and idea[0] != w and free(w, idea[0]):
            e = evidence_of(idea[1])
            if not new:
                new = idea[0]
            if new == idea[0]:
                ev.append((e, idea[1] + (", " + idea[2] if idea[2] else "")))
        cheats = kind == "g" and (q["cheat"].get(w) or q["cheat"].get(alias.get(w)))
        if cheats:
            ev.append(("cheat_clue", "; ".join(c[0] + (f" (+{c[1]:X})" if c[1] else "") for c in cheats[:6])))
        mv = kind == "g" and movable(d, w)
        if mv:
            ev.append(("one_reader", f"Only {d[3][0]} reads it."))
        if not ev:
            continue
        name_conf = max([EVIDENCE[e][0] for e, _ in ev if e not in ("one_reader", "cheat_clue")] or [0])
        conf = name_conf if new else EVIDENCE["one_reader"][0] if mv else EVIDENCE["cheat_clue"][0]
        status = [s for s, on in (("ready_to_move", mv), ("has_idea", bool(new) or bool(cheats)),
                                  ("named_elsewhere", bool(rn and new == rn and kind != "n")),
                                  ("named_differently", bool(rn and new == rn and kind == "n"))) if on]
        tasks.append({"kind": kind, "w": w, "new": new, "used": used, "d": d, "mv": mv, "ev": ev,
                      "conf": conf, "status": status, "cheats": cheats})

    # Every place each name is written in src/ and include/.
    want, hits = {x["w"] for x in tasks}, {}
    word = re.compile(r"\b\w+\b")
    bundles = {}
    for f in idx["files"]:
        if not f.get("b"):
            continue
        if f["b"] not in bundles:
            bundles[f["b"]] = load(t / (f["b"] + ".txt"))
        src = bundles[f["b"]][f["p"]]
        if not want.intersection(word.findall(src["t"])):
            continue
        for i, line in enumerate(src["t"].split("\n"), 1):
            if re.match(r"\s*INCLUDE_(ASM|RODATA)\(", line):
                continue  # it names the assembly file and keeps the old name
            for w in want.intersection(word.findall(re.sub(r'__asm__\s*\(\s*"[^"]*"\s*\)', "", line))):
                hits.setdefault(w, []).append((f["p"], i))

    sent = open_issues(idx["repo"])
    out = []
    for x in tasks:
        w, new, d, mv = x["w"], x["new"], x["d"], x["mv"]
        link = alias.get(w, w)
        what = "function" if x["kind"] == "f" or (x["kind"] == "n" and w in fns) else "global"
        h = hits.get(w, [])
        lvd = mv and d[0].startswith("level ")
        task = {"action": "rename_and_move" if new and mv else "rename" if new else "move" if mv else "investigate",
                "kind": what, "old": w}
        if new:
            task["new"] = new
        task["confidence"] = round(x["conf"], 2)
        task["level"] = "high" if x["conf"] >= HIGH else "medium" if x["conf"] >= MEDIUM else "low"
        task["status"] = x["status"]
        task["evidence"] = [e for e, _ in x["ev"]]
        notes = [n for e, n in x["ev"] if e in ("cheat_clue", "recovered_names")]
        if notes:
            task["note"] = "; ".join(notes)
        tool = "recovered_names" in task["evidence"] and what == "function"
        recipe = ((["rename_tool"] if tool else ["rename"]) + (["declare_once"] if what == "global" else []) if new else []) + (
            ["move_level" if lvd else "move_data" if d[0].endswith("data") else "move_other"] if mv else [])
        task["recipe"] = recipe + (["check_build_levels" if lvd or any(p.startswith("src/overlays/") for p, _ in h)
                                    else "check_build"] if recipe else ["investigate"])
        if link != w:
            task["asm"] = link
        if mv:
            task["move_to"] = d[3][0]
        if lvd:
            task["level_number"] = int(d[0][6:])
        if w in defs:
            task["declared"] = f"{defs[w][0]}:{defs[w][1]}"
        wa = not recipe and where(w)  # an address only helps a check in the game
        if wa:
            task["address"] = wa
        if d:
            g = idx["gtype"].get(w) or idx["gtype"].get(link)
            task["data"] = {k: v for k, v in (("section", d[0]), ("size", d[2]), ("type", g and g[0]),
                                              ("in_c", bool(d[1]))) if v is not None}
            if not mv:
                task["data"]["readers"] = d[3]
        files = {}
        for p, ln in h:
            files.setdefault(p, []).append(ln)
        task["files"] = {p: sorted(set(ls)) for p, ls in files.items()}
        if sent and (w in sent or (new and new in sent)):
            n, nw = sent.get(w) or sent[new]
            task["suggested"] = {"issue": n, "new": nw or None}
        out.append(((-x["conf"], "suggested" in task, -(d and d[2] or 0), -x["used"], w), task))

    out = [task for _, task in sorted(out, key=lambda o: o[0])]
    count = lambda f: sum(1 for t_ in out if f(t_))
    doc = {
        "api": "lombyte-naming-desk", "version": 1,
        "about": "Names and data worth changing in the Lombyte decompilation, with evidence, best first.",
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "source": {"repo": f"https://github.com/{idx['repo']}", "commit": idx["commit"],
                   "rules": f"https://github.com/{idx['repo']}/blob/main/docs/code-cleanup.md",
                   "issue": f"https://github.com/{idx['repo']}/issues/{{suggested.issue}}",
                   "desk": SITE + "namingdesk#{old}", "open_issues_checked": sent is not None},
        "guide": GUIDE,
        "levels": {"high": f"confidence >= {HIGH}", "medium": f"{MEDIUM} <= confidence < {HIGH}",
                   "low": f"confidence < {MEDIUM}"},
        "evidence": {k: {"confidence": c, "means": m} for k, (c, m) in EVIDENCE.items()},
        "recipes": RECIPES,
        "fields": {
            "action": "rename | move | rename_and_move | investigate (no name yet)",
            "evidence": "keys of `evidence`; `note` holds the cheat codes or the source of a name idea",
            "status": "ready_to_move, has_idea, named_elsewhere: the Desk's list filters",
            "recipe": "keys of `recipes`, in order; fill {old} {new} {asm} {move_to} {level_number} from the task",
            "asm": "the linker symbol when it differs from `old`; else it is `old`",
            "files": "file: lines where `old` is written, at source.commit",
            "address": "investigate only, for PCSX2: resident/boot address, or the address in each level",
            "suggested": "an open naming issue already proposes this; follow the issue, not this task",
        },
        "counts": {"tasks": len(out), "high": count(lambda t_: t_["level"] == "high"),
                   "ready_to_move": count(lambda t_: "ready_to_move" in t_["status"]),
                   "has_idea": count(lambda t_: "has_idea" in t_["status"]),
                   "suggested": count(lambda t_: "suggested" in t_),
                   "still_to_name": sum(1 for k, w, _ in q["queue"] if k != "d")},
        "tasks": out,
    }
    (site_dir / "namingdesk.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    return doc


if __name__ == "__main__":
    d = build(pathlib.Path(sys.argv[1]))
    print(f"namingdesk.json: {d['counts']}")
