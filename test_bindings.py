#!/usr/bin/env python3
"""
test_bindings.py

Tests the generated bitcraft_types bindings against live STDB data from Region 14.
For each table type:
  - Queries one row from STDB
  - Runs from_row() on it
  - Checks field types match annotations
  - Reports PASS / SKIP (no rows) / FAIL (exception or type mismatch)

Usage:
    python test_bindings.py
    python test_bindings.py --delay 0.3     # seconds between queries (default 0.5)
    python test_bindings.py --table ClaimLocalState InventoryState  # test specific tables
    python test_bindings.py --out results.json  # save results to JSON

Requires:
    - bitcraft_types/ in D:\\Dev\\Bitcraft_Projects\\Bindings\\
    - regions.json at ../shared/data/regions.json (relative to this script)
"""

import sys
import json
import time
import uuid
import threading
import argparse
import traceback
import importlib
from pathlib import Path
from dataclasses import fields as dc_fields
from enum import IntEnum

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_HERE       = Path(__file__).parent
_SHARED_DIR = _HERE.parent / 'shared'
_TYPES_DIR  = Path('D:/Dev/Bitcraft_Projects/Bindings/bitcraft_types')

sys.path.insert(0, str(_TYPES_DIR.parent))  # so "import bitcraft_types" works

try:
    from websockets import Subprotocol
    from websockets.sync.client import connect
except ImportError:
    print('ERROR: websockets not installed. Run: pip install websockets')
    sys.exit(1)

# ---------------------------------------------------------------------------
# STDB connection (reused from query_stdb.py)
# ---------------------------------------------------------------------------

STDB_HOST     = 'bitcraft-early-access.spacetimedb.com'
STDB_PROTO    = Subprotocol('v1.json.spacetimedb')
STDB_URI      = 'wss://{host}/v1/database/{module}/subscribe'

REGIONS_FILE  = _SHARED_DIR / 'data' / 'regions.json'
REGION_MODULE = 'bitcraft-live-14'
GLOBAL_MODULE = 'bitcraft-live-global'


def _get_token(module: str = REGION_MODULE, cli_token: str | None = None) -> str:
    if cli_token:
        return cli_token
    try:
        with open(REGIONS_FILE) as f:
            regions = [r for r in json.load(f) if r.get('enabled') and r.get('token')]
        for r in regions:
            if r['module'] == module:
                return r['token']
        if regions:
            return regions[0]['token']
    except FileNotFoundError:
        pass
    raise RuntimeError(f'No token found for {module} — provide --token or set up regions.json')


class StdbConn:
    def __init__(self, module: str, auth: str):
        self.module = module
        self._auth  = auth
        self._ws    = None
        self._lock  = threading.Lock()
        self._pending: dict = {}
        self._results: dict = {}

        url = STDB_URI.format(host=STDB_HOST, module=module)
        print(f'Connecting to {module}...', end=' ', flush=True)
        self._ws = connect(
            url,
            additional_headers={'Authorization': auth},
            subprotocols=[STDB_PROTO],
            max_size=None,
            max_queue=None,
        )
        self._ws.recv()
        print('connected.')

        t = threading.Thread(target=self._recv_loop, daemon=True)
        t.start()

    def _recv_loop(self):
        try:
            while True:
                raw  = self._ws.recv()
                data = json.loads(raw)
                if 'OneOffQueryResponse' in data:
                    resp   = data['OneOffQueryResponse']
                    msg_id = resp['message_id']
                    if resp['error'].get('some'):
                        self._results[msg_id] = (None, resp['error']['some'])
                    else:
                        rows = []
                        for t in resp.get('tables', []):
                            rows.extend(json.loads(row) for row in t.get('rows', []))
                        self._results[msg_id] = (rows, None)
                    ev = self._pending.get(msg_id)
                    if ev:
                        ev.set()
        except Exception:
            pass

    def query_one(self, table: str, timeout: float = 30.0) -> list:
        """
        Fetch one row from a table using a short-lived subscription.
        Returns the first row from InitialSubscription, or [] if the table is empty
        or not in this module. Raises RuntimeError on query errors.
        """
        result  = [None]   # [rows_list] or [None]
        error   = [None]   # [error_string] or [None]
        done    = threading.Event()
        req_id  = abs(hash(table)) % (2**31)

        def _run():
            try:
                url = STDB_URI.format(host=STDB_HOST, module=self.module)
                ws  = connect(
                    url,
                    additional_headers={'Authorization': self._auth},
                    subprotocols=[STDB_PROTO],
                    max_size=None,
                    max_queue=None,
                    open_timeout=10,
                    close_timeout=5,
                )
                ws.recv()  # discard IdentityToken
                ws.send(json.dumps({'Subscribe': {
                    'query_strings': [f'SELECT * FROM {table};'],
                    'request_id':    req_id,
                }}))

                while not done.is_set():
                    try:
                        raw  = ws.recv(timeout=2.0)
                        data = json.loads(raw)
                    except TimeoutError:
                        continue
                    except Exception:
                        break

                    if 'InitialSubscription' in data:
                        tables = data['InitialSubscription'].get('database_update', {}).get('tables', [])
                        for t in tables:
                            if t.get('table_name') == table:
                                updates = t.get('updates', [])
                                if updates:
                                    inserts = updates[0].get('inserts', [])
                                    if inserts:
                                        try:
                                            result[0] = [json.loads(inserts[0])]
                                        except Exception:
                                            result[0] = [inserts[0]]
                        if result[0] is None:
                            result[0] = []  # table exists but is empty
                        break

                    if 'SubscribeError' in data or 'Error' in data:
                        msg      = data.get('SubscribeError') or data.get('Error') or {}
                        err_text = msg.get('message', str(msg))
                        error[0] = err_text
                        break

                try:
                    ws.close()
                except Exception:
                    pass
            except Exception as e:
                error[0] = str(e)
            finally:
                done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if not done.is_set():
            done.set()  # signal thread to stop
            raise TimeoutError(f'Timeout querying {table}')

        if error[0]:
            if 'no such table' in error[0].lower():
                raise RuntimeError(f'no such table: {table}')
            raise RuntimeError(f'Query error on {table}: {error[0]}')

        return result[0] if result[0] is not None else []

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Type checking helpers
# ---------------------------------------------------------------------------

