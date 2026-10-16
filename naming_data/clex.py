"""Small C helpers for the Naming Desk data: comment stripping and top-level statements.

The game sources are plain C89-ish code for GCC 2.95, so a light scanner is enough: it keeps
line numbers, blanks comments and string contents, and splits the text at depth 0.
"""
import re

IDENT = re.compile(r"[A-Za-z_]\w*")


def strip(text):
    """Blank comments and the contents of string and char literals, keeping every newline."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        elif c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            if j < n and text[j] == c:
                body = text[i + 1:j]
                out.append(c + (body if c == '"' else re.sub(r"[^\n]", " ", body)) + c)
                i = j + 1
            else:  # unterminated: keep it as it is
                out.append(text[i:j])
                i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def preprocessor_lines(code):
    """(line number, directive text) for each # line, joined over backslash continuations."""
    lines = code.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("#"):
            start, text = i + 1, lines[i]
            while text.endswith("\\") and i + 1 < len(lines):
                i += 1
                text = text[:-1] + " " + lines[i]
            yield start, text.strip()
        i += 1


def blank_preprocessor(code):
    """The code with every # line (and its continuations) blanked, for the statement scanner."""
    lines = code.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("#"):
            while True:
                cont = lines[i].endswith("\\")
                lines[i] = ""
                if not cont or i + 1 >= len(lines):
                    break
                i += 1
        i += 1
    return "\n".join(lines)


def statements(code):
    """Top-level pieces of code as dicts. kind 'decl': text up to a ';' at depth 0. kind 'body': a head
    followed by a {...} block (function, struct, initializer), with the block text and, for
    `struct X {...} y;` or `= {...};`, the tail up to the ';'. Lines are 1-based."""
    depth, start, i, n = 0, 0, 0, len(code)
    line, start_line = 1, 1
    open_at = None
    while i < n:
        c = code[i]
        if c == "\n":
            line += 1
        elif c == "{":
            if depth == 0:
                open_at = (i, line)
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and open_at:
                head = code[start:open_at[0]]
                item = {"kind": "body", "head": head, "line": first_line(code, start, start_line), "end": line,
                        "body": code[open_at[0] + 1:i], "body_line": open_at[1], "tail": ""}
                j = i + 1
                rest = re.match(r"[^;{}()]*;", code[j:])
                if rest and (re.search(r"\b(struct|union|enum)\b", head) or "=" in head):
                    item["tail"] = rest.group(0)[:-1]
                    line += rest.group(0).count("\n")
                    j += len(rest.group(0))
                yield item
                i, start, start_line, open_at = j, j, line, None
                continue
            depth = max(depth, 0)
        elif c == ";" and depth == 0:
            yield {"kind": "decl", "text": code[start:i], "line": first_line(code, start, start_line), "end": line}
            start, start_line = i + 1, line
        i += 1


def first_line(code, start, start_line):
    """Line of the first non-blank character at or after start."""
    m = re.compile(r"\S").search(code, start)
    return start_line + code.count("\n", start, m.start()) if m else start_line


def match_paren(s, i, o="(", c=")"):
    """Index just past the bracket group opening at s[i]."""
    d = 0
    for j in range(i, len(s)):
        if s[j] == o:
            d += 1
        elif s[j] == c:
            d -= 1
            if d == 0:
                return j + 1
    return len(s)


def split_top(s, sep=","):
    """Split at sep outside brackets."""
    out, d, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            d += 1
        elif ch in ")]}":
            d -= 1
        if ch == sep and d == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


ATTR = re.compile(r"__attribute__\s*\(\(")


def drop_attributes(s):
    """Remove __attribute__((...)) and __asm__("...") groups, returning (text, attrs, asm name)."""
    attrs, asm = [], None
    while True:
        m = ATTR.search(s)
        if not m:
            break
        end = match_paren(s, m.end() - 2)
        attrs.append(s[m.end():end - 2])
        s = s[:m.start()] + " " + s[end:]
    m = re.search(r"__asm__\s*\(\s*\"(\w+)\"\s*\)", s)
    if m:
        asm = m.group(1)
        s = s[:m.start()] + " " + s[m.end():]
    return s, attrs, asm
