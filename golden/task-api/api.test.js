// HELD-OUT golden suite for task-api. Authored by the benchmark designer;
// the agents never see this. Copied into the benchmark dir at scoring time only.
import { describe, it, expect, beforeEach } from "vitest";
import { createStore, handleRequest } from "../src/index.js";

let store;
beforeEach(() => { store = createStore(); });
const req = (method, path, body) => handleRequest({ method, path, body }, store);

describe("task-api — CRUD", () => {
  it("creates with 201 and defaults done to false", () => {
    const r = req("POST", "/tasks", { title: "A" });
    expect(r.status).toBe(201);
    expect(r.body).toMatchObject({ id: 1, title: "A", done: false });
  });
  it("honors done=true on create", () => {
    expect(req("POST", "/tasks", { title: "A", done: true }).body.done).toBe(true);
  });
  it("increments ids", () => {
    req("POST", "/tasks", { title: "A" });
    expect(req("POST", "/tasks", { title: "B" }).body.id).toBe(2);
  });
  it("lists tasks in insertion order", () => {
    req("POST", "/tasks", { title: "A" });
    req("POST", "/tasks", { title: "B" });
    const r = req("GET", "/tasks");
    expect(r.status).toBe(200);
    expect(r.body.map((t) => t.title)).toEqual(["A", "B"]);
  });
  it("gets a task by id", () => {
    req("POST", "/tasks", { title: "A" });
    const r = req("GET", "/tasks/1");
    expect(r.status).toBe(200);
    expect(r.body.title).toBe("A");
  });
  it("returns 404 for a missing task", () => {
    expect(req("GET", "/tasks/99").status).toBe(404);
  });
  it("updates fields with 200", () => {
    req("POST", "/tasks", { title: "A" });
    const r = req("PUT", "/tasks/1", { done: true });
    expect(r.status).toBe(200);
    expect(r.body).toMatchObject({ title: "A", done: true });
  });
  it("preserves other fields on partial update", () => {
    req("POST", "/tasks", { title: "A", done: true });
    expect(req("PUT", "/tasks/1", { title: "B" }).body).toMatchObject({ title: "B", done: true });
  });
  it("returns 404 when updating a missing task", () => {
    expect(req("PUT", "/tasks/99", { done: true }).status).toBe(404);
  });
  it("deletes with 204 and no body", () => {
    req("POST", "/tasks", { title: "A" });
    const r = req("DELETE", "/tasks/1");
    expect(r.status).toBe(204);
    expect(r.body == null).toBe(true);
  });
  it("returns 404 after deletion", () => {
    req("POST", "/tasks", { title: "A" });
    req("DELETE", "/tasks/1");
    expect(req("GET", "/tasks/1").status).toBe(404);
  });
  it("returns 404 when deleting a missing task", () => {
    expect(req("DELETE", "/tasks/99").status).toBe(404);
  });
});

describe("task-api — validation", () => {
  it("rejects create without a title (400)", () => {
    expect(req("POST", "/tasks", {}).status).toBe(400);
  });
  it("rejects an empty title (400)", () => {
    expect(req("POST", "/tasks", { title: "" }).status).toBe(400);
  });
  it("rejects a non-string title (400)", () => {
    expect(req("POST", "/tasks", { title: 123 }).status).toBe(400);
  });
  it("rejects a non-boolean done (400)", () => {
    expect(req("POST", "/tasks", { title: "A", done: "yes" }).status).toBe(400);
  });
  it("rejects an invalid type on update (400)", () => {
    req("POST", "/tasks", { title: "A" });
    expect(req("PUT", "/tasks/1", { done: "nope" }).status).toBe(400);
  });
});

describe("task-api — routing", () => {
  it("returns 404 for an unknown path", () => {
    expect(req("GET", "/widgets").status).toBe(404);
  });
  it("returns 405 for a bad method on the collection", () => {
    expect(req("PATCH", "/tasks").status).toBe(405);
  });
  it("returns 405 for a bad method on an item", () => {
    req("POST", "/tasks", { title: "A" });
    expect(req("PATCH", "/tasks/1").status).toBe(405);
  });
  it("does not reuse ids after deletion", () => {
    req("POST", "/tasks", { title: "A" });
    req("DELETE", "/tasks/1");
    expect(req("POST", "/tasks", { title: "B" }).body.id).toBe(2);
  });
});