def _check_field_types(instance, type_class) -> list[str]:
    """
    Walk the dataclass fields and check that the parsed values have
    compatible types. Returns a list of mismatch descriptions (empty = OK).
    """
    issues = []
    for f in dc_fields(instance):
        val = getattr(instance, f.name)
        ann = f.type  # string annotation due to from __future__ import annotations

        # We do lightweight checks — just flag obviously wrong types
        if val is None:
            continue  # None is valid for Optional fields

        if isinstance(val, list):
            continue  # list items are their own nested types, spot-check separately

        if isinstance(val, IntEnum):
            continue  # enums are fine

        if isinstance(val, object) and hasattr(val, 'from_row'):
            # Nested dataclass — recurse
            nested_issues = _check_field_types(val, type(val))
            issues.extend(f'{f.name}.{i}' for i in nested_issues)
            continue

        # Basic type checks
        if 'int' in str(ann) and not isinstance(val, (int, float)):
            issues.append(f'{f.name}: expected int, got {type(val).__name__}({val!r})')
        elif str(ann) == 'float' and not isinstance(val, (int, float)):
            issues.append(f'{f.name}: expected float, got {type(val).__name__}({val!r})')
        elif str(ann) == 'bool' and not isinstance(val, bool):
            issues.append(f'{f.name}: expected bool, got {type(val).__name__}({val!r})')
        elif str(ann) == 'str' and not isinstance(val, str):
            issues.append(f'{f.name}: expected str, got {type(val).__name__}({val!r})')

    return issues


# ---------------------------------------------------------------------------
# Discover table types from the graph
# ---------------------------------------------------------------------------

def _load_table_types(graph_path: Path) -> list[str]:
    """Return the list of likely table type names from the graph JSON."""
    with open(graph_path) as f:
        graph = json.load(f)
    return graph['summary']['likely_tables']


def _snake(name: str) -> str:
    import re
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    s = re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
    return s.lower()


def _table_name_for(type_name: str) -> str:
    """Convert PascalCase type name to snake_case table name."""
    return _snake(type_name)


def _import_type(type_name: str, subpackage: str | None = None):
    """
    Import a type class from bitcraft_types.
    If subpackage is given ('region' or 'global_'), try that first.
    Otherwise tries region then global_.
    """
    mod_name = _snake(type_name)
    packages = [subpackage] if subpackage else ['region', 'global_']
    last_err = None
    for pkg in packages:
        try:
            mod = importlib.import_module(f'bitcraft_types.{pkg}.{mod_name}')
            return getattr(mod, type_name)
        except (ImportError, AttributeError) as e:
            last_err = e
    raise ImportError(f'Could not import {type_name}: {last_err}')


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------

