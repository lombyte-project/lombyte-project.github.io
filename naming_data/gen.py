#!/usr/bin/env python3
"""Build the Naming Desk data (naming-data/) from a checkout of the game repository.

Run from the game checkout (its main branch). It reads src/ and include/, the config tables and
the progress report, plus fixed tables kept next to this script: refs.json.gz (which retail
code touches each data address, see make_refs.py), xrefs.json.gz (where it calls or takes the
address of code, see make_xrefs.py) and cheats.json (public cheat codes per address). The output is what site/namingdesk.html reads:

  t/_index.txt   files, names, symbols, struct layouts, function map
  t/_queue.txt   names still to give, ideas for them, data facts, cheat clues
  t/_xref.txt    callers, address takers and callees of each function (xrefs.json.gz)
  t/<bundle>.txt the source text, by folder, with each file's functions
  sym/*.sym      symbol files for PCSX2

Each .txt is base64 of gzipped JSON (plain files from GitHub Pages, no binary types).

usage: gen.py REPORT_JSON OUT_DIR
"""
import base64
import bisect
import collections
import gzip
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from clex import IDENT, blank_preprocessor, drop_attributes, preprocessor_lines, statements, strip  # noqa: E402
from layout import Layouts, parse_decl, type_name  # noqa: E402

REPO = "lombyte-project/lombyte"
BOOT_DELTA = 0x100080 - 0x1000  # report virtual addresses of the executable are file offsets
RESIDENT_END = 0x15EF00  # level data and code start here (game docs/overlays.md)
BUNDLE_MAX = 600_000  # bytes of JSON per source bundle
TOO_BIG = 1_000_000  # files over this are not shown
PLACEHOLDER = re.compile(r"^(?:FUN|func|D|jtbl|lbl)_(?:L(\d\d)_)?([0-9A-Fa-f]{8})(?:_\w+)?$")
SYMBOL_HEX = re.compile(r"^(?:FUN|func|D|jtbl|lbl)_(?:L\d\d_)?([0-9A-Fa-f]{8})$")
UNNAMED = re.compile(r"^(?:fun|func|d|sub)_(?:l\d\d_)?[0-9a-f]", re.I)
FIELD_UNK = re.compile(r"^unk_?[0-9A-Fa-f]+$")
# per-file alias spellings that are no name for anyone (f2334d0_2133a8, ea210_hide, select54, spin_s,
# activate_effect_l08, fast_sin_cf): an address fragment, a level or variant tag, or trailing digits
JUNK_NAME = re.compile(r"(?:^|_)(?!f(?:16|32|64)(?:_|$))(?=[0-9a-f]*[0-9])[0-9a-f]{3,}(?:_|$)"
                       r"|(?:^|_)(?![usfv](?:8|16|32|64|128)$)[a-z]+\d{2,}$|_(?!(?:8|16|32|64|128)$)\d+$"
                       r"|_(?:l\d*|cf|c\d*|alt|[a-wA-W])$", re.I)
INCLUDE_ASM = re.compile(r"INCLUDE_ASM\s*\(\s*\"[^\"]*\"\s*,\s*(\w+)\s*\)")
KEYWORDS = set("if else for while do return switch case break continue default goto sizeof static extern const "
               "volatile struct union typedef enum inline register void int char short long float double "
               "unsigned signed __asm__ __attribute__ __typeof__".split())


def junk(name):
    """True for a junk spelling, also under a data placement suffix (visited52_lui)."""
    return bool(JUNK_NAME.search(re.sub(r"_(gp|lui|macro|const)$", "", name)))


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def pack(obj):
    return base64.b64encode(gzip.compress(json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode(),
                                          compresslevel=9, mtime=0)).decode()


def bundle_key(path):
    parts = path.split("/")
    if parts[0] == "include":
        return "include" if len(parts) == 2 else "include-" + parts[1]
    if len(parts) == 2:
        return "src"
    if parts[1] == "overlays":
        return "src-overlays" if len(parts) == 3 else "src-overlays-" + parts[2]
    return "src-" + parts[1]


def canon(w):
    """('f' or 'd', level, address) of a placeholder name, whatever its spelling, else None."""
    m = re.match(r"^(FUN|func|D)_(?:L(\d\d)_)?([0-9A-Fa-f]{8})$", w or "")
    if not m:
        return None
    return ("d" if m.group(1) == "D" else "f", m.group(2) or "", m.group(3).lower())


def snake(name):
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    return re.sub(r"_+", "_", s).lower().strip("_")


def symbol_files(game):
    """name -> address from the linker symbol files."""
    out = {}
    for f in ("config/us/symbol_addrs.txt", "config/us/undefined_syms.txt", "config/us/undefined_funcs_auto.txt"):
        p = game / f
        if p.exists():
            for m in re.finditer(r"^\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+)", p.read_text(), re.M):
                out.setdefault(m.group(1), int(m.group(2), 16))
    return out


