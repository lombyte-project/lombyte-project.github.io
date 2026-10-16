"""C declarations and struct layouts for the EE (GCC 2.95: int 4, long 8, pointers 4).

A size is only given when every member's type is known; otherwise it stays None, so the Desk
never shows a guessed size.
"""
import re

from clex import IDENT, drop_attributes, match_paren, split_top

BASIC = {"char": 1, "short": 2, "int": 4, "long": 8, "float": 4, "double": 8, "void": None,
         "__int128": 16, "_Bool": 1}
QUAL = {"extern", "static", "const", "volatile", "register", "inline", "__inline__", "__inline",
        "typedef", "auto", "__extension__", "signed", "unsigned", "restrict", "__restrict"}
TAGS = {"struct", "union", "enum"}


class Decl:
    """One declarator: name, base type, pointer depth, array dims, function or not."""

    def __init__(self, name, base, ptr, dims, func, fptr, init):
        self.name, self.base, self.ptr, self.dims, self.func, self.fptr, self.init = name, base, ptr, dims, func, fptr, init

    @property
    def is_object(self):
        return not self.func


def parse_base(s):
    """Split 'extern struct Moby *x, y' into (storage words, base type text, rest)."""
    words, base, has_type = set(), [], False
    i = 0
    while True:
        m = re.compile(r"\s*([A-Za-z_]\w*|\S)").match(s, i)
        if not m:
            break
        t = m.group(1)
        if t in QUAL:
            words.add(t)
            if t in ("signed", "unsigned"):
                base.append(t)
            i = m.end()
        elif t in BASIC:
            base.append(t)
            has_type = True
            i = m.end()
        elif t in TAGS:
            m2 = re.compile(r"\s*([A-Za-z_]\w*)").match(s, m.end())
            base.append(t + " " + (m2.group(1) if m2 else ""))
            has_type = True
            i = m2.end() if m2 else m.end()
        elif t in ("__typeof__", "typeof", "__typeof"):
            j = s.index("(", m.end())
            end = match_paren(s, j)
            base.append("__typeof__" + s[j:end])
            has_type = True
            i = end
        elif IDENT.fullmatch(t) and not has_type and not (base and base[-1] in ("signed", "unsigned")):
            base.append(t)
            has_type = True
            i = m.end()
        else:
            break
    if base and not has_type:  # 'unsigned x'
        has_type = True
    return words, " ".join(base), s[i:]


def parse_declarator(d, base):
    d, _attrs, _asm = drop_attributes(d)
    init = None
    parts = split_top(d, "=")
    if len(parts) > 1:
        d, init = parts[0], "=".join(parts[1:])
    d = d.strip()
    ptr = 0
    while True:
        m = re.match(r"(\*|const\b|volatile\b|__restrict\b|restrict\b)\s*", d)
        if not m:
            break
        if m.group(1) == "*":
            ptr += 1
        d = d[m.end():]
    func = fptr = False
    dims = []
    if d.startswith("("):
        end = match_paren(d, 0)
        inner, rest = d[1:end - 1], d[end:].strip()
        sub = parse_declarator(inner, base)
        if not sub:
            return None
        name = sub.name
        if rest.startswith("("):
            fptr = True  # pointer to function: an object
            ptr = max(1, sub.ptr)
        dims = sub.dims
        return Decl(name, base, ptr + sub.ptr, dims, False, fptr, init)
    m = IDENT.match(d)
    if not m:
        return None
    name, rest = m.group(0), d[m.end():].strip()
    if rest.startswith("("):
        func = True
    else:
        for dm in re.finditer(r"\[([^\]]*)\]", rest):
            dims.append(dm.group(1).strip())
    return Decl(name, base, ptr, dims, func, fptr, init)


def parse_decl(text):
    """(storage words, [Decl]) for one declaration statement without its ';'."""
    text, attrs, asm = drop_attributes(text)
    words, base, rest = parse_base(text.strip())
    if not base:
        return words, [], asm, attrs
    decls = []
    for d in split_top(rest):
        if d.strip():
            x = parse_declarator(d, base)
            if x:
                decls.append(x)
    return words, decls, asm, attrs


