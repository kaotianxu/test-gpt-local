"""Lightweight, language-aware code intelligence MCP tools.

The implementation deliberately has no language-server dependency.  Python is
indexed with :mod:`ast`; common TypeScript/JavaScript declarations are indexed
with a conservative line parser.  This provides useful answers immediately and
forms a stable fallback for a future LSP-backed index.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.services.envelope import error_result, ok_result
from app.services.path_guard import is_denied, resolve_within
from app.services.subprocess_utils import no_window_creationflags
from app.services.workspace_manager import get_workspace

_SOURCE_SUFFIXES = frozenset({".py", ".pyi", ".js", ".jsx", ".ts", ".tsx"})
_DIAGNOSTIC_SUFFIXES = _SOURCE_SUFFIXES | {".json", ".toml", ".yaml", ".yml"}
_SKIP_DIRS = frozenset({".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules"})
_MAX_FILES = 2_000
_MAX_FILE_BYTES = 2_000_000
_IDENTIFIER = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
_JS_DECLARATION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:(class|interface|enum|function|type)\s+([A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>)"
)
_JS_METHOD = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+)*"
    r"([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?::[^={]+)?[={]"
)
_JS_BASES = re.compile(r"\b(?:extends|implements)\s+([^\{]+)")
_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")


@dataclass
class _Symbol:
    name: str
    qualified_name: str
    kind: str
    path: str
    line: int
    end_line: int
    container: str | None = None
    signature: str = ""
    bases: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        if not data["bases"]:
            data.pop("bases")
        if not data["calls"]:
            data.pop("calls")
        if data["container"] is None:
            data.pop("container")
        return data


def _workspace_root(workspace_id: str) -> tuple[Path | None, dict[str, Any] | None]:
    record = get_workspace(workspace_id)
    if record is None:
        return None, error_result(
            "WORKSPACE_NOT_FOUND", f"workspace not found: {workspace_id}", workspace_id=workspace_id
        )
    root = Path(record["worktree_path"])
    if not root.is_dir():
        return None, error_result(
            "STALE_WORKSPACE", f"worktree path missing on disk: {root}", workspace_id=workspace_id
        )
    return root.resolve(), None


def _target(root: Path, path: str, *, must_exist: bool = True) -> Path:
    target = root if not path or path == "." else resolve_within(root, path, must_exist=must_exist)
    if is_denied(target, root):
        raise ValueError("path is denied by policy")
    return target


def _files(root: Path, path: str, suffixes: frozenset[str]) -> list[Path]:
    target = _target(root, path)
    candidates = [target] if target.is_file() else target.rglob("*")
    result: list[Path] = []
    for candidate in candidates:
        if len(result) >= _MAX_FILES:
            break
        if not candidate.is_file() or candidate.suffix.lower() not in suffixes:
            continue
        relative = candidate.relative_to(root)
        if any(part in _SKIP_DIRS for part in relative.parts) or is_denied(candidate, root):
            continue
        try:
            if candidate.stat().st_size <= _MAX_FILE_BYTES:
                result.append(candidate)
        except OSError:
            continue
    return sorted(result)


def _source_line(lines: list[str], line: int) -> str:
    return lines[line - 1].strip()[:500] if 0 < line <= len(lines) else ""


class _PythonIndexer(ast.NodeVisitor):
    def __init__(self, relative_path: str, lines: list[str]) -> None:
        self.relative_path = relative_path
        self.lines = lines
        self.symbols: list[_Symbol] = []
        self.stack: list[str] = []

    def _add(self, node: ast.AST, name: str, kind: str, bases: list[str] | None = None) -> None:
        qualified = ".".join([*self.stack, name])
        calls = sorted(
            {
                self._call_name(child.func)
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and self._call_name(child.func)
            }
        )
        self.symbols.append(
            _Symbol(
                name=name,
                qualified_name=qualified,
                kind=kind,
                path=self.relative_path,
                line=int(getattr(node, "lineno", 1)),
                end_line=int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
                container=".".join(self.stack) or None,
                signature=_source_line(self.lines, int(getattr(node, "lineno", 1))),
                bases=bases or [],
                calls=calls,
            )
        )

    @staticmethod
    def _call_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    @staticmethod
    def _expr_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = _PythonIndexer._expr_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        if isinstance(node, ast.Subscript):
            return _PythonIndexer._expr_name(node.value)
        return ""

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add(node, node.name, "class", [self._expr_name(base) for base in node.bases])
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._add(node, node.name, "method" if self.stack else "function")
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()


def _python_symbols(path: Path, root: Path, text: str) -> list[_Symbol]:
    try:
        tree = ast.parse(text, filename=str(path), type_comments=True)
    except (SyntaxError, ValueError):
        return []
    indexer = _PythonIndexer(path.relative_to(root).as_posix(), text.splitlines())
    indexer.visit(tree)
    return indexer.symbols


def _javascript_symbols(path: Path, root: Path, text: str) -> list[_Symbol]:
    symbols: list[_Symbol] = []
    active_class: tuple[str, int, int] | None = None
    depth = 0
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        declaration = _JS_DECLARATION.match(line)
        if declaration:
            raw_kind, named, variable = declaration.groups()
            name = named or variable
            kind = "function" if variable else str(raw_kind)
            bases_match = _JS_BASES.search(line) if kind in {"class", "interface"} else None
            bases = (
                [
                    item.strip().split("<", 1)[0]
                    for item in re.split(r"\s*,\s*", bases_match.group(1))
                ]
                if bases_match
                else []
            )
            end_line = _brace_end(lines, number)
            symbols.append(
                _Symbol(
                    name=name,
                    qualified_name=name,
                    kind=kind,
                    path=path.relative_to(root).as_posix(),
                    line=number,
                    end_line=end_line,
                    signature=line.strip()[:500],
                    bases=bases,
                    calls=sorted(set(_CALL.findall("\n".join(lines[number - 1 : end_line])))),
                )
            )
            if kind == "class":
                active_class = (name, depth, end_line)
        if active_class and number > active_class[2]:
            active_class = None
        elif active_class and number > 1:
            method = _JS_METHOD.match(line)
            if method and method.group(1) not in {"if", "for", "while", "switch", "catch"}:
                name = method.group(1)
                end_line = _brace_end(lines, number)
                symbols.append(
                    _Symbol(
                        name=name,
                        qualified_name=f"{active_class[0]}.{name}",
                        kind="method",
                        path=path.relative_to(root).as_posix(),
                        line=number,
                        end_line=end_line,
                        container=active_class[0],
                        signature=line.strip()[:500],
                        calls=sorted(set(_CALL.findall("\n".join(lines[number - 1 : end_line])))),
                    )
                )
        depth += line.count("{") - line.count("}")
    return symbols


def _brace_end(lines: list[str], start: int) -> int:
    depth = 0
    seen = False
    for number in range(start, min(len(lines), start + 2_000) + 1):
        line = lines[number - 1]
        depth += line.count("{") - line.count("}")
        seen = seen or "{" in line
        if seen and depth <= 0:
            return number
    return start


def _index(root: Path, path: str = "") -> tuple[list[_Symbol], list[str]]:
    symbols: list[_Symbol] = []
    warnings: list[str] = []
    files = _files(root, path, _SOURCE_SUFFIXES)
    if len(files) == _MAX_FILES:
        warnings.append(f"index limited to {_MAX_FILES} source files")
    for source in files:
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"could not read {source.relative_to(root).as_posix()}: {exc}")
            continue
        if source.suffix.lower() in {".py", ".pyi"}:
            symbols.extend(_python_symbols(source, root, text))
        else:
            symbols.extend(_javascript_symbols(source, root, text))
    return symbols, warnings


def _validate_symbol(symbol: str) -> str | None:
    value = symbol.strip()
    return value if value and _IDENTIFIER.fullmatch(value) else None


def _begin(workspace_id: str) -> tuple[Path | None, dict[str, Any] | None]:
    return _workspace_root(workspace_id)


def list_symbols(
    workspace_id: str, path: str = "", query: str = "", max_results: int = 200
) -> dict[str, Any]:
    root, error = _begin(workspace_id)
    if error or root is None:
        return error or {}
    try:
        symbols, warnings = _index(root, path)
    except ValueError as exc:
        return error_result("PATH_DENIED", str(exc), workspace_id=workspace_id)
    needle = query.casefold().strip()
    matches = [item for item in symbols if not needle or needle in item.qualified_name.casefold()]
    limit = max(1, min(int(max_results), 1_000))
    return ok_result(
        {"symbols": [item.public() for item in matches[:limit]], "count": min(len(matches), limit)},
        workspace_id=workspace_id,
        warnings=warnings,
        truncated=len(matches) > limit,
    )


def find_definition(
    workspace_id: str, symbol: str, path: str = "", max_results: int = 100
) -> dict[str, Any]:
    root, error = _begin(workspace_id)
    valid = _validate_symbol(symbol)
    if error or root is None:
        return error or {}
    if valid is None:
        return error_result(
            "INVALID_INPUT", "symbol must be a dotted identifier", workspace_id=workspace_id
        )
    try:
        symbols, warnings = _index(root, path)
    except ValueError as exc:
        return error_result("PATH_DENIED", str(exc), workspace_id=workspace_id)
    matches = [item for item in symbols if item.name == valid or item.qualified_name == valid]
    limit = max(1, min(int(max_results), 1_000))
    return ok_result(
        {
            "symbol": valid,
            "definitions": [item.public() for item in matches[:limit]],
            "count": min(len(matches), limit),
        },
        workspace_id=workspace_id,
        warnings=warnings,
        truncated=len(matches) > limit,
    )


def find_references(
    workspace_id: str, symbol: str, path: str = "", max_results: int = 200
) -> dict[str, Any]:
    root, error = _begin(workspace_id)
    valid = _validate_symbol(symbol)
    if error or root is None:
        return error or {}
    if valid is None:
        return error_result(
            "INVALID_INPUT", "symbol must be a dotted identifier", workspace_id=workspace_id
        )
    leaf = valid.rsplit(".", 1)[-1]
    pattern = re.compile(rf"(?<![\w$]){re.escape(leaf)}(?![\w$])")
    references: list[dict[str, Any]] = []
    try:
        files = _files(root, path, _SOURCE_SUFFIXES)
    except ValueError as exc:
        return error_result("PATH_DENIED", str(exc), workspace_id=workspace_id)
    limit = max(1, min(int(max_results), 1_000))
    for source in files:
        text = source.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            for match in pattern.finditer(line):
                references.append(
                    {
                        "path": source.relative_to(root).as_posix(),
                        "line": number,
                        "column": match.start() + 1,
                        "preview": line.strip()[:500],
                    }
                )
                if len(references) > limit:
                    break
            if len(references) > limit:
                break
        if len(references) > limit:
            break
    return ok_result(
        {"symbol": valid, "references": references[:limit], "count": min(len(references), limit)},
        workspace_id=workspace_id,
        truncated=len(references) > limit,
    )


def find_implementations(
    workspace_id: str, symbol: str, path: str = "", max_results: int = 100
) -> dict[str, Any]:
    root, error = _begin(workspace_id)
    valid = _validate_symbol(symbol)
    if error or root is None:
        return error or {}
    if valid is None:
        return error_result(
            "INVALID_INPUT", "symbol must be a dotted identifier", workspace_id=workspace_id
        )
    try:
        symbols, warnings = _index(root, path)
    except ValueError as exc:
        return error_result("PATH_DENIED", str(exc), workspace_id=workspace_id)
    leaf = valid.rsplit(".", 1)[-1]
    matches = [
        item
        for item in symbols
        if (
            item.kind in {"class", "interface"}
            and any(base.rsplit(".", 1)[-1] == leaf for base in item.bases)
        )
        or (
            "." in valid
            and item.kind == "method"
            and item.name == leaf
            and item.qualified_name != valid
        )
    ]
    limit = max(1, min(int(max_results), 1_000))
    return ok_result(
        {
            "symbol": valid,
            "implementations": [item.public() for item in matches[:limit]],
            "count": min(len(matches), limit),
        },
        workspace_id=workspace_id,
        warnings=warnings,
        truncated=len(matches) > limit,
    )


def get_call_hierarchy(workspace_id: str, symbol: str, path: str = "") -> dict[str, Any]:
    root, error = _begin(workspace_id)
    valid = _validate_symbol(symbol)
    if error or root is None:
        return error or {}
    if valid is None:
        return error_result(
            "INVALID_INPUT", "symbol must be a dotted identifier", workspace_id=workspace_id
        )
    try:
        symbols, warnings = _index(root, path)
    except ValueError as exc:
        return error_result("PATH_DENIED", str(exc), workspace_id=workspace_id)
    leaf = valid.rsplit(".", 1)[-1]
    targets = [item for item in symbols if item.name == valid or item.qualified_name == valid]
    outgoing_names = sorted({call for item in targets for call in item.calls})
    incoming = [item for item in symbols if leaf in item.calls and item not in targets]
    outgoing = [item for item in symbols if item.name in outgoing_names]
    return ok_result(
        {
            "symbol": valid,
            "definitions": [item.public() for item in targets],
            "incoming": [item.public() for item in incoming],
            "outgoing": [item.public() for item in outgoing],
            "unresolved_outgoing": sorted(set(outgoing_names) - {item.name for item in outgoing}),
        },
        workspace_id=workspace_id,
        warnings=warnings,
    )


def get_diagnostics(workspace_id: str, path: str = "") -> dict[str, Any]:
    root, error = _begin(workspace_id)
    if error or root is None:
        return error or {}
    diagnostics: list[dict[str, Any]] = []
    try:
        files = _files(root, path, _DIAGNOSTIC_SUFFIXES)
    except ValueError as exc:
        return error_result("PATH_DENIED", str(exc), workspace_id=workspace_id)
    for source in files:
        relative = source.relative_to(root).as_posix()
        try:
            text = source.read_text(encoding="utf-8", errors="strict")
            suffix = source.suffix.lower()
            if suffix in {".py", ".pyi"}:
                ast.parse(text, filename=relative, type_comments=True)
            elif suffix == ".json":
                json.loads(text)
            elif suffix == ".toml":
                tomllib.loads(text)
            elif suffix in {".yaml", ".yml"}:
                yaml.safe_load(text)
        except (
            SyntaxError,
            UnicodeError,
            json.JSONDecodeError,
            tomllib.TOMLDecodeError,
            yaml.YAMLError,
        ) as exc:
            mark = getattr(exc, "problem_mark", None)
            diagnostics.append(
                {
                    "path": relative,
                    "severity": "error",
                    "line": int(getattr(exc, "lineno", getattr(mark, "line", -1) + 1) or 1),
                    "column": int(getattr(exc, "offset", getattr(mark, "column", -1) + 1) or 1),
                    "message": str(exc),
                    "source": "fallback-parser",
                }
            )
    return ok_result(
        {"diagnostics": diagnostics, "count": len(diagnostics), "files_checked": len(files)},
        workspace_id=workspace_id,
    )


def get_changed_symbols(
    workspace_id: str, base: str = "HEAD~1", head: str = "HEAD", max_results: int = 200
) -> dict[str, Any]:
    root, error = _begin(workspace_id)
    if error or root is None:
        return error or {}
    if not base.strip() or not head.strip() or base.startswith("-") or head.startswith("-"):
        return error_result(
            "INVALID_INPUT", "base and head must be Git revisions", workspace_id=workspace_id
        )
    proc = subprocess.run(
        ["git", "diff", "--unified=0", "--no-color", base, head, "--"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=no_window_creationflags(),
    )
    if proc.returncode != 0:
        return error_result(
            "INVALID_INPUT", proc.stderr.strip() or "git diff failed", workspace_id=workspace_id
        )
    changed: dict[str, list[tuple[int, int]]] = {}
    current_path = ""
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
        elif current_path and line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2) or "1")
                changed.setdefault(current_path, []).append((start, start + max(count - 1, 0)))
    symbols: list[_Symbol] = []
    warnings: list[str] = []
    for changed_path, ranges in changed.items():
        candidate = root / changed_path
        if not candidate.is_file() or candidate.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        indexed, index_warnings = _index(root, changed_path)
        warnings.extend(index_warnings)
        for item in indexed:
            if any(item.line <= end and item.end_line >= start for start, end in ranges):
                symbols.append(item)
    unique = {(item.path, item.line, item.qualified_name): item for item in symbols}
    ordered = [unique[key] for key in sorted(unique)]
    limit = max(1, min(int(max_results), 1_000))
    return ok_result(
        {
            "base": base,
            "head": head,
            "changed_files": sorted(changed),
            "symbols": [item.public() for item in ordered[:limit]],
            "count": min(len(ordered), limit),
        },
        workspace_id=workspace_id,
        warnings=warnings,
        truncated=len(ordered) > limit,
    )


def register_tools(mcp: FastMCP) -> None:
    annotations = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    @mcp.tool(
        name="list_symbols",
        description="List classes, functions, methods, and declarations in a workspace path.",
        annotations=annotations,
    )
    async def list_symbols_tool(
        workspace_id: str, path: str = "", query: str = "", max_results: int = 200
    ) -> dict[str, Any]:
        return list_symbols(workspace_id, path, query, max_results)

    @mcp.tool(
        name="find_definition",
        description="Find definitions using the workspace's language-aware fallback index.",
        annotations=annotations,
    )
    async def find_definition_tool(
        workspace_id: str, symbol: str, path: str = "", max_results: int = 100
    ) -> dict[str, Any]:
        return find_definition(workspace_id, symbol, path, max_results)

    @mcp.tool(
        name="find_references",
        description="Find identifier-boundary references to a symbol.",
        annotations=annotations,
    )
    async def find_references_tool(
        workspace_id: str, symbol: str, path: str = "", max_results: int = 200
    ) -> dict[str, Any]:
        return find_references(workspace_id, symbol, path, max_results)

    @mcp.tool(
        name="find_implementations",
        description="Find subclasses, interface implementors, and method implementations.",
        annotations=annotations,
    )
    async def find_implementations_tool(
        workspace_id: str, symbol: str, path: str = "", max_results: int = 100
    ) -> dict[str, Any]:
        return find_implementations(workspace_id, symbol, path, max_results)

    @mcp.tool(
        name="get_call_hierarchy",
        description="Return incoming and outgoing calls for a function or method.",
        annotations=annotations,
    )
    async def get_call_hierarchy_tool(
        workspace_id: str, symbol: str, path: str = ""
    ) -> dict[str, Any]:
        return get_call_hierarchy(workspace_id, symbol, path)

    @mcp.tool(
        name="get_diagnostics",
        description="Return parser diagnostics for supported source and configuration files.",
        annotations=annotations,
    )
    async def get_diagnostics_tool(workspace_id: str, path: str = "") -> dict[str, Any]:
        return get_diagnostics(workspace_id, path)

    @mcp.tool(
        name="get_changed_symbols",
        description="Return symbols overlapping lines changed between two Git revisions.",
        annotations=annotations,
    )
    async def get_changed_symbols_tool(
        workspace_id: str, base: str = "HEAD~1", head: str = "HEAD", max_results: int = 200
    ) -> dict[str, Any]:
        return get_changed_symbols(workspace_id, base, head, max_results)