class Tree:
    """Everything the Desk knows from src/ and include/."""

    def __init__(self, game, report):
        self.game = game
        self.files = []  # (path, text or None, reason)
        self.lay = Layouts()
        self.alias, self.defs, self.macros = {}, {}, {}
        self.alias_files = collections.defaultdict(set)  # C name -> files that declare it for a symbol
        self.bound = collections.defaultdict(lambda: collections.defaultdict(set))  # C name -> label -> files
        self.fnames, self.afn, self.infra, self.typedef_names = set(), set(), set(), set()
        self.gtype, self.decl_line, self.fn_meta = {}, {}, {}
        self.words = collections.Counter()  # word -> occurrences over the tree
        self.word_files = collections.defaultdict(set)
        self.c_words = collections.Counter()  # the same, without INCLUDE_ASM lines (they name the assembly file)
        self.funcs = {}  # path -> [{n,s,e,k,(p)}]
        self.fuzzy = {}
        for u in report.get("units", []):
            for f in u.get("functions", []):
                if f.get("fuzzy_match_percent") is not None:
                    self.fuzzy.setdefault(f["name"], float(f["fuzzy_match_percent"]))

    # --- reading -------------------------------------------------------------
    def read(self):
        paths = git("-C", str(self.game), "ls-files", "src", "include").split("\n")
        for path in sorted(p for p in paths if p):
            raw = (self.game / path).read_bytes()
            if b"\0" in raw:
                self.files.append((path, None, "bin"))
                continue
            text = raw.decode("utf-8", errors="replace")
            self.files.append((path, text, "big" if len(raw) > TOO_BIG else ""))
        for path, text, _ in self.files:
            if text is not None and path.endswith((".c", ".h")):
                self.scan(path, text)
        self.finish()

    def scan(self, path, text):
        code = strip(text)
        for w in re.findall(r"\b[A-Za-z_]\w*\b", code):
            self.words[w] += 1
        for w in set(re.findall(r"\b[A-Za-z_]\w*\b", code)):
            self.word_files[w].add(path)
        self.c_words.update(re.findall(r"\b[A-Za-z_]\w*\b", re.sub(r"(?m)^\s*INCLUDE_(ASM|RODATA)\(.*$", "", code)))
        top_infra = path.startswith("include/") and path.count("/") == 1
        for line, d in preprocessor_lines(code):
            m = re.match(r"#\s*define\s+([A-Za-z_]\w*)(\(?)\s*(.*)", d)
            if m:
                name, value = m.group(1), m.group(3).strip()
                self.macros.setdefault(name, [path, line])
                if top_infra or re.fullmatch(r"LOMBYTE_\w+_H", name):
                    self.infra.add(name)
                if not m.group(2):
                    v = re.sub(r"(?<=[0-9a-fA-FxX])[uUlL]+\b", "", value.strip("() "))
                    if re.fullmatch(r"-?(0[xX][0-9a-fA-F]+|\d+)", v):
                        self.lay.consts.setdefault(name, int(v, 16) if v.lower().lstrip('-').startswith('0x') else int(v, 8 if re.fullmatch(r'-?0\d+', v) else 10))
        flat = blank_preprocessor(code)
        defs_here = []
        for st in statements(flat):
            if st["kind"] == "decl":
                self.declaration(path, st["text"], st["line"], top_infra)
            else:
                d = self.body(path, st, top_infra)
                if d:
                    defs_here.append(d)
        if path.endswith(".c"):
            self.functions(path, code, defs_here)

    def at(self, text, start_line, name):
        """Line of name inside text, where text's first non-blank character is on start_line."""
        lead = len(text) - len(text.lstrip())
        m = re.search(r"\b%s\b" % re.escape(name), text)
        return start_line + (text[lead:m.start()].count("\n") if m and m.start() >= lead else 0)

    def declaration(self, path, text, line, infra):
        s = text.strip()
        if not s or re.match(r"(INCLUDE_ASM|INCLUDE_RODATA|__asm__|asm)\b", s):
            return
        words, decls, asm, attrs = parse_decl(text)
        alias_to = next((m.group(1) for a in attrs for m in [re.search(r"alias\s*\(\s*\"(\w+)\"", a)] if m), None)
        aligned = max([int(a.group(1), 0) for x in attrs for a in [re.search(r"aligned\s*\(\s*(\w+)\s*\)", x)] if a] or [0])
        for i, d in enumerate(decls):
            if d.name in KEYWORDS:
                continue
            if "typedef" in words:
                mode = next((m.group(1) for a in attrs for m in [re.search(r"mode\s*\(\s*(\w+)\s*\)", a)] if m), None)
                base = {"TI": "__int128", "DI": "long long", "SI": "int", "HI": "short", "QI": "char"}.get(mode, d.base)
                self.lay.typedefs.setdefault(d.name, (base, d.ptr if not (d.func or d.fptr) else 1, d.dims, aligned, path, line))
                if not re.match(r"(struct|union)\b", d.base) or d.ptr or d.dims:
                    self.typedef_names.add(d.name)
                else:
                    self.name_record(d.name, type_name(d.base), path)
                continue
            target = (asm if i == 0 else None) or (alias_to if i == 0 else None)
            if target and target != d.name:
                self.note_alias(d.name, target, path)
            ln = self.at(text, line, d.name)
            if d.func:
                (self.afn if target else self.fnames).add(d.name)
                if infra:
                    self.infra.add(d.name)
                continue
            self.var(d, words, path, ln)

    def note_alias(self, name, target, path):
        self.alias.setdefault(name, target)
        self.bound[name][target].add(path)
        if self.alias[name] == target:
            self.alias_files[name].add(path)

    def real_names(self):
        """The name each placeholder symbol goes by: a plain name (not a per-file variant like
        frame_time_2080b0 or strlen_alt), the one a header declares, else the one most files use."""
        variant = re.compile(r"_(gp|lui|macro|const|alt|c\d+|[a-z]|[0-9a-f]{4,})$", re.I)
        best, spellings = {}, collections.defaultdict(set)  # one symbol, any spelling (func_001204B8, FUN_001204b8)
        for w in list(self.words) + [t for ts in self.bound.values() for t in ts]:
            spellings[canon(w) or w].add(w)
        # every label a name is bound to counts (hero_set_state is each level's copy)
        for c, t, fs in ((c, t, fs) for c, ts in self.bound.items() for t, fs in ts.items()):
            if UNNAMED.match(c) or c in KEYWORDS or junk(c):
                continue
            key = (bool(variant.search(c)), not any(f.startswith("include/") for f in fs), -len(fs), len(c), c)
            g = canon(t) or t
            if g not in best or key < best[g][0]:
                best[g] = (key, c)
        return {w: c for g, (_k, c) in best.items() for w in sorted(spellings[g] | ({g} if isinstance(g, str) else set()))}

    def var(self, d, words, path, line):
        if d.base.startswith("__typeof__"):
            return
        key = d.name
        if key not in self.gtype or (self.gtype[key][1] is None and not d.ptr):
            self.gtype[key] = [d, path, line]
        self.decl_line.setdefault(key, [path, line])
        if "extern" not in words:
            self.defs.setdefault(key, [path, line])

    def body(self, path, st, infra):
        head = st["head"]
        hs = head.strip()
        m = re.search(r"\b(struct|union|enum)\s*(\w*)\s*$", hs)
        if m and "(" not in hs:
            kind, tag = m.group(1), m.group(2) or None
            words = hs[:m.start()].split()
            if kind == "enum":
                self.enum(st["body"], tag)
                key = tag
            else:
                key = self.lay.add_record(kind, tag, st["body"], path, self.at(head, st["line"], kind), st["body_line"])
            tail = st["tail"]
            if tail.strip():
                ws, decls, asm, attrs = parse_decl((kind + " " + (key or "int") if kind != "enum" else "int") + " " + tail)
                for i, d in enumerate(decls):
                    if "typedef" in words:
                        if kind == "enum":
                            self.lay.enums.add(d.name)
                            self.typedef_names.add(d.name)
                        elif d.ptr or d.dims:
                            self.lay.typedefs.setdefault(d.name, (d.base, d.ptr, d.dims, 0, path, st["line"]))
                            self.typedef_names.add(d.name)
                        else:
                            self.name_record(d.name, key, path)
                    else:
                        if asm and i == 0:
                            self.note_alias(d.name, asm, path)
                        self.var(d, set(words), path, st["end"])
            return None
        if "=" in hs:
            words, decls, asm, attrs = parse_decl(hs.split("=")[0])
            if decls and not decls[0].func:
                if asm:
                    self.note_alias(decls[0].name, asm, path)
                self.var(decls[0], words, path, self.at(head, st["line"], decls[0].name))
            return None
        words, decls, asm, attrs = parse_decl(hs)
        if not decls or not decls[0].func or decls[0].name in KEYWORDS:
            return None
        d = decls[0]
        line = self.at(head, st["line"], d.name)
        self.local_records(path, st["body"], st["body_line"])
        self.fnames.add(d.name)
        self.defs.setdefault(d.name, [path, line])
        if asm and asm != d.name:
            self.note_alias(d.name, asm, path)
        if infra:
            self.infra.add(d.name)
        return {"n": d.name, "s": line, "e": st["end"]}

    def local_records(self, path, body, body_line):
        """Structs and unions a function defines for itself (top level of its body only)."""
        i = 0
        rx = re.compile(r"(\btypedef\s+)?\b(struct|union)\s*(\w*)\s*\{")
        while True:
            m = rx.search(body, i)
            if not m:
                return
            j = body.index("{", m.start(2))
            from clex import match_paren
            end = match_paren(body, j, "{", "}")
            line = body_line + body[:m.start(2)].count("\n")
            key = self.lay.add_record(m.group(2), m.group(3) or None, body[j + 1:end - 1], path, line,
                                      body_line + body[:j].count("\n"))
            tail = re.match(r"[^;{}]*", body[end:]).group(0)
            if m.group(1):
                for name in re.findall(r"^\s*(\w+)\s*$", tail):
                    self.name_record(name, key, path)
            i = end

    def enum(self, body, tag):
        if tag:
            self.lay.enums.add(tag)
        v = -1
        for part in body.split(","):
            m = re.match(r"\s*([A-Za-z_]\w*)\s*(?:=\s*(.+))?$", part.strip(), re.S)
            if not m:
                continue
            if m.group(2):
                from layout import evaluate
                x = evaluate(m.group(2), self.lay.consts)
                v = x if x is not None else v + 1
            else:
                v += 1
            self.lay.consts.setdefault(m.group(1), v)

    # --- functions in one C file --------------------------------------------
    def functions(self, path, code, defs_here):
        blocks, stack = [], []
        for line, d in preprocessor_lines(code):
            m = re.match(r"#\s*(if|ifdef|ifndef|elif|else|endif)\b\s*(.*)", d)
            if not m:
                continue
            k, cond = m.group(1), m.group(2)
            if k in ("if", "ifdef", "ifndef"):
                nm = None
                if k == "ifndef" and re.match(r"NON_MATCHING\b", cond) or k == "if" and re.match(r"!\s*defined\s*\(?\s*NON_MATCHING", cond):
                    nm = "asm_first"
                elif k == "ifdef" and re.match(r"NON_MATCHING\b", cond) or k == "if" and re.match(r"defined\s*\(?\s*NON_MATCHING", cond):
                    nm = "c_first"
                stack.append({"s": line, "nm": nm, "else": None})
            elif k in ("else", "elif") and stack:
                stack[-1]["else"] = stack[-1]["else"] or line
            elif k == "endif" and stack:
                b = stack.pop()
                b["e"] = line
                if b["nm"]:
                    blocks.append(b)
        lines = code.split("\n")
        asm_at = [(i + 1, m.group(1)) for i, l in enumerate(lines) for m in [INCLUDE_ASM.search(l)] if m]
        asm_line = {n: ln for ln, n in reversed(asm_at)}
        out, used_defs, used_asm = [], set(), set()
        for b in blocks:
            mid = b["else"] or b["e"]
            asm_part = (b["s"], mid) if b["nm"] == "asm_first" else (mid, b["e"])
            c_part = (mid, b["e"]) if b["nm"] == "asm_first" else (b["s"], mid)
            names = [n for ln, n in asm_at if asm_part[0] < ln < asm_part[1]]
            bodies = [d for d in defs_here if c_part[0] < d["s"] < c_part[1]]
            for d in bodies:
                used_defs.add(d["s"])
            for ln, n in asm_at:
                if asm_part[0] < ln < asm_part[1]:
                    used_asm.add(ln)
            for n in names:
                x = {"n": n, "s": b["s"], "e": b["e"], "k": "draft" if bodies else "asm"}
                fz = [self.fuzzy.get(n)] + [self.fuzzy.get(d["n"]) for d in bodies]
                fz = [v for v in fz if v is not None and 0 < v < 100]
                if fz and bodies:
                    x["p"] = round(max(fz), 1)
                out.append(x)
        for ln, n in asm_at:
            if ln not in used_asm:
                out.append({"n": n, "s": ln, "e": ln, "k": "asm"})
        for d in defs_here:
            if d["s"] in used_defs:
                continue
            fz = self.fuzzy.get(d["n"])
            if fz is None and d["n"] in self.alias:
                fz = self.fuzzy.get(self.alias[d["n"]])
            x = dict(d, k="exact")
            if fz is not None and fz < 100:
                x["k"] = "draft"
                if fz > 0:
                    x["p"] = round(fz, 1)
            out.append(x)
        for x in out:
            if x["k"] != "exact" or x["n"] not in self.fn_meta:
                self.fn_meta.setdefault(x["n"], (path, x["k"]))
            self.fnames.add(x["n"])
            if x["n"] not in self.defs and x["k"] != "exact":
                self.defs.setdefault(x["n"], [path, asm_line.get(x["n"], x["s"])])
        out.sort(key=lambda x: (x["s"], x["e"]))
        self.funcs[path] = out

    # --- after every file -----------------------------------------------------
    def name_record(self, name, key, path):
        """`typedef struct key name;`: the record is known by name in this file (and after)."""
        rec = self.lay.records.get(self.lay.resolve(key, path) or "")
        if not rec:
            self.typedef_names.add(name)  # a struct nobody defines: a plain type name
            return
        if name in self.lay.records and self.lay.records[name] is not rec:
            if self.lay.records[name]["p"] == path:
                return
            name = "%s@%s" % (name, path)
        if name not in self.lay.records:
            self.lay.records[name] = rec
            self.lay.order.append(name)

    def finish(self):
        self.afn -= self.fnames

    def structs(self):
        out = {}
        for key in self.lay.order:
            if key.startswith("__anon_"):
                continue
            lay = self.lay.layout(key)
            if lay:
                out[key] = {"p": lay["p"], "l": lay["l"], "z": lay["z"], "f": lay["f"]}
        return out

    def gtypes(self):
        out = {}
        for name, (d, path, line) in self.gtype.items():
            base = type_name(d.base)
            if d.ptr:
                out[name] = [base + " *", 4, 1 if d.dims else 0]
                continue
            z, _a = self.lay.base_size(d.base, path)
            out[name] = [base, z, 1 if d.dims else 0]
        return out