def type_name(base):
    """'struct Moby' -> 'Moby', 'unsigned int' -> 'unsigned int'."""
    return re.sub(r"^(struct|union|enum)\s+", "", base).strip()


def evaluate(expr, consts):
    """Value of an array dimension, or None."""
    expr = expr.strip()
    if not expr:
        return None
    expr = re.sub(r"\b([A-Za-z_]\w*)\b", lambda m: str(consts[m.group(1)]) if m.group(1) in consts else m.group(1), expr)
    expr = re.sub(r"(?<=[0-9a-fA-FxX])[uUlL]+\b", "", expr)
    if not re.fullmatch(r"[0-9a-fA-FxX\s()+\-*/<>|&~%]+", expr):
        return None
    try:
        v = eval(expr.replace("/", "//"), {"__builtin__": None}, {})
        return int(v) if isinstance(v, int) else None
    except Exception:
        return None


class Layouts:
    """Typedefs and struct/union layouts collected over the whole tree."""

    def __init__(self):
        self.typedefs = {}  # name -> (base, ptr, dims, aligned, path, line)
        self.records = {}  # key -> dict(kind, members, path, line)
        self.order = []
        self.consts = {}
        self.enums = set()
        self._size = {}

    # --- collecting ---------------------------------------------------------
    def add_record(self, kind, tag, body, path, line, body_line):
        """Parse 'struct tag { body }'; returns its key (anonymous records get a made-up key)."""
        members = self.parse_members(body, path, body_line)
        key = tag or "__anon_%d" % len(self.records)
        rec = {"kind": kind, "members": members, "p": path, "l": line, "tag": tag}
        if key in self.records:
            # another file's (or this file's earlier) copy: each file keeps its own, so the Desk
            # shows the layout the reader is looking at
            if self.records[key]["p"] == path:
                return key
            key = "%s@%s" % (key, path)
            if key in self.records:
                return key
        self.records[key] = rec
        self.order.append(key)
        return key

    def parse_members(self, body, path, body_line):
        """[(name, base, ptr, dims, bits, line, aligned)] for one record body."""
        out = []
        i, n, line = 0, len(body), body_line
        start, start_line = 0, body_line
        depth = 0
        while i < n:
            c = body[i]
            if c == "\n":
                line += 1
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif c == ";" and depth == 0:
                text = body[start:i]
                lead = text[:len(text) - len(text.lstrip())]
                self.member(text, path, start_line + lead.count("\n"), out)
                start, start_line = i + 1, line
            i += 1
        return out

    def member(self, text, path, line, out):
        if not text.strip():
            return
        m = re.match(r"\s*(struct|union)\s*(\w*)\s*\{", text)
        if m:
            j = text.index("{", m.start())
            end = match_paren(text, j, "{", "}")
            inner_line = line + text[:j].count("\n")
            key = self.add_record(m.group(1), m.group(2) or None, text[j + 1:end - 1], path, line, inner_line)
            text = m.group(1) + " " + key + " " + text[end:]
        text, attrs, _ = drop_attributes(text)
        aligned = max([int(a.group(1), 0) for x in attrs for a in [re.search(r"aligned\s*\(\s*(\w+)\s*\)", x)] if a] or [0])
        words, base, rest = parse_base(text.strip())
        for d in split_top(rest):
            bits = None
            if ":" in d:
                d, b = d.rsplit(":", 1)
                bits = evaluate(b, self.consts)
            x = parse_declarator(d, base) if d.strip() else None
            if x:
                out.append((x.name, base, x.ptr if not x.fptr else max(1, x.ptr), x.dims, bits, line, aligned))
            elif bits is not None:
                out.append((None, base, 0, [], bits, line, aligned))

    # --- sizes --------------------------------------------------------------
    def resolve(self, name, ctx):
        """The record a type name means in file ctx: that file's own copy first."""
        if ctx and "%s@%s" % (name, ctx) in self.records:
            return "%s@%s" % (name, ctx)
        return name if name in self.records else None

    def base_size(self, base, ctx=None):
        """(size, align) of a base type, or (None, None)."""
        words = [w for w in base.split() if w not in ("signed", "unsigned")] or ["int"]
        b = " ".join(words)
        if words and words[0] in ("struct", "union"):
            key = self.resolve(" ".join(words[1:]), ctx)
            return self.record_size(key) if key else (None, None)
        if words and words[0] == "enum":
            return 4, 4
        if b in self.enums:
            return 4, 4
        if b in ("long long", "long long int", "long int", "long"):
            return 8, 8
        if b in ("short int", "short"):
            return 2, 2
        if b in BASIC:
            z = BASIC[b]
            return (z, z) if z else (None, None)
        if b in self.typedefs:
            tb, ptr, dims, al = self.typedefs[b][:4]
            z, a = self.full_size(tb, ptr, dims, self.typedefs[b][4])
            if al:
                a = max(a or 1, al)
                if z is not None:
                    z = (z + a - 1) // a * a
            return z, a
        key = self.resolve(b, ctx)
        if key:
            return self.record_size(key)
        return None, None

    def full_size(self, base, ptr, dims, ctx=None):
        if ptr:
            z, a = 4, 4
        else:
            z, a = self.base_size(base, ctx)
        n = 1
        for d in dims:
            v = evaluate(d, self.consts)
            if v is None:
                return None, a
            n *= v
        return (z * n if z is not None else None), a

    def record_size(self, key):
        lay = self.layout(key)
        return (lay["z"], lay["a"]) if lay else (None, None)

    def layout(self, key):
        if key not in self.records:
            return None
        if key in self._size:
            return self._size[key]
        self._size[key] = None  # recursion guard
        rec = self.records[key]
        union = rec["kind"] == "union"
        off, bit, max_a, max_z, fields, ok = 0, 0, 1, 0, [], True
        for name, base, ptr, dims, bits, line, aligned in rec["members"]:
            if bits is not None and not ptr:
                z, a = self.base_size(base, rec["p"])
                if z is None or off is None:
                    ok = False
                    off = None
                    if name:
                        fields.append([name, None, line])
                    continue
                unit = z * 8
                pos = (0 if union else off * 8 + bit)
                if bits == 0 or (pos % unit) + bits > unit:
                    pos = (pos + unit - 1) // unit * unit
                if name:
                    fields.append([name, pos // unit * z, line, type_name(base), z, z])
                max_a = max(max_a, a)
                if not union:
                    off, bit = (pos + bits) // 8, (pos + bits) % 8
                else:
                    max_z = max(max_z, z)
                continue
            if bit:
                off, bit = (off + 1 if off is not None else None), 0
            z, a = self.full_size(base, ptr, dims, rec["p"])
            el = None
            if not ptr and z is not None:
                el = z
                for d in dims:
                    v = evaluate(d, self.consts)
                    el = el // v if v else el
            if aligned:
                a = max(a or 1, aligned)
            here = 0 if union else off
            if here is not None and a:
                here = (here + a - 1) // a * a
            if ptr or type_name(base).startswith("__anon_"):
                fields.append([name, here, line])
            elif z is None:
                fields.append([name, here, line])
            else:
                fields.append([name, here, line, type_name(base), z, el])
            if z is None or a is None:
                ok = False
                if not union:
                    off = None
                continue
            max_a = max(max_a, a)
            if union:
                max_z = max(max_z, z)
            elif off is not None:
                off = here + z
        if bit and off is not None:
            off += 1
        size = None
        if ok:
            end = max_z if union else off
            size = (end + max_a - 1) // max_a * max_a if end is not None else None
        res = {"p": rec["p"], "l": rec["l"], "z": size, "a": max_a if ok else None, "f": fields}
        self._size[key] = res
        return res
