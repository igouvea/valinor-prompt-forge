# Benchmark: json-patch (RFC 6902)

Implement a JSON Patch applier following **RFC 6902** (operations) and **RFC 6901**
(JSON Pointer).

## Module contract (exact — the hidden test suite imports this)

Create `src/index.js` as an **ES module** that exports:

```js
export function applyPatch(document, operations) { /* ... */ }
```

- `document` — any JSON value (object, array, string, number, boolean, null).
- `operations` — an array of patch operation objects.
- Returns the **new document** with all operations applied, in order.
- **Must NOT mutate** the input `document`.
- On any error, **throw** an `Error`. The patch is **atomic**: if any operation
  fails, the caller must observe no changes (apply to a copy; throw on failure).

## Operations (RFC 6902)

Each operation has an `op` and a `path` (a JSON Pointer). Some have `value` or `from`.

- `add` — add `value` at `path`.
  - Object member: create it, or **replace** it if it already exists.
  - Array index: **insert** at that index (shifting later elements). Index equal
    to the array length **appends**. The token `-` also appends. An index greater
    than the length is an error.
  - Path `""` replaces the **whole document** with `value`.
- `remove` — remove the value at `path`. Error if it does not exist. Array
  removal shifts later elements down.
- `replace` — replace the value at `path` with `value`. Error if it does not exist.
- `move` — `{ op, from, path }`. Remove the value at `from`, then add it at `path`.
- `copy` — `{ op, from, path }`. Add a copy of the value at `from` to `path`.
- `test` — `{ op, path, value }`. Error unless the value at `path` is **deeply
  equal** to `value`. On success the document is unchanged.
- Any unknown `op` is an error.

## JSON Pointer (RFC 6901)

- `""` → the whole document.
- `"/foo"` → member `foo`; `"/foo/0"` → index 0 of array `foo`.
- `"/"` → the member with the **empty-string** key `""`.
- `~1` decodes to `/` and `~0` decodes to `~` (so `"/a~1b"` is the key `a/b`,
  and `"/m~0n"` is the key `m~n`). Decode `~1` before `~0`.

## Done means

`src/index.js` exports `applyPatch`, and it correctly handles every operation,
the pointer-escaping rules, array insert/append/`-`, whole-document replace,
the error cases, immutability, and atomicity above.