RESULT_PASS = 'PASS'
RESULT_SKIP = 'SKIP'
RESULT_FAIL = 'FAIL'


def run_tests(table_types: list[dict], conn_map: dict, delay: float, timeout: float = 30.0) -> list[dict]:
    """
    table_types: list of {'type': str, 'module': str} dicts
    conn_map: {'region': StdbConn, 'global': StdbConn}
    """
    results = []
    total   = len(table_types)

    for i, entry in enumerate(table_types):
        type_name  = entry['type']
        mod        = entry['module']   # 'region', 'global', or 'both'
        subpkg     = 'global_' if mod == 'global' else 'region'
        stdb_module = GLOBAL_MODULE if mod == 'global' else REGION_MODULE
        conn       = conn_map.get('global' if mod == 'global' else 'region')
        table_name = _table_name_for(type_name)
        prefix     = f'[{i+1}/{total}] {type_name} ({mod})'

        if conn is None:
            print(f'{prefix} ... SKIP (no connection for {mod} module)')
            results.append({'type': type_name, 'table': table_name, 'module': mod,
                            'result': RESULT_SKIP, 'reason': f'no {mod} connection'})
            continue

        # Import the generated type
        try:
            type_class = _import_type(type_name, subpkg)
        except ImportError as e:
            print(f'{prefix} ... FAIL (import error: {e})')
            results.append({'type': type_name, 'table': table_name, 'module': mod,
                            'result': RESULT_FAIL, 'error': str(e)})
            time.sleep(delay)
            continue

        # Query one row
        try:
            rows = conn.query_one(table_name, timeout=timeout)
        except TimeoutError:
            print(f'{prefix} ... FAIL (timeout)')
            results.append({'type': type_name, 'table': table_name, 'module': mod,
                            'result': RESULT_FAIL, 'error': 'timeout'})
            time.sleep(delay)
            continue
        except RuntimeError as e:
            err = str(e)
            if 'no such table' in err.lower():
                print(f'{prefix} ... SKIP (not in {mod} module)')
                results.append({'type': type_name, 'table': table_name, 'module': mod,
                                'result': RESULT_SKIP, 'reason': f'not in {mod} module'})
            else:
                print(f'{prefix} ... FAIL ({err})')
                results.append({'type': type_name, 'table': table_name, 'module': mod,
                                'result': RESULT_FAIL, 'error': err})
            time.sleep(delay)
            continue

        if not rows:
            print(f'{prefix} ... SKIP (no rows)')
            results.append({'type': type_name, 'table': table_name, 'module': mod,
                            'result': RESULT_SKIP, 'reason': 'no rows'})
            time.sleep(delay)
            continue

        raw = rows[0]
        try:
            instance = type_class.from_row(raw)
        except Exception as e:
            print(f'{prefix} ... FAIL (from_row raised: {e})')
            results.append({'type': type_name, 'table': table_name, 'module': mod,
                            'result': RESULT_FAIL, 'error': f'from_row: {e}',
                            'traceback': traceback.format_exc()})
            time.sleep(delay)
            continue

        issues = _check_field_types(instance, type_class)
        if issues:
            print(f'{prefix} ... FAIL (type mismatch: {issues})')
            results.append({'type': type_name, 'table': table_name, 'module': mod,
                            'result': RESULT_FAIL, 'error': f'type mismatch: {issues}'})
        else:
            print(f'{prefix} ... PASS')
            results.append({'type': type_name, 'table': table_name, 'module': mod,
                            'result': RESULT_PASS})

        time.sleep(delay)

    return results


def print_summary(results: list[dict]):
    passed  = [r for r in results if r['result'] == RESULT_PASS]
    skipped = [r for r in results if r['result'] == RESULT_SKIP]
    failed  = [r for r in results if r['result'] == RESULT_FAIL]

    print()
    print('=' * 60)
    print(f'RESULTS: {len(passed)} passed, {len(skipped)} skipped, {len(failed)} failed')
    print('=' * 60)

    # Module breakdown
    for mod in ('region', 'global', 'both'):
        mod_results = [r for r in results if r.get('module') == mod]
        if mod_results:
            p = sum(1 for r in mod_results if r['result'] == RESULT_PASS)
            s = sum(1 for r in mod_results if r['result'] == RESULT_SKIP)
            f = sum(1 for r in mod_results if r['result'] == RESULT_FAIL)
            print(f'  {mod:8s}: {p} passed, {s} skipped, {f} failed')

    if skipped:
        no_rows    = [r for r in skipped if r.get('reason') == 'no rows']
        not_in_mod = [r for r in skipped if r.get('reason', '').startswith('not in')]
        if no_rows:
            print(f'\nSKIPPED (no rows) [{len(no_rows)}]:')
            for r in no_rows:
                print(f'  {r["type"]}')
        if not_in_mod:
            print(f'\nSKIPPED (not in module) [{len(not_in_mod)}]:')
            for r in not_in_mod:
                print(f'  {r["type"]}')

    if failed:
        print(f'\nFAILED [{len(failed)}]:')
        for r in failed:
            print(f'  {r["type"]} ({r.get("module","?")}): {r.get("error", "unknown")}')


