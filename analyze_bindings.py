#!/usr/bin/env python3
"""
analyze_bindings.py

Scans one or two SpacetimeDB TypeScript bindings directories and produces a
combined JSON graph with each type tagged by module origin.

Usage:
    # Single module
    python analyze_bindings.py --region path/to/bindings_region/src --out combined_graph.json

    # Both modules at once
    python analyze_bindings.py \
        --region D:/Dev/Bitcraft_Projects/Bindings/bindings_region/src \
        --global  D:/Dev/Bitcraft_Projects/Bindings/bindings_global/src \
        --out combined_graph.json

    # Also print full chains for specific types
    python analyze_bindings.py ... --chains-for InventoryState EmpireState
"""

import re
import json
import argparse
import sys
from pathlib import Path
from collections import defaultdict, deque

# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

IMPORT_RE = re.compile(
    r'import\s*\{\s*(\w+)\s+as\s+__(\w+)\s*\}\s*from\s*["\']\.\/(\w+)["\']'
)
TYPE_DEF_RE     = re.compile(r'export\s+type\s+(\w+)\s*=\s*\{([^}]+)\}', re.DOTALL)
FIELD_RE        = re.compile(r'(\w+)\s*:\s*(.+?)(?:,\s*$|\s*$)', re.MULTILINE)
PRODUCT_ELEM_RE = re.compile(r'new ProductTypeElement\(\s*["\'](\w+)["\']', re.MULTILINE)
SUM_VARIANT_RE  = re.compile(r'new SumTypeVariant\(\s*["\'](\w+)["\']', re.MULTILINE)
PROD_ELEM_FULL_RE = re.compile(
    r'new ProductTypeElement\(\s*["\'](\w+)["\'],\s*AlgebraicType\.(\w+)\('
)


def _snake(name: str) -> str:
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    s = re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
    return s.lower()


def parse_file(path: Path, module: str) -> dict:
    """Parse a single *_type.ts file. module is 'region' or 'global'."""
    src = path.read_text(encoding='utf-8')

    stem = path.stem
    if stem.endswith('_type'):
        stem = stem[:-5]
    type_name = ''.join(w.capitalize() for w in stem.split('_'))

    deps = [m.group(1) for m in IMPORT_RE.finditer(src)]

    is_sum   = bool(SUM_VARIANT_RE.search(src)) and 'createSumType' in src
    fields   = PRODUCT_ELEM_RE.findall(src)
    variants = SUM_VARIANT_RE.findall(src) if is_sum else []

    ts_fields = {}
    m = TYPE_DEF_RE.search(src)
    if m:
        for fm in FIELD_RE.finditer(m.group(2)):
            ts_fields[fm.group(1).strip()] = fm.group(2).strip()

    algebraic_types = {}
    for pm in PROD_ELEM_FULL_RE.finditer(src):
        algebraic_types[pm.group(1)] = pm.group(2)

    return {
        'type_name':       type_name,
        'file':            path.name,
        'module':          module,          # 'region', 'global', or 'both'
        'is_sum':          is_sum,
        'deps':            deps,
        'fields':          fields,
        'variants':        variants,
        'ts_fields':       ts_fields,
        'algebraic_types': algebraic_types,
        'snake_fields':    {_snake(f): f for f in fields},
    }


def parse_dir(path: Path, module: str) -> dict[str, dict]:
    """Parse all *_type.ts files in a directory. Returns {type_name: info}."""
    type_files = sorted(path.glob('*_type.ts'))
    print(f'  [{module}] Found {len(type_files)} type files in {path}')
    types = {}
    errors = 0
    for f in type_files:
        try:
            info = parse_file(f, module)
            types[info['type_name']] = info
        except Exception as e:
            print(f'    WARNING: Failed to parse {f.name}: {e}')
            errors += 1
    print(f'  [{module}] Parsed {len(types)} types ({errors} errors)')
    return types


# ---------------------------------------------------------------------------
# Merge — handle types that appear in both modules
# ---------------------------------------------------------------------------

