// HELD-OUT golden suite for json-patch. Authored by the benchmark designer;
// the agents never see this. Copied into the benchmark dir at scoring time only.
import { describe, it, expect } from "vitest";
import { applyPatch } from "../src/index.js";

const clone = (x) => JSON.parse(JSON.stringify(x));

describe("json-patch — objects", () => {
  it("adds a member", () => {
    expect(applyPatch({ a: 1 }, [{ op: "add", path: "/b", value: 2 }])).toEqual({ a: 1, b: 2 });
  });
  it("add replaces an existing member", () => {
    expect(applyPatch({ a: 1 }, [{ op: "add", path: "/a", value: 9 }])).toEqual({ a: 9 });
  });
  it("replaces an existing member", () => {
    expect(applyPatch({ a: 1 }, [{ op: "replace", path: "/a", value: 5 }])).toEqual({ a: 5 });
  });
  it("removes a member", () => {
    expect(applyPatch({ a: 1, b: 2 }, [{ op: "remove", path: "/b" }])).toEqual({ a: 1 });
  });
  it("adds nested", () => {
    expect(applyPatch({ a: { b: {} } }, [{ op: "add", path: "/a/b/c", value: 1 }])).toEqual({ a: { b: { c: 1 } } });
  });
});

describe("json-patch — arrays", () => {
  it("inserts and shifts", () => {
    expect(applyPatch({ a: [1, 2, 3] }, [{ op: "add", path: "/a/1", value: 9 }])).toEqual({ a: [1, 9, 2, 3] });
  });
  it("appends when index equals length", () => {
    expect(applyPatch({ a: [1, 2] }, [{ op: "add", path: "/a/2", value: 3 }])).toEqual({ a: [1, 2, 3] });
  });
  it('appends with "-"', () => {
    expect(applyPatch({ a: [1, 2] }, [{ op: "add", path: "/a/-", value: 3 }])).toEqual({ a: [1, 2, 3] });
  });
  it("removes and shifts", () => {
    expect(applyPatch({ a: [1, 2, 3] }, [{ op: "remove", path: "/a/1" }])).toEqual({ a: [1, 3] });
  });
});

describe("json-patch — pointer escaping", () => {
  it("decodes ~1 to a slash key", () => {
    expect(applyPatch({}, [{ op: "add", path: "/a~1b", value: 1 }])).toEqual({ "a/b": 1 });
  });
  it("decodes ~0 to a tilde key", () => {
    expect(applyPatch({}, [{ op: "add", path: "/m~0n", value: 1 }])).toEqual({ "m~n": 1 });
  });
  it('treats "/" as the empty-string key', () => {
    expect(applyPatch({}, [{ op: "add", path: "/", value: 1 }])).toEqual({ "": 1 });
  });
  it('replaces the whole document with path ""', () => {
    expect(applyPatch({ a: 1 }, [{ op: "replace", path: "", value: { x: 2 } }])).toEqual({ x: 2 });
  });
});

describe("json-patch — move / copy / test", () => {
  it("moves", () => {
    expect(applyPatch({ a: 1, b: {} }, [{ op: "move", from: "/a", path: "/b/c" }])).toEqual({ b: { c: 1 } });
  });
  it("copies", () => {
    expect(applyPatch({ a: 1, b: {} }, [{ op: "copy", from: "/a", path: "/b/c" }])).toEqual({ a: 1, b: { c: 1 } });
  });
  it("test passes and leaves the document unchanged", () => {
    expect(applyPatch({ a: 1 }, [{ op: "test", path: "/a", value: 1 }])).toEqual({ a: 1 });
  });
  it("test deep-equals arrays", () => {
    expect(applyPatch({ a: [1, 2] }, [{ op: "test", path: "/a", value: [1, 2] }])).toEqual({ a: [1, 2] });
  });
});