def main():
    parser = argparse.ArgumentParser(description='Test bitcraft_types bindings against live STDB.')
    parser.add_argument('--token', metavar='TOKEN',
                        help='STDB auth token (overrides regions.json lookup)')
    parser.add_argument('--delay', type=float, default=0.5,
                        help='Seconds between queries (default: 0.5)')
    parser.add_argument('--timeout', type=float, default=30.0,
                        help='Query timeout in seconds (default: 30)')
    parser.add_argument('--table', nargs='+', metavar='TYPE',
                        help='Test specific type names only')
    parser.add_argument('--skip-tables', nargs='+', metavar='TYPE',
                        help='Skip specific type names')
    parser.add_argument('--out', metavar='FILE',
                        help='Save full results to JSON file')
    parser.add_argument('--region-graph', default='region_graph.json',
                        help='Path to region graph JSON (default: region_graph.json)')
    parser.add_argument('--global-graph', metavar='FILE',
                        help='Path to global graph JSON')
    args = parser.parse_args()

    # Load table lists from graphs
    table_entries = []  # list of {'type': str, 'module': str}

    region_graph_path = Path(args.region_graph)
    if region_graph_path.exists():
        with open(region_graph_path) as f:
            rg = json.load(f)
        region_tables = rg['summary']['likely_tables']
        region_module_map = {t: rg['types'][t].get('module', 'region')
                             for t in region_tables if t in rg['types']}
        for t in region_tables:
            mod = region_module_map.get(t, 'region')
            # 'both' tables go through region connection
            table_entries.append({'type': t, 'module': 'region' if mod in ('region', 'both') else mod})
    else:
        print(f'WARNING: {region_graph_path} not found')

    if args.global_graph:
        gp = Path(args.global_graph)
        if gp.exists():
            with open(gp) as f:
                gg = json.load(f)
            global_tables = gg['summary']['likely_tables']
            existing = {e['type'] for e in table_entries}
            for t in global_tables:
                mod = gg['types'].get(t, {}).get('module', 'global')
                if mod == 'global' and t not in existing:
                    table_entries.append({'type': t, 'module': 'global'})
        else:
            print(f'WARNING: {gp} not found')

    if not table_entries:
        print('ERROR: No tables to test.')
        sys.exit(1)

    # Apply filters
    if args.table:
        keep = set(args.table)
        table_entries = [e for e in table_entries if e['type'] in keep]

    if args.skip_tables:
        skip_set     = set(args.skip_tables)
        table_entries = [e for e in table_entries if e['type'] not in skip_set]
        print(f'Skipping: {", ".join(sorted(skip_set))}')

    needs_region = any(e['module'] in ('region', 'both') for e in table_entries)
    needs_global = any(e['module'] == 'global' for e in table_entries)

    print(f'Testing {len(table_entries)} table type(s)')
    print(f'  Region module: {REGION_MODULE}' if needs_region else '')
    print(f'  Global module: {GLOBAL_MODULE}' if needs_global else '')
    print(f'Query delay: {args.delay}s  Timeout: {args.timeout}s\n')

    conn_map = {}
    try:
        if needs_region:
            conn_map['region'] = StdbConn(REGION_MODULE, _get_token(REGION_MODULE, args.token))
        if needs_global:
            conn_map['global'] = StdbConn(GLOBAL_MODULE, _get_token(GLOBAL_MODULE, args.token))

        results = run_tests(table_entries, conn_map, args.delay, args.timeout)
    finally:
        for c in conn_map.values():
            c.close()

    print_summary(results)

    if args.out:
        out_path = Path(args.out)
        with open(out_path, 'w') as f:
            json.dump(results, f, indent=2)
        print(f'\nFull results saved to {out_path}')


if __name__ == '__main__':
    main()