def merge_types(region_types: dict, global_types: dict) -> dict[str, dict]:
    """
    Merge region and global type dicts.
    Types in both modules are tagged module='both'.
    Types only in one are tagged with that module.
    If a type exists in both with identical fields, one copy is kept.
    If fields differ, the region version is preferred (region is primary for bots).
    """
    merged   = {}
    only_region = set(region_types) - set(global_types)
    only_global = set(global_types) - set(region_types)
    in_both     = set(region_types) & set(global_types)

    for name in only_region:
        merged[name] = region_types[name]

    for name in only_global:
        merged[name] = global_types[name]

    for name in in_both:
        r = region_types[name]
        g = global_types[name]
        # Check if they're identical
        identical = (r['fields'] == g['fields'] and r['is_sum'] == g['is_sum']
                     and r['variants'] == g['variants'])
        entry = dict(r)  # prefer region
        entry['module']          = 'both' if identical else 'region'
        entry['global_differs']  = not identical
        if not identical:
            entry['global_fields']   = g['fields']
            entry['global_ts_fields'] = g['ts_fields']
        merged[name] = entry

    return merged


# ---------------------------------------------------------------------------
# Graph analysis (unchanged)
# ---------------------------------------------------------------------------

def build_graph(types: dict[str, dict]) -> dict:
    rdeps: dict[str, list[str]] = defaultdict(list)
    for name, info in types.items():
        for dep in info['deps']:
            if dep in types:
                rdeps[dep].append(name)

    depths = {}

    def depth(name, visited=None):
        if visited is None:
            visited = set()
        if name in depths:
            return depths[name]
        if name in visited:
            return 0
        visited = visited | {name}
        info = types.get(name)
        if not info:
            depths[name] = 0
            return 0
        known_deps = [dep for dep in info['deps'] if dep in types]
        if not known_deps:
            depths[name] = 0
            return 0
        d = 1 + max(depth(dep, visited) for dep in known_deps)
        depths[name] = d
        return d

    for name in types:
        depth(name)

    in_degree = {name: len([d for d in info['deps'] if d in types])
                 for name, info in types.items()}
    queue = deque(n for n, d in in_degree.items() if d == 0)
    topo  = []
    while queue:
        node = queue.popleft()
        topo.append(node)
        for rdep in rdeps.get(node, []):
            in_degree[rdep] -= 1
            if in_degree[rdep] == 0:
                queue.append(rdep)

    cycles        = [n for n in types if n not in topo]
    likely_tables = [n for n in types if n.endswith('State') or n.endswith('Data')]

    return {
        'reverse_deps':  dict(rdeps),
        'depths':        depths,
        'topo_order':    topo,
        'cycles':        cycles,
        'likely_tables': likely_tables,
    }


