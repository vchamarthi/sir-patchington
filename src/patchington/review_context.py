"""Deterministic, bounded review excerpts over already authorized pinned source.

Canonical source remains local. Ranges are the authority for what the model saw.
Static call resolution intentionally abstains on unknown receivers and shadows.
"""
from __future__ import annotations

import ast
import copy
import html
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import PurePosixPath

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)


def patch_hunks(patch: str) -> list[dict]:
    """Accept complete unified hunks, including anchors for deletion-only changes."""
    blocks, current = [], None
    for row in patch.splitlines():
        match = _HUNK.match(row)
        if match:
            current = {"old": int(match[2] or 1), "new": int(match[4] or 1),
                       "start_line": int(match[3]), "rows": []}
            blocks.append(current)
        elif current is not None:
            if row.startswith(("diff --git ", "--- ", "+++ ")):
                current = None
            elif row != r"\ No newline at end of file":
                current['rows'].append(row)
    result = []
    for block in blocks:
        old = new = 0
        added = []
        valid = True
        for row in block['rows']:
            if not row or row[0] not in ' +-':
                valid = False
                break
            if row[0] in ' -':
                old += 1
            if row[0] in ' +':
                if row[0] == '+':
                    added.append(block['start_line'] + new)
                new += 1
        if not valid or old != block['old'] or new != block['new']:
            return []
        result.append({'start_line': block['start_line'],
                       'end_line': block['start_line'] + max(new - 1, 0), 'added': added})
    return result


def line_selected(source: dict, start: int, end: int | None = None) -> bool:
    """Legacy full-file rows remain valid; excerpt rows must cover every cited line."""
    end = start if end is None else end
    ranges = source.get('selected_ranges')
    return ranges is None or any(r['start_line'] <= start <= end <= r['end_line'] for r in ranges)


def _module(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix('').parts)
    if parts[0] == 'src':
        parts.pop(0)
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


