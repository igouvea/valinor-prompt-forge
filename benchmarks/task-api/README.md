# Benchmark: task-api (in-memory REST-style task service)

Implement the request-handling logic of a small task API. To keep it
deterministic, it is a **pure function** over an in-memory store — no real HTTP
server, no ports.

## Module contract (exact — the hidden test suite imports this)

Create `src/index.js` as an **ES module** that exports:

```js
export function createStore() { /* returns a fresh, opaque store */ }
export function handleRequest(req, store) { /* returns { status, body } */ }
```

- `req` is `{ method, path, body }` (`body` may be `undefined`).
- The return is `{ status, body }`. For `204` responses, `body` must be `null`
  or `undefined`.

## Resource: tasks

A task is `{ id, title, done }`. `id` is a **number** assigned by the server,
starting at `1` and **strictly increasing** (never reused, even after deletes).

### Routes & status codes

- `POST /tasks` — body `{ title, done? }`.
  - `title` is **required**, a **non-empty string**. `done` is optional and
    defaults to `false`; if present it must be a **boolean**.
  - Success → `201` with the created task.
- `GET /tasks` — `200` with the array of tasks in insertion order.
- `GET /tasks/:id` — `200` with the task, or `404` if no task has that id.
- `PUT /tasks/:id` — body may contain `title` and/or `done`.
  - Updates only the provided fields; the others are preserved.
  - `200` with the updated task; `404` if the id is unknown.
- `DELETE /tasks/:id` — `204` (no body); `404` if the id is unknown.

### Errors

- Invalid body (missing/empty/non-string `title` on create; wrong types on
  create or update) → `400`.
- A path that is not `/tasks` or `/tasks/:id` → `404`.
- A recognized path with an unsupported method → `405`.

## Done means

`src/index.js` exports `createStore` and `handleRequest`, and it implements
every route, the validation rules, the `404`-vs-`405` distinction, the `done`
default, partial updates, monotonic non-reused ids, and the `204`-with-no-body
behavior above.