def main():
    report_path, out_dir = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    game = pathlib.Path.cwd()
    report = json.loads(report_path.read_text())
    tree = Tree(game, report)
    tree.read()
    commit = git("-C", str(game), "rev-parse", "HEAD").strip()

    # --- addresses ---------------------------------------------------------------
    addr = symbol_files(game)
    fmap = []
    for u in report.get("units", []):
        if u["name"].split("/")[0] not in ("game", "sdk"):
            continue
        for f in u.get("functions", []):
            va = f.get("metadata", {}).get("virtual_address")
            if va is None:
                continue
            a = int(va) + BOOT_DELTA
            fmap.append([a, int(f["size"]), f["name"], ""])
            addr.setdefault(f["name"], a)
    boot_name_at = {a: n for a, _z, n, _l in fmap}
    for w in tree.words:
        m = SYMBOL_HEX.match(w)
        if m:
            addr.setdefault(w, int(m.group(1), 16))

    # The C names the code gives placeholder symbols, keyed by canon(): a function body's own
    # name first, then any other declaration's name. Boot functions also carry the report's name.
    defined_c, named = {}, set()
    for c, target in tree.alias.items():
        if c in tree.defs and not UNNAMED.match(c) and canon(target):
            defined_c.setdefault(canon(target), c)
    for a, _z, n, _l in fmap:
        if not UNNAMED.match(n):
            named.add(("f", "", format(a, "08x")))
    named |= set(defined_c)
    for c, target in tree.alias.items():  # a global is named once any declaration names it
        if canon(target) and canon(target)[0] == "d" and not UNNAMED.match(c) and c not in KEYWORDS:
            named.add(canon(target))
    for n, a in symbol_files(game).items():  # real names the linker files give an address
        if not UNNAMED.match(n):
            named.add(("f", "", format(a, "08x")))
            named.add(("d", "", format(a, "08x")))

    levels, places_tsv = {}, []
    ftsv = game / "config/overlays/us/functions.tsv"
    for line in ftsv.read_text().splitlines() if ftsv.exists() else []:
        f = line.split("\t")
        if line.startswith("#") or len(f) < 6:
            continue
        name, kind, size = f[0], f[1], int(f[2])
        pl = [p.split(":") for p in f[5].split(",") if ":" in p]
        levels[name] = ",".join(sorted(lv for lv, _ in pl))
        shown = defined_c.get(canon(name))
        if not shown and kind == "exe":
            m = SYMBOL_HEX.match(name)
            shown = m and boot_name_at.get(int(m.group(1), 16))
        for lv, a in pl:
            fmap.append([int(a, 16), size, shown or name, lv])
    fmap.sort(key=lambda x: (x[3], x[0]))

    files_out, bundles = [], collections.defaultdict(dict)
    for path, text, why in tree.files:
        entry = {"p": path, "z": len(text.encode()) if text is not None else (tree.game / path).stat().st_size}
        fs = tree.funcs.get(path)
        if fs:
            c = collections.Counter(x["k"] for x in fs)
            entry["st"] = [c["exact"], c["draft"], c["asm"]]
        if text is not None and not why:
            item = {"t": text}
            if fs:
                item["f"] = fs
            bundles[bundle_key(path)][path] = item
        elif why == "bin":
            entry["bin"] = 1
        files_out.append(entry)

    # split big bundles in path order
    names = {}
    out_bundles = {}
    for key, items in bundles.items():
        parts, cur, size = [], {}, 0
        for p in sorted(items):
            z = len(json.dumps({p: items[p]}, ensure_ascii=False).encode())
            if cur and size + z > BUNDLE_MAX:
                parts.append(cur)
                cur, size = {}, 0
            cur[p] = items[p]
            size += z
        parts.append(cur)
        for i, part in enumerate(parts):
            k = key if len(parts) == 1 else f"{key}-{i + 1}"
            out_bundles[k] = part
            for p in part:
                names[p] = k
    for e in files_out:
        if e["p"] in names:
            e["b"] = names[e["p"]]

    structs = tree.structs()
    gtype = tree.gtypes()
    index = {
        "repo": REPO, "commit": commit, "files": files_out,
        "fnames": sorted(tree.fnames), "afn": sorted(tree.afn),
        "macros": tree.macros, "infra": sorted(tree.infra), "gtype": gtype, "structs": structs,
        "typedefs": sorted(tree.typedef_names - set(structs)),
        "defs": tree.defs, "addr": addr, "alias": tree.alias, "rname": tree.real_names(), "levels": levels, "fmap": fmap,
    }

    queue = build_queue(game, tree, index, named)
    out = out_dir / "t"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.txt"):
        old.unlink()
    (out / "_index.txt").write_text(pack(index))
    (out / "_queue.txt").write_text(pack(queue))
    for k, part in out_bundles.items():
        (out / f"{k}.txt").write_text(pack(part))
    xrefs = build_xrefs(index)
    if xrefs is not None:
        (out / "_xref.txt").write_text(pack(xrefs))
    write_syms(out_dir / "sym", index, tree)
    print(f"naming-data: {len(files_out)} files in {len(out_bundles)} bundles, {len(structs)} structs, "
          f"{len(queue['queue'])} names to give, {len(queue['data'])} data facts, commit {commit[:10]}")