describe("json-patch — errors", () => {
  it("throws when test fails", () => {
    expect(() => applyPatch({ a: 1 }, [{ op: "test", path: "/a", value: 2 }])).toThrow();
  });
  it("throws when removing a nonexistent member", () => {
    expect(() => applyPatch({ a: 1 }, [{ op: "remove", path: "/b" }])).toThrow();
  });
  it("throws when replacing a nonexistent member", () => {
    expect(() => applyPatch({ a: 1 }, [{ op: "replace", path: "/b", value: 1 }])).toThrow();
  });
  it("throws on an out-of-range array index", () => {
    expect(() => applyPatch({ a: [1] }, [{ op: "add", path: "/a/5", value: 9 }])).toThrow();
  });
  it("throws on an unknown op", () => {
    expect(() => applyPatch({}, [{ op: "frobnicate", path: "/a", value: 1 }])).toThrow();
  });
});

describe("json-patch — immutability & atomicity", () => {
  it("does not mutate the input document", () => {
    const doc = { a: 1, arr: [1, 2] };
    const snap = clone(doc);
    applyPatch(doc, [{ op: "add", path: "/b", value: 2 }, { op: "add", path: "/arr/-", value: 3 }]);
    expect(doc).toEqual(snap);
  });
  it("aborts the whole patch when a later op fails (atomic)", () => {
    const doc = { a: 1 };
    expect(() => applyPatch(doc, [{ op: "add", path: "/b", value: 2 }, { op: "test", path: "/a", value: 999 }])).toThrow();
    expect(doc).toEqual({ a: 1 });
  });
});

describe("json-patch — hard edge cases (RFC corners)", () => {
  it("rejects move into one's own child (from is a proper prefix of path)", () => {
    expect(() => applyPatch({ a: { b: 1 } }, [{ op: "move", from: "/a", path: "/a/b/c" }])).toThrow();
  });
  it("rejects add to a nonexistent parent", () => {
    expect(() => applyPatch({}, [{ op: "add", path: "/a/b", value: 1 }])).toThrow();
  });
  it('rejects "-" outside add (remove)', () => {
    expect(() => applyPatch({ a: [1, 2] }, [{ op: "remove", path: "/a/-" }])).toThrow();
  });
  it("rejects a leading-zero array index", () => {
    expect(() => applyPatch({ a: [1, 2, 3] }, [{ op: "replace", path: "/a/01", value: 9 }])).toThrow();
  });
  it("copy is an independent deep clone (mutating the source later is isolated)", () => {
    const out = applyPatch({ a: { x: 1 }, b: {} }, [
      { op: "copy", from: "/a", path: "/b/c" },
      { op: "remove", path: "/a/x" },
    ]);
    expect(out).toEqual({ a: {}, b: { c: { x: 1 } } });
  });
  it("rejects move of a nonexistent from", () => {
    expect(() => applyPatch({ a: 1 }, [{ op: "move", from: "/zzz", path: "/b" }])).toThrow();
  });
  it("rejects replace at an out-of-range array index", () => {
    expect(() => applyPatch({ a: [1] }, [{ op: "replace", path: "/a/3", value: 9 }])).toThrow();
  });
  it("rejects test against a missing path", () => {
    expect(() => applyPatch({ a: 1 }, [{ op: "test", path: "/missing", value: 1 }])).toThrow();
  });
  it("test treats null as a value (null equals null)", () => {
    expect(applyPatch({ a: null }, [{ op: "test", path: "/a", value: null }])).toEqual({ a: null });
  });
  it("rejects remove at an out-of-range array index", () => {
    expect(() => applyPatch({ a: [1, 2] }, [{ op: "remove", path: "/a/5" }])).toThrow();
  });
  it("rejects add at an array index greater than length", () => {
    expect(() => applyPatch({ a: [1, 2] }, [{ op: "add", path: "/a/3", value: 9 }])).toThrow();
  });
  it("move within an array shifts indices correctly", () => {
    expect(applyPatch({ a: [1, 2, 3] }, [{ op: "move", from: "/a/0", path: "/a/2" }])).toEqual({ a: [2, 3, 1] });
  });
});
