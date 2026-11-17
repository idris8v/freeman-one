#!/usr/bin/env python3
"""Offline source inventory and link/syntax checks; never reads .env or data/.

The manifest proves automated coverage, not a manual review of every line.
Run with the project Python environment and Node on PATH (or --node PATH).
"""
import argparse
import ast
from collections import Counter, defaultdict
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class AssetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'link', 'img', 'source'):
            for name in ('src', 'href'):
                if name in attrs:
                    self.links.append(attrs[name])


def source_paths():
    result = subprocess.check_output(
        ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT
    )
    return sorted({name.decode() for name in result.split(b'\0') if name and (ROOT / name.decode()).is_file()})


def walk_routes(routes, prefix=''):
    for route in routes:
        # FastAPI 0.143 defers include_router expansion; older versions flatten.
        if hasattr(route, 'original_router'):
            context = route.include_context
            yield from walk_routes(route.original_router.routes, prefix + context.prefix)
        elif getattr(route, 'methods', None):
            yield route, prefix + route.path


def audit(node):
    paths = source_paths()
    modules = {}
    for name in paths:
        if name.endswith('.py'):
            module = name[:-3].replace('/', '.')
            if module.endswith('.__init__'):
                module = module[:-9]
            modules[module] = name
    manifest, errors, edges, duplicates = [], [], defaultdict(set), defaultdict(list)
    warnings = []
    for name in paths:
        raw = (ROOT / name).read_bytes()
        entry = {'path': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'checks': ['read/hash']}
        manifest.append(entry)
        try:
            content = raw.decode('utf-8')
        except UnicodeDecodeError:
            entry['kind'] = 'binary'
            continue
        entry.update(kind='text', lines=len(content.splitlines()))
        if name.endswith('.py'):
            try:
                tree = ast.parse(content, filename=name)
            except SyntaxError as error:
                errors.append({'path': name, 'line': error.lineno, 'kind': 'python_syntax'})
                continue
            entry['checks'].extend(['Python AST', 'local imports', 'exact function duplicates', 'unreachable statements'])
            package = name[:-3].replace('/', '.')
            package = package[:-9] if package.endswith('.__init__') else package.rpartition('.')[0]
            imports = []
            for item in ast.walk(tree):
                if isinstance(item, ast.Import):
                    imports.extend(alias.name for alias in item.names)
                elif isinstance(item, ast.ImportFrom):
                    target = item.module or ''
                    if item.level:
                        target = importlib.util.resolve_name('.' * item.level + target, package)
                    imports.append(target)
                    imports.extend(target + '.' + alias.name for alias in item.names)
                elif isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == '__import__':
                    if item.args and isinstance(item.args[0], ast.Constant) and isinstance(item.args[0].value, str):
                        imports.append(item.args[0].value)
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(item.body) > 1:
                    fingerprint = hashlib.sha256(ast.dump(ast.Module(body=item.body, type_ignores=[]), include_attributes=False).encode()).hexdigest()
                    duplicates[fingerprint].append({'path': name, 'function': item.name, 'line': item.lineno})
                for _, value in ast.iter_fields(item):
                    if isinstance(value, list) and value and all(isinstance(v, ast.stmt) for v in value):
                        stopped = False
                        for statement in value:
                            if stopped:
                                errors.append({'path': name, 'line': statement.lineno, 'kind': 'unreachable_statement'})
                            if isinstance(statement, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                                stopped = True
            for target in imports:
                if target in modules:
                    edges[name].add(modules[target])
                elif target.startswith('app.'):
                    # Imported symbols need not be separate modules.
                    parent = target.rpartition('.')[0]
                    if parent in modules:
                        edges[name].add(modules[parent])
                    else:
                        errors.append({'path': name, 'kind': 'unresolved_local_import', 'target': target})
        elif name.endswith(('.js', '.cjs')):
            entry['checks'].append('node --check')
            result = subprocess.run([node, '--check', str(ROOT / name)], capture_output=True, text=True)
            if result.returncode:
                errors.append({'path': name, 'kind': 'javascript_syntax', 'detail': result.stderr[:1200]})
        elif name.endswith('.html'):
            entry['checks'].append('HTML local asset links')
            parser = AssetLinks()
            parser.feed(content)
            for link in parser.links:
                path = urlsplit(link).path
                if path.startswith('/assets/') and not (ROOT / 'app/web/static' / path[8:]).is_file():
                    errors.append({'path': name, 'kind': 'missing_asset', 'target': path})
        # Only report locations, never the credential values.
        if not name.startswith('tests/'):
            for number, line in enumerate(content.splitlines(), 1):
                if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}|\b\d{8,12}:[A-Za-z0-9_-]{30,}', line):
                    warnings.append({'path': name, 'line': number, 'kind': 'potential_credential_review_required'})

    # Construct routes without starting lifespan, workers, external APIs or DB.
    from app.config.settings import Settings
    from app.main import create_app
    app = create_app(Settings(_env_file=None, database_url='sqlite+aiosqlite:///:memory:', bot_token='', freeman_ai_enabled=False))
    routes = []
    seen = set()
    for route, path in walk_routes(app.routes):
        for method in sorted(getattr(route, 'methods', ())):
            key = (method, path)
            if key in seen:
                errors.append({'kind': 'duplicate_route', 'method': method, 'path': path})
            seen.add(key)
            routes.append({'method': method, 'path': path})
    return {
        'coverage': 'All versioned and non-ignored working source files read; checks listed per file. Automated coverage is not manual line-by-line approval.',
        'summary': {'files': len(manifest), 'text_lines': sum(m.get('lines', 0) for m in manifest), 'extensions': dict(Counter(Path(m['path']).suffix for m in manifest)), 'route_methods': len(routes), 'errors': len(errors), 'review_warnings': len(warnings)},
        'files': manifest, 'import_graph': {key: sorted(value) for key, value in sorted(edges.items())},
        'routes': routes, 'exact_function_duplicate_candidates': [group for group in duplicates.values() if len(group) > 1],
        'errors': errors, 'review_warnings': warnings,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', default='node')
    parser.add_argument('--output', default='artifacts/code-audit-20261012/source-manifest.json')
    options = parser.parse_args()
    result = audit(options.node)
    output = ROOT / options.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result['summary'], ensure_ascii=False))
    for item in result['errors']:
        print(json.dumps(item, ensure_ascii=False))
    sys.exit(bool(result['errors']))