def build_xrefs(index):
    """Per function name, who calls it and where its address is kept (xrefs.json.gz).

    {name: {"c": [[caller, caller copy, callee copy, levels, [call offsets in the caller]]],
            "p": [[holder, "d", levels, offset in the table, callee copy] or
                  [function, "c", levels, [offsets], callee copy, function copy]]}}
    Levels are 'boot' or two-digit numbers joined by ','. A copy is the index of an identical
    function among the same name's addresses in one level (functions.tsv merges identical code);
    offsets are relative to the function's start, so the page finds the address in any level."""
    path = HERE / "xrefs.json.gz"
    if not path.exists():
        return None
    xr = json.loads(gzip.decompress(path.read_bytes()))
    progs = collections.defaultdict(list)
    for a, z, n, lv in index["fmap"]:
        progs[lv or "boot"].append((a, z, n))
    data_at = collections.defaultdict(list)
    for n, a in index["addr"].items():
        m = re.match(r"^D_L(\d\d)_", n)
        if n.startswith("D_") or m:
            data_at[m.group(1) if m else "boot"].append((a, n))

    def finder(lv):
        # a level replaces the start-up code above LEVEL_DATA_START; only the resident part stays
        rows = sorted(progs.get(lv, []) + ([r for r in progs["boot"] if r[0] < 0x15EF00] if lv != "boot" else []))
        starts = [r[0] for r in rows]
        copies = collections.defaultdict(list)
        for r in rows:
            copies[r[2]].append(r[0])
        exact = {r[0]: (r[2], copies[r[2]].index(r[0])) for r in rows}

        def inside(a):
            i = bisect.bisect_right(starts, a) - 1
            if i < 0 or a >= rows[i][0] + max(rows[i][1], 4):
                return None
            return rows[i][2], copies[rows[i][2]].index(rows[i][0]), a - rows[i][0]
        return exact, inside

    calls = collections.defaultdict(lambda: collections.defaultdict(set))  # callee -> key -> levels
    held = collections.defaultdict(lambda: collections.defaultdict(set))
    for lv, prog in xr.items():
        exact, inside = finder(lv)
        sites = collections.defaultdict(list)
        for insn, target, _tail in prog["calls"]:
            t, c = exact.get(target), inside(insn)
            if t and c and (c[0], c[1]) != t:
                sites[(t, c[0], c[1])].append(c[2])
        for (t, cn, cc), offs in sites.items():
            calls[t[0]][(cn, cc, t[1], tuple(sorted(offs)))].add(lv)
        sites = collections.defaultdict(list)
        for insn, target in prog["ptrs"]:
            t, c = exact.get(target), inside(insn)
            if t and c and (c[0], c[1]) != t:
                sites[(t, c[0], c[1])].append(c[2])
        for (t, cn, cc), offs in sites.items():
            held[t[0]][(cn, "c", tuple(sorted(offs)), t[1], cc)].add(lv)
        # a level keeps the executable's data names only below its own data (LEVEL_DATA_START)
        dl = sorted(data_at.get(lv, []) + [d for d in data_at["boot"] if lv == "boot" or d[0] < 0x15EF00])
        dstarts = [d[0] for d in dl]
        for at, target in prog["words"]:
            t = exact.get(target)
            i = bisect.bisect_right(dstarts, at) - 1
            if t and i >= 0:
                held[t[0]][(dl[i][1], "d", at - dl[i][0], t[1])].add(lv)

    def lvs(s):
        return ",".join(sorted(s, key=lambda x: (x != "boot", x)))
    out = {}
    for name in sorted(set(calls) | set(held)):
        c = [[cn, cc, tc, lvs(l), list(offs)] for (cn, cc, tc, offs), l in calls.get(name, {}).items()]
        c.sort(key=lambda r: (-len(r[4]), r[0]))
        p = []
        for k, l in held.get(name, {}).items():
            p.append([k[0], "d", lvs(l), k[2], k[3]] if k[1] == "d" else [k[0], "c", lvs(l), list(k[2]), k[3], k[4]])
        p.sort(key=lambda r: (r[1], r[0]))
        out[name] = {"c": c, "p": p}
    return out


