#!/usr/bin/env python3
"""
schema_to_graph.py

Generates a bindings graph JSON directly from SpacetimeDB schema JSON files
(produced by GET /v1/database/{module}/schema?version=9).

Replaces analyze_bindings.py — no TypeScript bindings repo required.

Usage:
    # Single module
    python schema_to_graph.py --region schema_region.json --out region_graph.json
    python schema_to_graph.py --global  schema_global.json --out global_graph.json

    # Both (produces two separate graph files for generate_bindings.py)
    python schema_to_graph.py \
        --region schema_region.json \
        --global schema_global.json \
        --out-region region_graph.json \
        --out-global global_graph.json
"""

import json
import re
import argparse
import sys
from pathlib import Path
from collections import defaultdict, deque

# ---------------------------------------------------------------------------
# STDB algebraic type → createXType mapping (mirrors generate_bindings.py)
# ---------------------------------------------------------------------------

PRIMITIVE_MAP: dict[str, str] = {
    'U8':     'createU8Type',
    'U16':    'createU16Type',
    'U32':    'createU32Type',
    'U64':    'createU64Type',
    'U128':   'createU128Type',
    'U256':   'createU256Type',
    'I8':     'createI8Type',
    'I16':    'createI16Type',
    'I32':    'createI32Type',
    'I64':    'createI64Type',
    'I128':   'createI128Type',
    'I256':   'createI256Type',
    'F32':    'createF32Type',
    'F64':    'createF64Type',
    'Bool':   'createBoolType',
    'String': 'createStringType',
    'Bytes':  'createBytesType',
}


def _snake(name: str) -> str:
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    s = re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
    return s.lower()


def _is_option(sum_def: dict) -> bool:
    """True if this Sum is the STDB Option<T> pattern: exactly 'some' and 'none' variants."""
    variants = sum_def.get('Sum', {}).get('variants', [])
    if len(variants) != 2:
        return False
    names = {v['name'].get('some', '') for v in variants}
    return names == {'some', 'none'}


def _option_inner(sum_def: dict) -> dict | None:
    """Return the algebraic_type of the 'some' variant."""
    for v in sum_def['Sum']['variants']:
        if v['name'].get('some') == 'some':
            return v['algebraic_type']
    return None


def _is_simple_enum(sum_def: dict) -> bool:
    """
    True if every variant payload is a primitive or empty Product —
    i.e. this is a C-style enum that maps cleanly to Python IntEnum.
    """
    variants = sum_def.get('Sum', {}).get('variants', [])
    if not variants:
        return False
    for v in variants:
        at = v['algebraic_type']
        if 'Product' in at and at['Product']['elements']:
            return False
    return True


# ---------------------------------------------------------------------------
# Schema parser
# ---------------------------------------------------------------------------

