"""REGRESSION guard for the Stop-mid-stream NameError:

    cannot access free variable '_reconcile_subagent_results' where it is not
    associated with a value in enclosing scope

`_cancel_teardown` (inside the `except asyncio.CancelledError:` handler of the
v2.1 response handler in utils/middleware.py) closes over helper functions
defined in the enclosing scope. A user Stop can cancel the coroutine while the
FIRST model round is still streaming — i.e. at the earliest `await` inside the
handler's `try:` block. Any free variable the teardown closes over must
therefore be bound BEFORE that `try:` statement, or an early cancel hits an
unbound closure cell: the NameError replaces the CancelledError, surfaces to
the user as a retryable chat error, and the frontend auto-retries a turn the
user explicitly stopped.

Uses `symtable` for exact free-variable resolution (no false positives from
teardown-locals like `update_data`) and the AST for binding line numbers.
"""

import ast
import os
import symtable


MIDDLEWARE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "utils", "middleware.py"
)


def _build_parent_map(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _find_teardown(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_cancel_teardown":
            return node
    return None


def _nearest_ancestor(node, parents, kinds):
    current = parents.get(node)
    while current is not None:
        if isinstance(current, kinds):
            return current
        current = parents.get(current)
    return None


def _find_symtable_scope(table, name):
    for child in table.get_children():
        if child.get_name() == name:
            return child
        found = _find_symtable_scope(child, name)
        if found is not None:
            return found
    return None


def _bindings_in_function(func_node):
    """Map name -> first binding lineno for defs/assignments belonging to
    func_node's own scope (nested function BODIES are skipped — their internal
    assignments bind their own scope, but their names bind here)."""
    bound = {}

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.setdefault(child.name, child.lineno)
                continue  # inner scope
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    for n in ast.walk(target):
                        if isinstance(n, ast.Name):
                            bound.setdefault(n.id, child.lineno)
            elif isinstance(child, ast.AnnAssign) and isinstance(
                child.target, ast.Name
            ):
                bound.setdefault(child.target.id, child.lineno)
            visit(child)

    visit(func_node)
    return bound


def test_cancel_teardown_free_variables_are_bound_before_the_try_block():
    with open(MIDDLEWARE_PATH, "r") as f:
        source = f.read()
    tree = ast.parse(source)

    parents = _build_parent_map(tree)
    teardown = _find_teardown(tree)
    assert teardown is not None, "could not locate _cancel_teardown in middleware.py"

    handler = _nearest_ancestor(teardown, parents, (ast.ExceptHandler,))
    assert handler is not None, "_cancel_teardown is no longer in an except handler"
    try_stmt = _nearest_ancestor(handler, parents, (ast.Try,))
    assert try_stmt is not None
    func = _nearest_ancestor(
        teardown, parents, (ast.FunctionDef, ast.AsyncFunctionDef)
    )
    assert func is not None

    # Exact free variables of the teardown closure.
    module_table = symtable.symtable(source, MIDDLEWARE_PATH, "exec")
    teardown_table = _find_symtable_scope(module_table, "_cancel_teardown")
    assert teardown_table is not None
    free_vars = {
        s.get_name() for s in teardown_table.get_symbols() if s.is_free()
    }

    bindings = _bindings_in_function(func)
    # A free var bound INSIDE the handler itself (e.g. sibling locals of the
    # teardown) executes before the teardown runs — only bindings that live in
    # the function body AFTER the try: are unreachable on an early cancel.
    handler_start = handler.lineno
    late = sorted(
        f"{name} (bound at line {bindings[name]})"
        for name in free_vars
        if name in bindings
        and bindings[name] > try_stmt.lineno
        and not (handler_start <= bindings[name] <= (handler.end_lineno or 10**9))
    )
    assert not late, (
        f"free variables of _cancel_teardown are bound AFTER the try: on line "
        f"{try_stmt.lineno} — an early Stop (cancel during the first streaming "
        f"round) hits an unbound closure cell ('cannot access free variable') "
        f"and breaks stop-means-stop: {late}"
    )

    # The two helpers this regression is about must exist and be early-bound.
    for name in ("_reconcile_subagent_results", "_sweep_subagent_runs"):
        assert name in bindings, f"{name} no longer defined in the handler's scope"
        assert bindings[name] < try_stmt.lineno, (
            f"{name} is defined after the try: block again (line "
            f"{bindings[name]} > {try_stmt.lineno}) — regression of the cancel "
            f"NameError"
        )


def test_cancel_teardown_calls_terminal_sweep_before_reconcile():
    with open(MIDDLEWARE_PATH, "r") as f:
        tree = ast.parse(f.read())

    teardown = _find_teardown(tree)
    assert teardown is not None, "could not locate _cancel_teardown in middleware.py"

    awaited_helpers = []
    for node in ast.walk(teardown):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if isinstance(call.func, ast.Name):
            awaited_helpers.append((call.func.id, node.lineno))

    sweep_lines = [
        line for name, line in awaited_helpers if name == "_sweep_subagent_runs"
    ]
    reconcile_lines = [
        line for name, line in awaited_helpers if name == "_reconcile_subagent_results"
    ]
    assert sweep_lines, (
        "cancel teardown no longer terminalizes stranded subagent runs; a Stop "
        "can leave persisted cards stuck in status='running'"
    )
    assert reconcile_lines
    assert min(sweep_lines) < min(reconcile_lines), (
        "cancel teardown must sweep before reconciling so a recovered finished "
        "run can be mirrored into the parent tool result"
    )


if __name__ == "__main__":
    test_cancel_teardown_free_variables_are_bound_before_the_try_block()
    test_cancel_teardown_calls_terminal_sweep_before_reconcile()
    print("cancel teardown scope test passed")