# --- the queue ---------------------------------------------------------------------
def build_queue(game, tree, index, named):
    alias, addr = index["alias"], index["addr"]
    refs_path = HERE / "refs.json.gz"
    refs = json.loads(gzip.decompress(refs_path.read_bytes())) if refs_path.exists() else None
    cheats_path = HERE / "cheats.json"
    cheats = json.loads(cheats_path.read_text()) if cheats_path.exists() else {}

    def address_of(w):
        a = addr.get(w)
        if a is None and w in alias:
            a = addr.get(alias[w])
        for n in (w, alias.get(w)):
            m = n and SYMBOL_HEX.match(n)
            if a is None and m:
                a = int(m.group(1), 16)
        return a

    def still_unnamed(w):
        return canon(w) is not None and canon(w) not in named

    queue = []
    for w, n in tree.c_words.items():  # a name only an INCLUDE_ASM line uses has nothing to rename in C
        m = PLACEHOLDER.match(w)
        if not m or w != m.group(0) or not SYMBOL_HEX.match(w):
            continue
        if not still_unnamed(w):
            continue
        if w.startswith(("FUN_", "func_")):
            if w in tree.fnames or w in tree.afn or w in index["levels"] or w in index["addr"]:
                queue.append(["f", w, n])
        elif w.startswith("D_"):
            queue.append(["g", w, n])

    # struct fields still called unkXX: uses of .unkXX / ->unkXX in the files that name the struct
    member_use = collections.Counter()
    for path, text, _ in tree.files:
        if text is None:
            continue
        for m in re.finditer(r"(?:\.|->)\s*(unk_?[0-9A-Fa-f]+)\b", text):
            member_use[(path, m.group(1))] += 1
    for key, s in index["structs"].items():
        name = key.split("@")[0]
        where = tree.word_files.get(name, set()) | {s["p"]}
        for f in s["f"]:
            if FIELD_UNK.match(f[0] or ""):
                n = 1 + sum(member_use[(p, f[0])] for p in where)
                queue.append(["d", f"{key}.{f[0]}", n])
    # names that differ from what the rest of the code calls the same symbol (vend_rand for scale_game_frames);
    # the sda/lui/macro/const forms are deliberate data aliases (game docs/code-cleanup.md) and stay
    rname = index["rname"]
    texts = {p: strip(t) for p, t, _ in tree.files if t is not None and p.endswith((".c", ".h"))}
    for c, t in sorted(alias.items()):
        if rname.get(t, c) != c and not UNNAMED.match(c) and c not in KEYWORDS \
                and not re.search(r"_(gp|lui|macro|const)$", c) and tree.c_words.get(c):
            # counted only in the files that declare it: `section` is also an attribute elsewhere
            n = sum(len(re.findall(r"\b%s\b" % c, texts[f])) for f in tree.alias_files[c] if f in texts)
            queue.append(["n", c, n or tree.c_words[c]])
    queue.sort(key=lambda x: (-x[2], x[0], x[1]))

    # ideas: proposals in the game's tables, for every spelling of the placeholder in the code
    spelled = collections.defaultdict(set)
    for w in tree.words:
        if canon(w):
            spelled[canon(w)].add(w)
    sugg = {}
    rn = game / "config/us/recovered_names.json"
    if rn.exists():
        for e in json.loads(rn.read_text()).get("rename_proposals", {}).get("entries", []):
            cur, new = e.get("current_name"), e.get("proposed_name")
            here = spelled.get(canon(cur)) if canon(cur) else None
            if here and new and still_unnamed(cur) and not junk(new):
                for w in here:
                    sugg.setdefault(w, [new, "name proposal (recovered_names.json)", ""])
    names_dir = game / "config/overlays/us/names"
    lvl_names = {}
    for f in sorted(names_dir.glob("level-*.json")) if names_dir.exists() else []:
        lv = f.stem.split("-")[1]
        for e in json.loads(f.read_text()).get("functions", []):
            ev = e.get("name_evidence") or {}
            if not ev.get("name"):
                continue
            method = ev.get("method", "")
            why = ("moby class table" if "moby" in method else
                   "same code as a named boot function" if "boot" in method else method.replace("-", " "))
            raw = ev["name"].split("__")[0] if "moby" not in method else ev["name"]
            new = re.sub(r"_+", "_", (snake(raw) if "moby" not in method else raw.lower() + "_update"))
            a = e.get("addr")
            lvl_names.setdefault((lv, a), (new, f"level {lv}: {why}"))
    # level functions: the level's address -> the canonical name in functions.tsv
    canon_at = {}
    ftsv = game / "config/overlays/us/functions.tsv"
    for line in ftsv.read_text().splitlines() if ftsv.exists() else []:
        f = line.split("\t")
        if line.startswith("#") or len(f) < 6:
            continue
        for p in f[5].split(","):
            if ":" in p:
                lv, a = p.split(":")
                canon_at[(lv, int(a, 16))] = f[0]
    # one class name on two update functions of a level (rocket in level 13) names neither
    per_level = collections.Counter((lv, new) for (lv, _a), (new, _w) in lvl_names.items())
    for (lv, a), (new, why) in sorted(lvl_names.items()):
        cn = canon_at.get((lv, a))
        if cn and still_unnamed(cn) and new != cn and per_level[(lv, new)] == 1 and not junk(new):
            for w in spelled.get(canon(cn), ()):
                sugg.setdefault(w, [new, why, ""])

    # data: what the code shows about each global
    data, gloc = data_facts(game, tree, index, refs, address_of, still_unnamed)
    return {"queue": queue, "sugg": sugg, "sent": [], "data": data, "gloc": gloc, "cheat": cheats}