class SchemaParser:
    def __init__(self, schema: dict, module_tag: str):
        self.module_tag  = module_tag
        self.typespace   = schema['typespace']['types']
        # ref index → type name
        self.ref_to_name: dict[int, str] = {
            t['ty']: t['name']['name'] for t in schema['types']
        }
        # all named type names (set for O(1) lookup)
        self.known_names: set[str] = set(self.ref_to_name.values())
        # table name → product_type_ref, for tagging table types later
        self.table_refs: set[int] = {t['product_type_ref'] for t in schema['tables']}

    # ------------------------------------------------------------------
    # Field type classification
    # ------------------------------------------------------------------

    def _classify(self, at: dict) -> tuple[str | None, str | None, list[str]]:
        """
        Returns (algebraic_method, ts_type, deps).

        algebraic_method — 'createU64Type' etc., used by generate_bindings.py for primitives.
        ts_type          — TypeScript-style string for complex types ('Foo | undefined', 'Foo[]', 'Foo').
        deps             — named types this field references.

        At most one of algebraic_method / ts_type is set.
        """
        # Primitives
        for prim, method in PRIMITIVE_MAP.items():
            if prim in at:
                return method, None, []

        # Named reference
        if 'Ref' in at:
            ref_idx   = at['Ref']
            type_name = self.ref_to_name.get(ref_idx)
            if type_name is None:
                return None, 'object', []

            ref_def = self.typespace[ref_idx]

            # Transparent Option<T> — unwrap to 'T | undefined'
            if 'Sum' in ref_def and _is_option(ref_def):
                inner_at = _option_inner(ref_def)
                if inner_at is not None:
                    # Option<NamedType>
                    if 'Ref' in inner_at:
                        inner_name = self.ref_to_name.get(inner_at['Ref'])
                        if inner_name:
                            return None, f'{inner_name} | undefined', [type_name, inner_name]
                    # Option<Primitive>
                    for prim, method in PRIMITIVE_MAP.items():
                        if prim in inner_at:
                            return None, f'{prim.lower()} | undefined', [type_name]
                return None, f'{type_name} | undefined', [type_name]

            return None, type_name, [type_name]

        # Array
        if 'Array' in at:
            inner = at['Array']
            # Array<Primitive>
            for prim, method in PRIMITIVE_MAP.items():
                if prim in inner:
                    if prim == 'U8':
                        return 'createBytesType', None, []  # U8[] → bytes
                    return None, f'{prim.lower()}[]', []
            # Array<Ref>
            if 'Ref' in inner:
                type_name = self.ref_to_name.get(inner['Ref'])
                if type_name:
                    return None, f'{type_name}[]', [type_name]
            return None, 'list', []

        # Inline Option (Sum embedded directly in the element, not via Ref)
        if 'Sum' in at and _is_option(at):
            inner_at = _option_inner(at)
            if inner_at is not None and 'Ref' in inner_at:
                inner_name = self.ref_to_name.get(inner_at['Ref'])
                if inner_name:
                    return None, f'{inner_name} | undefined', [inner_name]
            return None, 'object | undefined', []

        return None, 'object', []

    # ------------------------------------------------------------------
    # Per-type parsing
    # ------------------------------------------------------------------

    def _parse_product(self, elements: list) -> dict:
        fields          = []
        ts_fields       = {}
        algebraic_types = {}
        all_deps        = []

        for el in elements:
            name = el['name'].get('some', '')
            if not name:
                continue
            at                    = el['algebraic_type']
            alg, ts_t, deps       = self._classify(at)
            fields.append(name)
            if alg:
                algebraic_types[name] = alg
            if ts_t:
                ts_fields[name] = ts_t
            all_deps.extend(deps)

        # Deduplicate deps while preserving order; keep only known named types
        seen       = set()
        unique_deps = []
        for d in all_deps:
            if d not in seen and d in self.known_names:
                seen.add(d)
                unique_deps.append(d)

        return {
            'is_sum':          False,
            'deps':            unique_deps,
            'fields':          fields,
            'variants':        [],
            'ts_fields':       ts_fields,
            'algebraic_types': algebraic_types,
            'snake_fields':    {_snake(f): f for f in fields},
            'module':          self.module_tag,
        }

    def _parse_sum(self, sum_def: dict) -> dict:
        variants = [
            v['name'].get('some', f'Variant{i}')
            for i, v in enumerate(sum_def['Sum']['variants'])
        ]
        return {
            'is_sum':          True,
            'deps':            [],
            'fields':          [],
            'variants':        variants,
            'ts_fields':       {},
            'algebraic_types': {},
            'snake_fields':    {},
            'module':          self.module_tag,
        }

    def parse_all(self) -> dict[str, dict]:
        types = {}
        for entry in self._schema_types:
            name    = entry['name']['name']
            ref_idx = entry['ty']
            type_def = self.typespace[ref_idx]

            if 'Product' in type_def:
                info = self._parse_product(type_def['Product']['elements'])
            elif 'Sum' in type_def:
                info = self._parse_sum(type_def)
            else:
                info = {
                    'is_sum': False, 'deps': [], 'fields': [], 'variants': [],
                    'ts_fields': {}, 'algebraic_types': {}, 'snake_fields': {},
                    'module': self.module_tag,
                }
            types[name] = info
        return types

    def _attach_schema(self, schema: dict):
        self._schema_types = schema['types']


# ---------------------------------------------------------------------------
# Graph builder (mirrors analyze_bindings.py's build_graph)
# ---------------------------------------------------------------------------

