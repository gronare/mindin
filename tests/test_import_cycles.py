import ast
from pathlib import Path

import mindin

PACKAGE = Path(mindin.__file__).parent


def module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def runtime_nodes(tree: ast.AST) -> list[ast.AST]:
    typing_only = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and ast.unparse(node.test) in ("TYPE_CHECKING", "typing.TYPE_CHECKING")
        for statement in node.body
        for inner in ast.walk(statement)
    }
    return [node for node in ast.walk(tree) if id(node) not in typing_only]


def imports_graph() -> dict[str, set[str]]:
    modules = {module_name(path): path for path in PACKAGE.rglob("*.py")}
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        for node in runtime_nodes(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                targets = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            else:
                continue
            graph[name].update(target for target in targets if target in modules and target != name)
    return graph


def find_cycle(graph: dict[str, set[str]]) -> list[str]:
    done: set[str] = set()

    def visit(name: str, trail: list[str]) -> list[str]:
        if name in trail:
            return [*trail[trail.index(name) :], name]
        if name in done:
            return []
        for target in sorted(graph[name]):
            cycle = visit(target, [*trail, name])
            if cycle:
                return cycle
        done.add(name)
        return []

    for name in sorted(graph):
        cycle = visit(name, [])
        if cycle:
            return cycle
    return []


def test_no_module_imports_back_into_a_module_that_imports_it_at_runtime_even_inside_a_function() -> None:
    assert find_cycle(imports_graph()) == []