def find_chain(type_name: str, types: dict, visited=None) -> dict:
    if visited is None:
        visited = set()
    if type_name in visited:
        return {'type': type_name, 'circular': True}
    visited = visited | {type_name}
    info = types.get(type_name)
    if not info:
        return {'type': type_name, 'unknown': True}
    return {
        'type':     type_name,
        'module':   info.get('module', 'unknown'),
        'is_sum':   info['is_sum'],
        'fields':   info['fields'],
        'variants': info['variants'],
        'deps':     [find_chain(d, types, visited) for d in info['deps'] if d in types],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Analyze SpacetimeDB TypeScript bindings.')
    parser.add_argument('--region', metavar='DIR', help='Path to region bindings src/')
    parser.add_argument('--global', dest='global_dir', metavar='DIR',
                        help='Path to global bindings src/')
    parser.add_argument('--out', default='combined_graph.json', help='Output JSON file')
    parser.add_argument('--chains-for', nargs='+', metavar='TYPE')
    args = parser.parse_args()

    if not args.region and not args.global_dir:
        print('ERROR: Specify at least one of --region or --global')
        sys.exit(1)

    # Parse each module
    region_types = {}
    global_types = {}

    if args.region:
        p = Path(args.region)
        if not p.exists():
            print(f'ERROR: Region dir not found: {p}'); sys.exit(1)
        region_types = parse_dir(p, 'region')

    if args.global_dir:
        p = Path(args.global_dir)
        if not p.exists():
            print(f'ERROR: Global dir not found: {p}'); sys.exit(1)
        global_types = parse_dir(p, 'global')

    # Merge
    if region_types and global_types:
        print(f'\nMerging {len(region_types)} region + {len(global_types)} global types...')
        types = merge_types(region_types, global_types)
        in_both  = [n for n, t in types.items() if t['module'] in ('both', 'region') and n in global_types]
        differs  = [n for n, t in types.items() if t.get('global_differs')]
        print(f'  Shared types:    {len(in_both)}')
        print(f'  Differing defs:  {len(differs)}')
        if differs:
            print(f'  Types with different fields in region vs global:')
            for n in differs:
                t = types[n]
                print(f'    {n}: region={t["fields"]} | global={t["global_fields"]}')
    elif region_types:
        types = region_types
    else:
        types = global_types

    print(f'\nTotal unique types: {len(types)}')

    graph = build_graph(types)
    if graph['cycles']:
        print(f'WARNING: Circular deps: {graph["cycles"]}')

    depths    = graph['depths']
    max_depth = max(depths.values()) if depths else 0
    deepest   = [n for n, d in depths.items() if d == max_depth]

    print(f'Max chain depth: {max_depth} (types: {deepest})')
    print(f'Likely table types: {len(graph["likely_tables"])}')
    print(f'Topo order: {len(graph["topo_order"])} types')

    # Module breakdown
    by_module = defaultdict(list)
    for name, info in types.items():
        by_module[info.get('module', 'unknown')].append(name)
    print(f'\nModule breakdown:')
    for mod, names in sorted(by_module.items()):
        print(f'  {mod}: {len(names)} types')

    # Table breakdown by module
    tables_by_module = defaultdict(list)
    for name in graph['likely_tables']:
        tables_by_module[types[name].get('module', 'unknown')].append(name)
    print(f'\nTable types by module:')
    for mod, names in sorted(tables_by_module.items()):
        print(f'  {mod}: {len(names)} tables')

    if args.chains_for:
        for t in args.chains_for:
            match = t if t in types else next(
                (k for k in types if k.lower() == t.lower()), None
            )
            if match:
                print(f'\n--- Chain for {match} ---')
                print(json.dumps(find_chain(match, types), indent=2))
            else:
                print(f'\nWARNING: Type "{t}" not found.')

    table_chains = {t: find_chain(t, types) for t in graph['likely_tables']}

    output = {
        'summary': {
            'total_types':     len(types),
            'max_depth':       max_depth,
            'deepest_types':   deepest,
            'likely_tables':   graph['likely_tables'],
            'cycles':          graph['cycles'],
            'topo_order':      graph['topo_order'],
            'module_counts':   {k: len(v) for k, v in by_module.items()},
            'tables_by_module': {k: v for k, v in tables_by_module.items()},
        },
        'types': {
            name: {
                'file':            info['file'],
                'module':          info.get('module', 'unknown'),
                'is_sum':          info['is_sum'],
                'deps':            info['deps'],
                'fields':          info['fields'],
                'variants':        info['variants'],
                'ts_fields':       info['ts_fields'],
                'algebraic_types': info.get('algebraic_types', {}),
                'snake_fields':    info['snake_fields'],
                'depth':           depths.get(name, 0),
                'used_by':         graph['reverse_deps'].get(name, []),
                'global_differs':  info.get('global_differs', False),
            }
            for name, info in types.items()
        },
        'table_chains': table_chains,
    }

    out_path = Path(args.out)
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2)

    print(f'\nOutput written to {out_path}')
    print(f'  {len(types)} types, {len(table_chains)} table chains')


if __name__ == '__main__':
    main()