def _walk_scope(node):
    """Walk one lexical function/module without borrowing calls from nested scopes."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (*_FUNCTION, ast.ClassDef, ast.Lambda)):
            yield from _walk_scope(child)


def _imports(nodes, module: str, package: bool) -> dict[str, str]:
    result = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                result[alias.asname or alias.name.split('.')[0]] = (
                    alias.name if alias.asname else alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            prefix = module.split('.') if package else module.split('.')[:-1]
            if node.level:
                if node.level > len(prefix):
                    continue
                base = prefix[:len(prefix) - node.level + 1] + (node.module or '').split('.')
            else:
                base = (node.module or '').split('.')
            for alias in node.names:
                if alias.name != '*':
                    result[alias.asname or alias.name] = '.'.join(x for x in (*base, alias.name) if x)
    return result


@dataclass
class Function:
    path: str
    name: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    parents: tuple[str, ...]
    owner_class: str | None
    calls: list[tuple[str, int]]


def _functions(items: dict[str, dict]) -> tuple[list[Function], dict[str, ast.Module]]:
    functions, trees = [], {}
    for path, item in items.items():
        if not path.endswith('.py'):
            continue
        try:
            tree = ast.parse(item['content'])
        except (SyntaxError, ValueError, RecursionError):
            continue
        trees[path] = tree
        module = _module(path)

        def visit(node, prefix, parents=(), owner_class=None, source_path=path):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, _FUNCTION):
                    name = prefix + '.' + child.name
                    functions.append(Function(source_path, name, child, parents, owner_class, []))
                    visit(child, name, (*parents, name), owner_class)
                elif isinstance(child, ast.ClassDef):
                    name = prefix + '.' + child.name
                    visit(child, name, parents, name)
                else:
                    visit(child, prefix, parents, owner_class)
        visit(tree, module)
    definitions = defaultdict(list)
    for function in functions:
        definitions[function.name].append(function)

    def bindings(function):
        nodes = list(_walk_scope(function.node))
        args = function.node.args
        names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        names.update(a.arg for a in (args.vararg, args.kwarg) if a is not None)
        names.update(n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
        names.update(n.name for n in nodes if isinstance(n, (*_FUNCTION, ast.ClassDef)))
        names.update(n.name for n in nodes if isinstance(n, ast.ExceptHandler) and n.name)
        names.update(_imports(nodes, _module(function.path), function.path.endswith('/__init__.py')))
        return names

    for function in functions:
        module = _module(function.path)
        nodes = list(_walk_scope(function.node))
        aliases = _imports(trees[function.path].body, module,
                           function.path.endswith('/__init__.py'))
        aliases.update(_imports(nodes, module, function.path.endswith('/__init__.py')))
        args = function.node.args
        shadowed = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        shadowed.update(a.arg for a in (args.vararg, args.kwarg) if a is not None)
        shadowed.update(n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
        local_defs = {n.name for n in nodes if isinstance(n, (*_FUNCTION, ast.ClassDef))}
        ancestor_bound = {name for parent in function.parents for ancestor in definitions[parent]
                          for name in bindings(ancestor)}
        local_imports = _imports(nodes, module, function.path.endswith('/__init__.py'))
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            parts, current = [], node.func
            while isinstance(current, ast.Attribute):
                parts.insert(0, current.attr)
                current = current.value
            if not isinstance(current, ast.Name):
                continue
            root, target = current.id, None
            if root in ('self', 'cls') and function.owner_class and parts:
                target = '.'.join((function.owner_class, *parts))
            elif root in local_defs:
                target = '.'.join((function.name, root, *parts))
            elif root in ancestor_bound and root not in local_imports:
                # A closure binding is not the module's similarly named import.
                continue
            elif root in aliases and root not in shadowed:
                target = '.'.join((aliases[root], *parts))
            elif not parts and root not in shadowed:
                candidates = [function.name + '.' + root] + [
                    p + '.' + root for p in reversed(function.parents)] + [module + '.' + root]
                target = next((name for name in candidates if name in definitions), None)
            if target and len(definitions.get(target, ())) == 1:
                function.calls.append((target, node.lineno))
    return functions, trees


@dataclass(frozen=True)
class Excerpt:
    path: str
    start: int
    end: int
    reason: str
    target: str = ''


def _fair(candidates: list[Excerpt]) -> list[Excerpt]:
    by_path = defaultdict(deque)
    for candidate in sorted(set(candidates), key=lambda c: (c.path, c.start, c.end, c.reason, c.target)):
        by_path[candidate.path].append(candidate)
    result = []
    while any(by_path.values()):
        for path in sorted(by_path):
            if by_path[path]:
                result.append(by_path[path].popleft())
    return result


def select_review_context(source_items: list[dict], snapshot, budget: int) -> tuple[list[dict], dict]:
    items = {item['path']: item for item in source_items}
    lines = {path: item['content'].splitlines(keepends=True) for path, item in items.items()}
    functions, trees = _functions(items)
    by_name = {f.name: f for f in functions}
    by_path = defaultdict(list)
    for function in functions:
        by_path[function.path].append(function)
    groups = {key: [] for key in ('changed_hunk', 'guard', 'test', 'caller', 'function', 'guidance')}
    changed = {f.path for f in snapshot.files}
    hunks = {f.path: patch_hunks(f.patch) for f in snapshot.files}
    changed_functions = set()

    def add(path, start, end, reason, target=''):
        if path not in lines or not lines[path]:
            return
        start, end = max(1, start), min(len(lines[path]), end)
        if start <= end:
            groups[reason].append(Excerpt(path, start, end, reason, target))

    def enclosing(path, number):
        matches = [f for f in by_path[path] if min(
            [f.node.lineno, *(d.lineno for d in f.node.decorator_list)]) <= number <= f.node.end_lineno]
        return min(matches, key=lambda f: f.node.end_lineno - f.node.lineno, default=None)

    def function_excerpt(function, reason, focus=None, target=''):
        node, path = function.node, function.path
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        if sum(len(line.encode()) for line in lines[path][start - 1:node.end_lineno]) <= 2_000:
            add(path, start, node.end_lineno, reason, target)
        else:
            add(path, start, min(node.body[0].lineno, start + 8), reason, target)
            if focus:
                add(path, max(start, focus - 5), min(node.end_lineno, focus + 8), reason, target)

    for file in snapshot.files:
        if file.path not in items:
            continue
        for hunk in hunks[file.path]:
            points = hunk['added'] or [max(1, min(len(lines[file.path]), hunk['start_line']))]
            remaining = {point for point in points if 1 <= point <= len(lines[file.path])}
            # Relationship discovery is independent of packing windows: one window may
            # cover several short functions or start in the module's imports.
            for point in remaining:
                function = enclosing(file.path, point)
                if function:
                    changed_functions.add(function.name)
            while remaining:
                point = min(remaining)
                function = enclosing(file.path, point)
                low, high = (min([function.node.lineno, *(
                    d.lineno for d in function.node.decorator_list)]), function.node.end_lineno) if function else (
                    max(1, point - 2), len(lines[file.path]))
                start, end = max(low, point - 2), min(high, point + 37)
                # Preserve the whole line; unusually long single lines are omitted by the budget.
                while end > point and sum(len(x.encode()) for x in lines[file.path][start - 1:end]) > 1_600:
                    end -= 1
                add(file.path, start, end, 'changed_hunk')
                remaining.difference_update(range(start, end + 1))
                if function:
                    changed_functions.add(function.name)
        if not hunks[file.path]:
            # Missing/invalid patch gives weak context only; it cannot authorize changed-line findings.
            add(file.path, 1, min(12, len(lines[file.path])), 'changed_hunk')
    def guards(function, focuses=()):
        # Entry checks and early returns can refute a claim even when far from its hunk.
        for node in _walk_scope(function.node):
            if isinstance(node, ast.Assert) or (isinstance(node, ast.If) and any(
                    isinstance(n, (ast.Raise, ast.Return)) for n in ast.walk(node))):
                end = node.end_lineno if node.end_lineno - node.lineno < 15 else node.lineno + 2
                add(function.path, node.lineno, end, 'guard', function.name)
            elif isinstance(node, (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.Match)) and any(
                    node.lineno <= focus <= node.end_lineno for focus in focuses):
                add(function.path, node.lineno, node.lineno + 2, 'guard', function.name)

    for name in sorted(changed_functions):
        function = by_name[name]
        function_excerpt(function, 'function', target=name)
        guards(function, [n for h in hunks[function.path] for n in h['added']
                          if function.node.lineno <= n <= function.node.end_lineno])
        for target, call_line in function.calls:
            if target in by_name:
                guard_name = by_name[target].node.name.lower()
                reason = 'guard' if any(word in guard_name for word in (
                    'guard', 'valid', 'scope', 'check', 'permission', 'require', 'verify',
                    'safe', 'signature', 'authoriz')) else 'function'
                function_excerpt(by_name[target], reason, target=target)
                add(function.path, call_line, call_line, reason, target)
    for function in functions:
        targets = [(target, line) for target, line in function.calls if target in changed_functions]
        test = ('tests' in PurePosixPath(function.path).parts
                or 'test' in PurePosixPath(function.path).parts
                or function.node.name.startswith('test_'))
        for target, line in targets:
            function_excerpt(function, 'test' if test else 'caller', line, target)
        if targets:
            guards(function, [line for _, line in targets])
            # Import bindings and decorators are part of interpreting a caller/test.
            for node in trees[function.path].body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    add(function.path, node.lineno, node.end_lineno, 'test' if test else 'caller')
    # Imports, constants and class signatures are the local contract surrounding a changed function.
    for path in sorted(changed & set(trees)):
        for node in trees[path].body:
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.ClassDef)):
                end = node.lineno if isinstance(node, ast.ClassDef) else min(node.end_lineno, node.lineno + 8)
                add(path, node.lineno, end, 'function')
    from .context import _ancestor_guidance
    for path in items:
        if _ancestor_guidance(path, changed):
            add(path, 1, min(40, len(lines[path])), 'guidance')

    selected = defaultdict(set)
    accepted = []
    used = 0

    def pack(candidates, allowance):
        nonlocal used
        spent = 0
        for candidate in _fair(candidates):
            unseen = set(range(candidate.start, candidate.end + 1)) - selected[candidate.path]
            cost = sum(len(lines[candidate.path][n - 1].encode()) for n in unseen)
            if cost <= allowance - spent and cost <= budget - used:
                selected[candidate.path].update(unseen)
                accepted.append(candidate)
                used += cost
                spent += cost

    # Reserved shares protect relationship evidence from large/new changed files.
    shares = {'changed_hunk': 45, 'guard': 10, 'test': 20, 'caller': 10,
              'function': 10, 'guidance': 5}
    for group, share in shares.items():
        pack(groups[group], budget * share // 100)
    for group in shares:
        pack(groups[group], budget - used)
    rows, selections = [], []
    for path in sorted(selected):
        numbers = sorted(selected[path])
        if not numbers:
            continue
        ranges = []
        for number in numbers:
            if ranges and ranges[-1]['end_line'] + 1 == number:
                ranges[-1]['end_line'] = number
            else:
                ranges.append({'start_line': number, 'end_line': number})
        reasons = sorted({c.reason for c in accepted if c.path == path})
        row = copy.deepcopy(items[path])
        row.update(selected_ranges=ranges, selection_reasons=reasons,
                   selected_bytes=sum(len(lines[path][n - 1].encode()) for n in numbers))
        rows.append(row)
        added = {n for h in hunks.get(path, ()) for n in h['added']}
        selections.append({'path': path, 'reasons': reasons, 'ranges': ranges,
                           'selected_lines': len(numbers), 'source_lines': len(lines[path]),
                           'added_lines': len(added), 'selected_added_lines': len(added & selected[path])})
    all_added = sum(len({n for h in hs for n in h['added']}) for hs in hunks.values())
    selected_added = sum(s['selected_added_lines'] for s in selections)
    all_hunks = [(path, h) for path, hs in hunks.items() for h in hs]
    relationships = {}
    for reason in ('guard', 'caller', 'test', 'function'):
        candidates = set(groups[reason])
        relationships[reason] = {'candidates': len(candidates),
            'selected': sum(set(range(c.start, c.end + 1)) <= selected[c.path] for c in candidates)}
    omissions = [{'path': path, 'reason': 'excerpt_budget' if path in changed or any(
        c.path == path for cs in groups.values() for c in cs) else 'not_relevant'}
        for path in items if not selected[path]]
    return rows, {'strategy': 'function-hunk-v1', 'retrieved_files': len(rows),
        'retrieved_bytes': used, 'retrieved_chunks': sum(len(s['ranges']) for s in selections),
        'changed_files': {'total': len(changed), 'available': len(changed & set(items)),
                          'with_context': sum(bool(selected[path]) for path in changed)},
        'changed_lines': {'total': all_added, 'selected': selected_added},
        'changed_hunks': {'total': len(all_hunks), 'with_context': sum(
            bool(selected[path] & set(h['added'] or [h['start_line']])) for path, h in all_hunks),
            'fully_selected': sum(bool(h['added']) and set(h['added']) <= selected[path]
                                  for path, h in all_hunks)},
        'relationships': relationships, 'selections': selections, 'omissions': omissions,
        'budget_shares': shares,
        'limitations': ['Only selected ranges were supplied; omitted lines are not citation evidence.',
            'Call edges resolve static Python names/import aliases and self/cls methods only; dynamic dispatch and unknown receivers remain unresolved.',
            'Caller/test discovery is limited to safely acquired files; no transitive call-graph claim.',
            'Guard excerpts identify static checks and called helpers, not proven runtime dominance.',
            'Non-Python source uses diff hunks without function/caller resolution.']}


def render_review_coverage(report: dict) -> str:
    """Bounded literal HTML from immutable controller coverage, never model claims."""
    def count(value):
        return value if type(value) is int and 0 <= value <= 20_000_000 else 0

    def ratio(key, selected):
        value = report.get(key, {})
        return f"{count(value.get(selected))} / {count(value.get('total'))}"

    body = ('<details><summary>Context coverage</summary>\n\n'
            f"<p>Changed files with excerpts: <strong>{ratio('changed_files', 'with_context')}</strong><br>"
            f"Added lines supplied: <strong>{ratio('changed_lines', 'selected')}</strong><br>"
            f"Hunks with changed-line context: <strong>{ratio('changed_hunks', 'with_context')}</strong><br>"
            f"Selected source: {count(report.get('retrieved_bytes')):,} bytes; "
            f"{count(report.get('retrieved_files'))} files; "
            f"{count(report.get('retrieved_chunks'))} ranges.</p>\n")
    for reason in ('guard', 'caller', 'test', 'function'):
        value = report.get('relationships', {}).get(reason, {})
        body += (f"<p>{reason.capitalize()} excerpts: {count(value.get('selected'))} / "
                 f"{count(value.get('candidates'))} candidates supplied.</p>\n")
    body += '<table><tr><th>File</th><th>Added lines supplied</th><th>Source ranges</th></tr>\n'
    for item in report.get('selections', [])[:100]:
        path = html.escape(' '.join(str(item.get('path', '')).split())[:500]).replace('@', '@\u200b')
        ranges = item.get('ranges', [])
        spans = ', '.join(f"{count(r.get('start_line'))}–{count(r.get('end_line'))}"
                          for r in ranges[:8])
        if len(ranges) > 8:
            spans += f', plus {len(ranges) - 8} ranges'
        row = (f'<tr><td><code>{path}</code></td>'
               f"<td>{count(item.get('selected_added_lines'))} / {count(item.get('added_lines'))}</td>"
               f'<td>{spans}</td></tr>\n')
        if len((body + row).encode()) > 18_000:
            break
        body += row
    body += '</table>\n'
    omissions = [(item.get('path', ''), item.get('reason', ''))
                 for item in report.get('omissions', []) if item.get('reason') != 'not_relevant']
    omissions += list(report.get('source_slice_omissions', {}).items())
    for path, reason in sorted(set(omissions))[:30]:
        path = html.escape(' '.join(str(path).split())[:500]).replace('@', '@\u200b')
        reason = html.escape(' '.join(str(reason).split())[:80])
        body += f'<p>Omitted source: <code>{path}</code> ({reason}).</p>\n'
    body += ('<p>Coverage measures supplied evidence, not review completeness. '
             'Unknown receivers, dynamic calls, excluded sources and omitted ranges remain outside '
             'static context coverage.</p>\n</details>')
    return body