def boot_in_c(game):
    """[(start, end)] of executable data that C defines: the *_OVERLAYS tables of configure.py."""
    text = (game / "configure.py").read_text()
    ranges = []
    for table in re.finditer(r"^(\w+_OVERLAYS)\s*=\s*\{(.*?)^\}", text, re.S | re.M):
        for m in re.finditer(r"\(\s*(0x[0-9A-Fa-f]+)\s*,\s*(0x[0-9A-Fa-f]+)\s*,\s*(0x[0-9A-Fa-f]+|\d+)\s*\)", table.group(2)):
            a, z = int(m.group(1), 16), int(m.group(3), 0)
            ranges.append((a, a + z))
    return ranges


def level_in_c(game):
    """name -> size of level data defined in C (config/overlays/us/data.tsv)."""
    out = {}
    p = game / "config/overlays/us/data.tsv"
    for line in p.read_text().splitlines() if p.exists() else []:
        f = line.split("\t")
        if line.startswith("#") or len(f) < 2:
            continue
        try:
            out[f[0]] = int(f[1], 0)
        except ValueError:
            pass
    return out


# u8/s8/u128 globals are byte or quadword stand-ins for a type nobody knows yet: not ready to move
BYTE_TYPES = {"u8", "s8", "u128"}


def data_facts(game, tree, index, refs, address_of, still_unnamed):
    alias = index["alias"]
    in_c_boot = boot_in_c(game)
    in_c_level = level_in_c(game)
    boot_secs = refs["boot"]["sections"] if refs else []
    lvl = {k: v for k, v in (refs or {}).items() if k != "boot"}

    def section_of(a, lv):
        if lv and lv in lvl:
            for name, s, z in lvl[lv]["sections"]:
                if s <= a < s + z:
                    return f"level {lv}", name
        for name, s, z in boot_secs:
            if s <= a < s + z:
                return name, name
        return None, None

    # which file each retail owner lives in now
    unit_file = {}
    for path, _t, _w in tree.files:
        if path.startswith("src/") and path.endswith(".c"):
            unit_file[path[4:-2]] = path
    fn_file = {n: meta[0] for n, meta in tree.fn_meta.items()}
    for c, target in alias.items():
        if c in fn_file:
            fn_file.setdefault(target, fn_file[c])
    ftsv = game / "config/overlays/us/functions.tsv"
    exe_unit = {}
    for line in ftsv.read_text().splitlines() if ftsv.exists() else []:
        f = line.split("\t")
        if not line.startswith("#") and len(f) >= 7 and f[1] == "exe":
            exe_unit[f[0]] = f[6]

    def owner_files(scope, owners):
        out = []
        for o in owners:
            if scope == "boot":
                out.append(unit_file.get(o) or unit_file.get("assembly/" + o) or o)
            else:
                f = fn_file.get(o) or (unit_file.get(exe_unit[o]) if o in exe_unit else None)
                out.append(f or o)
        return out

    # Where each object ends: at the next named address in its program (or its declared size).
    starts = collections.defaultdict(set)
    for n, v in index["addr"].items():
        if v is None or v < 0x100000 or re.match(r"(FUN|func|jtbl|lbl)_", n) or n in tree.fnames or n in tree.afn:
            continue
        m = re.match(r"^D_L(\d\d)_", n)
        starts[m.group(1) if m else "boot"].add(v)
    starts = {k: sorted(v) for k, v in starts.items()}
    keys = {k: sorted(int(x, 16) for x in r["refs"]) for k, r in (refs or {}).items()}

    def refs_of(a, lv, size):
        """Files whose code touches the object at a. Level data: that level's code. Executable data:
        the executable's code and every level's code (above the resident part a level's own data
        sits at the same addresses, so either may be the one meant). None when the tables don't
        cover the address."""
        scope = lv or "boot"
        st = starts.get(scope, [])
        i = bisect.bisect_right(st, a)
        end = st[i] if i < len(st) else a + 1
        if size:
            end = min(end, a + size)
        end = max(end, a + 1)
        r = (refs or {}).get(scope)
        if not r or not any(s <= a < s + z for _n, s, z in r["sections"]):
            return None
        scopes = [scope] + ([k for k in refs if k != "boot"] if scope == "boot" else [])
        files = set()
        for k in scopes:
            ks, rk = keys[k], refs[k]
            for t in ks[bisect.bisect_left(ks, a):bisect.bisect_left(ks, end)]:
                files.update(owner_files(k, [rk["owners"][j] for j in rk["refs"][format(t, "x")]]))
        return sorted(files)

    data, gloc = {}, {}
    candidates = set(index["gtype"]) | {w for w in tree.words if SYMBOL_HEX.match(w) and w.startswith("D_")}
    for w in sorted(candidates):
        if w in tree.fnames or w in tree.afn:
            continue
        a = address_of(w)
        if a is None or a < 0x100000:
            continue
        target = alias.get(w, w)
        m = re.match(r"^D_L(\d\d)_", target)
        lv = m.group(1) if m else None
        label, sec = section_of(a, lv)
        if not label:
            continue
        readers = sorted(p for p in tree.word_files.get(w, ()) if p.endswith(".c"))
        # data.tsv lists level data and the level-shared data C defines; *_OVERLAYS the executable's
        in_c = target in in_c_level or w in in_c_level or (not lv and any(s <= a < e for s, e in in_c_boot))
        size = in_c_level.get(target, in_c_level.get(w))
        row = [label, 1 if in_c else 0, size, readers[:4], len(readers)]
        if len(readers) == 1 and not in_c:
            if "lit" in sec:
                row.append("lit")
            else:
                files = refs_of(a, lv, size)
                if files is not None and not files:
                    row.append("none")
                elif files is not None:
                    others = [f for f in files if f != readers[0]]
                    if others:
                        row.append(others[:6] + ([len(others)] if len(others) > 6 else []))
                    elif (index["gtype"].get(w) or [""])[0] not in BYTE_TYPES:
                        row.append("ok")
        data[w] = row
    for w, at in tree.decl_line.items():
        if w in data or canon(w):
            gloc[w] = at
    return data, gloc


def write_syms(out, index, tree):
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.sym"):
        old.unlink()
    alias = index["alias"]
    rev = {}
    for c, t in alias.items():
        if not UNNAMED.match(c) and c not in KEYWORDS:
            rev.setdefault(t, c)
    funcs = collections.defaultdict(list)
    for a, z, n, lv in index["fmap"]:
        funcs[lv or "boot"].append((a, f"{n}:{z:X}"))
    data = collections.defaultdict(dict)
    for n, a in index["addr"].items():
        if n in tree.fnames or n in tree.afn or re.match(r"(FUN|func|jtbl|lbl)_", n) or a is None:
            continue
        m = re.match(r"^D_L(\d\d)_", n)
        scope = m.group(1) if m else ("boot" if 0x100000 <= a < RESIDENT_END else None)
        if scope:
            data[scope].setdefault(a, rev.get(n, n))
    for scope in sorted(set(funcs) | set(data)):
        rows = sorted(funcs[scope] + list(data[scope].items()))
        name = "boot" if scope == "boot" else f"level-{scope}"
        (out / f"{name}.sym").write_text("".join(f"{a:08X} {n}\n" for a, n in rows))


if __name__ == "__main__":
    main()
