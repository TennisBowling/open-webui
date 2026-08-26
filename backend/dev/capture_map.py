"""Exact closure-capture analysis for middleware.py via symtable.

For every function (at any nesting depth) prints:
  - FREE vars it captures from enclosing scopes (reads)
  - which of those it declares `nonlocal` (writes)
This is the compiler's own view — no regex guessing.
"""

import symtable
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "backend/open_webui/utils/middleware.py"
FILTER = sys.argv[2] if len(sys.argv) > 2 else None  # only functions whose path contains this

with open(SRC) as f:
    code = f.read()

top = symtable.symtable(code, SRC, "exec")


def walk(table, path):
    name = table.get_name()
    p = f"{path}.{name}" if path else name
    if table.get_type() == "function":
        free = sorted(s.get_name() for s in table.get_symbols() if s.is_free())
        nl = sorted(
            s.get_name()
            for s in table.get_symbols()
            if s.is_free() and s.is_assigned()
        )
        if (FILTER is None or FILTER in p) and free:
            reads = [v for v in free if v not in nl]
            print(f"{p}  (line {table.get_lineno()})")
            if nl:
                print(f"  WRITES: {', '.join(nl)}")
            if reads:
                print(f"  reads : {', '.join(reads)}")
    for child in table.get_children():
        walk(child, p)


walk(top, "")
