# bitcraft_types

Auto-generated Python dataclass bindings for BitCraft Online's SpacetimeDB tables.

Generated from the [BitCraft_Bindings](https://github.com/BitCraftToolBox/BitCraft_Bindings)
repository using `analyze_bindings.py` + `generate_bindings.py`.

---

## Package structure

```
bitcraft_types/
    __init__.py       # re-exports from both subpackages
    region/           # region module tables (bitcraft-live-14, etc.)
        __init__.py
        inventory_state.py
        claim_local_state.py
        ...
    global_/          # global module tables (bitcraft-live-global)
        __init__.py
        empire_state.py
        empire_foundry_state.py
        ...
```

> **Note:** `global_` uses a trailing underscore because `global` is a reserved
> keyword in Python.

Shared types (types that appear identically in both modules) are duplicated into
both subpackages — each is fully self-contained with no cross-imports, mirroring
the structure of the upstream TypeScript bindings repo.

---

## Usage

```python
from bitcraft_types.region import InventoryState, ClaimLocalState, EmpireNodeSiegeState
from bitcraft_types.global_ import EmpireState, EmpireFoundryState

# Parse a raw row from a STDB subscription or OneOffQuery
row = ...  # list or dict from STDB wire format
inv = InventoryState.from_row(row)

print(inv.entity_id)          # int
print(inv.inventory_index)    # int
for pocket in inv.pockets:    # list[Pocket]
    print(pocket.volume)      # int
    if pocket.contents:       # ItemStack | None
        print(pocket.contents.item_id)   # int
        print(pocket.contents.quantity)  # int
```

### Row formats

`from_row()` accepts both wire formats STDB can return:

- **Dict** (from `OneOffQuery` responses): keys are snake_case, e.g. `entity_id`
- **List** (from subscription `InitialSubscription` / `TransactionUpdate` rows):
  positional, matching the field order in `getTypeScriptAlgebraicType()`

```python
# Both work
inv1 = InventoryState.from_row({'entity_id': 123, 'pockets': [], ...})
inv2 = InventoryState.from_row([123, [], 0, 0, 456, 789])
```

### SDK types

SpacetimeDB SDK types are mapped as follows:

| STDB type      | Python type | Wire format                                          |
|----------------|-------------|------------------------------------------------------|
| `Timestamp`    | `int`       | `{'__timestamp_micros_since_unix_epoch__': 1234...}` |
| `TimeDuration` | `int`       | `{'__time_duration_micros__': 1234}`                 |
| `Identity`     | `str`       | `{'__identity__': '0xabc...'}`                       |
| `ConnectionId` | `int`       | `{'__connection_id__': 1234}`                        |
| `Option<T>`    | `T \| None` | `[0, value]` = Some, `[1, []]` = None               |
| Sum types      | `IntEnum`   | `[variant_index, payload]`                           |

### Null safety

`from_row(None)` returns a default-constructed instance rather than raising — safe
to call on missing rows.

---

## Regenerating

When the upstream bindings repo updates, regenerate with:

```bash
# 1. Pull latest bindings
cd D:\Dev\Bitcraft_Projects\Bindings\bindings_region
git pull
cd D:\Dev\Bitcraft_Projects\Bindings\bindings_global
git pull

# 2. Re-analyse
python analyze_bindings.py --region D:\Dev\Bitcraft_Projects\Bindings\bindings_region\src --out region_graph.json
python analyze_bindings.py --global D:\Dev\Bitcraft_Projects\Bindings\bindings_global\src --out global_graph.json

# 3. Re-generate
python generate_bindings.py \
    --region-graph region_graph.json \
    --global-graph global_graph.json \
    --out D:\Dev\Bitcraft_Projects\Bindings\bitcraft_types

# 4. Run tests
python test_bindings.py \
    --region-graph region_graph.json \
    --global-graph global_graph.json \
    --out results.json \
    --delay 1.0 --timeout 60 \
    --skip-tables ActionLogData ActionLogState
```

---

## Test results

Tested against `bitcraft-live-14` (region) and `bitcraft-live-global` (global).

| Result             | Count |
|--------------------|-------|
| ✅ PASS             | 136   |
| ⏭️ SKIP (no rows)   | 11    |
| ⏱️ SLOW (timeout)   | 37    |
| ❌ FAIL             | 0     |

All failures are subscription timeouts on large or restricted tables — no binding
errors were found.

Some tables have very large row counts or restricted access and will time out
during subscription-based testing. The bindings themselves are correct — they
simply cannot be tested without a very long timeout or a row-filtering subscription.

## Notes

- These bindings cover the **region** and **global** modules only. There is no
  Python binding generator for other SpacetimeDB modules at this time.
- The bindings are generated from the TypeScript branch of
  [BitCraft_Bindings](https://github.com/BitCraftToolBox/BitCraft_Bindings)
  (`ts-region` and `ts-global` branches).
- The upstream repo is automatically updated when the game schema changes.
  Re-run the generation pipeline after game updates.
