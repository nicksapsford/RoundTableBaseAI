#!/usr/bin/env python3
"""Validate JavaScript embedded in dashboard_*.py page templates.

WHY THIS EXISTS: the dashboards build their HTML page as Python string literals.
Because the page is a Python string, JS escape sequences are processed by Python
*first*. A JS spinner written "\\" in the source collapses to a single backslash
and emits the broken JS string "\" -- an unterminated literal that throws a
SyntaxError and disables the ENTIRE <script> block (blank page after restart).
`python -m py_compile` cannot see this -- the Python is valid; the emitted JS is
not. This happened to RoundTable on 15 Jul 2026.

This tool parses each dashboard with `ast` (NO execution -- safe on live trading
processes), reconstructs the emitted string values, extracts every complete
<script>...</script> block, and fails if any block contains an unterminated JS
string literal or unbalanced brackets.

Usage:  python tools/check_dashboard_js.py [file ...]
Default target: dashboard_*.py in the current directory (the repo root, where a
git pre-commit hook runs). Exit code 1 on any problem, 0 if clean.
"""
import ast
import glob
import os
import re
import sys

BS = chr(92)   # backslash


def emitted_string_literals(src):
    """Every string literal's emitted value (post-Python-escape), each independent
    -- a <script> block lives entirely inside one literal, so validating
    per-literal matches what the browser actually receives. f-string
    interpolations are neutralised to '0' so surrounding JS structure is kept."""
    tree = ast.parse(src)
    values, joined_ids = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                    joined_ids.add(id(v))
                else:
                    parts.append("0")
            values.append("".join(parts))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in joined_ids:
            values.append(node.value)
    return values


def _unterminated(line):
    """True if a JS string literal is left open at end of line (raw newline in a
    string, or a mangled `"\"` backslash). Honours // line comments and escapes."""
    i, n, q = 0, len(line), None
    while i < n:
        c = line[i]
        if q:
            if c == BS:
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c == '/' and i + 1 < n and line[i + 1] == '/':
            break
        if c in ('"', "'"):
            q = c
        i += 1
    return q is not None


def _strip_strings_comments(s):
    out = []
    i, n, q, blk = 0, len(s), None, False
    while i < n:
        c = s[i]
        if blk:
            if s[i:i + 2] == '*/':
                blk = False
                i += 2
                continue
            i += 1
            continue
        if q:
            if c == BS:
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if s[i:i + 2] == '//':
            j = s.find('\n', i)
            i = n if j < 0 else j
            continue
        if s[i:i + 2] == '/*':
            blk = True
            i += 2
            continue
        if c in ('"', "'"):
            q = c
            i += 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def check_script(sc):
    probs, blk = [], False
    for ln, line in enumerate(sc.split('\n'), 1):
        if blk:
            if '*/' in line:
                line = line.split('*/', 1)[1]
                blk = False
            else:
                continue
        while '/*' in line:
            head, tail = line.split('/*', 1)
            if '*/' in tail:
                line = head + tail.split('*/', 1)[1]
            else:
                line = head
                blk = True
                break
        if _unterminated(line):
            probs.append("unterminated string @ JS line %d: %s" % (ln, line.strip()[:100]))
    clean = _strip_strings_comments(sc)
    for pair, (o, c) in {'()': ('(', ')'), '{}': ('{', '}'), '[]': ('[', ']')}.items():
        d = clean.count(o) - clean.count(c)
        if d:
            probs.append("bracket imbalance %s: %+d" % (pair, d))
    return probs


def check_file(path):
    try:
        src = open(path, encoding='utf-8').read()
    except OSError as e:
        return ["cannot read file: %s" % e]
    try:
        lits = emitted_string_literals(src)
    except SyntaxError as e:
        return ["python parse error: %s" % e]
    probs, nscripts = [], 0
    for value in lits:
        for sc in re.findall(r'<script[^>]*>(.*?)</script>', value, re.S | re.I):
            nscripts += 1
            probs += check_script(sc)
    if nscripts == 0:
        print("  note: %s has no complete <script> block (skipped)" % os.path.basename(path))
    return probs


def main(argv):
    targets = argv[1:] or sorted(glob.glob("dashboard_*.py"))
    if not targets:
        print("check_dashboard_js: no dashboard_*.py in %s -- nothing to check." % os.getcwd())
        return 0
    failed = False
    for path in targets:
        probs = check_file(path)
        if probs:
            failed = True
            print("FAIL  %s" % path)
            for p in probs:
                print("      %s" % p)
        else:
            print("OK    %s" % path)
    if failed:
        print("\nEmbedded-dashboard JS validation FAILED. This usually means a mangled")
        print("escape in the page template: write %s%s for a JS backslash and %sn for a"
              % (BS, BS, BS + BS))
        print("JS newline (Python eats a single backslash inside the page string). Fix")
        print("and re-commit.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