def build_graph(types: dict[str, dict]) -> dict:
    rdeps: dict[str, list[str]] = defaultdict(list)
    for name, info in types.items():
        for dep in info['deps']:
            if dep in types:
                rdeps[dep].append(name)

    depths: dict[str, int] = {}

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
        known_deps = [d for d in info['deps'] if d in types]
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
    likely_tables = [n for n in types if n.endswith('State') or n.endswith('Desc') or n.endswith('Data')]

    return {
        'reverse_deps':  dict(rdeps),
        'depths':        depths,
        'topo_order':    topo + cycles,  # append cycles so everything is included
        'cycles':        cycles,
        'likely_tables': likely_tables,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def process_schema(schema_path: Path, module_tag: str) -> dict:
    with open(schema_path) as f:
        schema = json.load(f)

    parser = SchemaParser(schema, module_tag)
    parser._attach_schema(schema)
    types  = parser.parse_all()

    print(f'  [{module_tag}] {len(types)} types parsed from {schema_path.name}')

    graph  = build_graph(types)

    if graph['cycles']:
        print(f'  [{module_tag}] WARNING: {len(graph["cycles"])} circular deps: {graph["cycles"][:5]}')

    depths    = graph['depths']
    max_depth = max(depths.values(), default=0)
    deepest   = [n for n, d in depths.items() if d == max_depth]

    by_module = defaultdict(list)
    for name, info in types.items():
        by_module[info.get('module', 'unknown')].append(name)

    print(f'  [{module_tag}] max chain depth: {max_depth} ({deepest[:3]})')
    print(f'  [{module_tag}] likely tables: {len(graph["likely_tables"])}')
    print(f'  [{module_tag}] topo order: {len(graph["topo_order"])} types')

    return {
        'summary': {
            'total_types':    len(types),
            'max_depth':      max_depth,
            'deepest_types':  deepest,
            'likely_tables':  graph['likely_tables'],
            'cycles':         graph['cycles'],
            'topo_order':     graph['topo_order'],
            'module_counts':  {k: len(v) for k, v in by_module.items()},
        },
        'types': {
            name: {
                'file':            f'{_snake(name)}_type.ts',  # kept for compat
                'module':          info.get('module', module_tag),
                'is_sum':          info['is_sum'],
                'deps':            info['deps'],
                'fields':          info['fields'],
                'variants':        info['variants'],
                'ts_fields':       info['ts_fields'],
                'algebraic_types': info['algebraic_types'],
                'snake_fields':    info['snake_fields'],
                'depth':           depths.get(name, 0),
                'used_by':         graph['reverse_deps'].get(name, []),
                'global_differs':  False,
            }
            for name, info in types.items()
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Generate bindings graph from STDB schema JSON.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''\
Examples:
  python schema_to_graph.py --region schema_region.json --out region_graph.json
  python schema_to_graph.py --global  schema_global.json --out global_graph.json
  python schema_to_graph.py \\
      --region schema_region.json --global schema_global.json \\
      --out-region region_graph.json --out-global global_graph.json
''')
    parser.add_argument('--region',     metavar='FILE', help='Region schema JSON path')
    parser.add_argument('--global',     dest='global_schema', metavar='FILE',
                        help='Global schema JSON path')
    parser.add_argument('--out',        metavar='FILE', help='Output graph (single module)')
    parser.add_argument('--out-region', metavar='FILE', help='Output region graph')
    parser.add_argument('--out-global', metavar='FILE', help='Output global graph')
    args = parser.parse_args()

    if not args.region and not args.global_schema:
        print('ERROR: specify --region and/or --global')
        sys.exit(1)

    if args.region and args.global_schema and not args.out_region and not args.out_global and not args.out:
        print('ERROR: with both modules, use --out-region / --out-global (or --out for one)')
        sys.exit(1)

    if args.region:
        graph = process_schema(Path(args.region), 'region')
        out   = Path(args.out_region or args.out or 'region_graph.json')
        with open(out, 'w') as f:
            json.dump(graph, f, indent=2)
        print(f'  region graph → {out}')

    if args.global_schema:
        graph = process_schema(Path(args.global_schema), 'global')
        out   = Path(args.out_global or (args.out if not args.region else 'global_graph.json'))
        with open(out, 'w') as f:
            json.dump(graph, f, indent=2)
        print(f'  global graph → {out}')

    print('Done.')


if __name__ == '__main__':
    main()
