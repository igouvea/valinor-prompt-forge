// HELD-OUT golden suite for expr-eval. Authored by the benchmark designer;
// the agents never see this. Copied into the benchmark dir at scoring time only.
import { describe, it, expect } from "vitest";
import { evaluate } from "../src/index.js";

describe("expr-eval — arithmetic", () => {
  it("adds", () => expect(evaluate("2+3")).toBe(5));
  it("applies * before +", () => expect(evaluate("2+3*4")).toBe(14));
  it("honors parentheses", () => expect(evaluate("(2+3)*4")).toBe(20));
  it("is left-associative for -", () => expect(evaluate("10-2-3")).toBe(5));
  it("is left-associative for /", () => expect(evaluate("8/4/2")).toBe(1));
  it("computes modulo", () => expect(evaluate("7%3")).toBe(1));
  it("orders % with +", () => expect(evaluate("10+7%3")).toBe(11));
  it("handles decimals", () => expect(evaluate("1.5*2")).toBe(3));
  it("returns a decimal result", () => expect(evaluate("7/2")).toBe(3.5));
  it("evaluates nested parentheses", () => expect(evaluate("((1+2)*(3+4))")).toBe(21));
  it("ignores whitespace", () => expect(evaluate("  2  +  3 * 4 ")).toBe(14));
  it("evaluates a long chain", () => expect(evaluate("1+2+3+4+5")).toBe(15));
  it("evaluates a mixed expression", () => expect(evaluate("2*(3+4)-5/5")).toBe(13));
});

describe("expr-eval — unary operators", () => {
  it("leading unary minus", () => expect(evaluate("-3+4")).toBe(1));
  it("unary minus binds tight", () => expect(evaluate("2*-3")).toBe(-6));
  it("unary plus", () => expect(evaluate("+5")).toBe(5));
  it("stacked unary minus", () => expect(evaluate("--5")).toBe(5));
  it("unary inside parens", () => expect(evaluate("3*(-2+5)")).toBe(9));
});

describe("expr-eval — errors", () => {
  it("throws on division by zero", () => expect(() => evaluate("5/0")).toThrow());
  it("throws on modulo by zero", () => expect(() => evaluate("5%0")).toThrow());
  it("throws on an unbalanced opening paren", () => expect(() => evaluate("(2+3")).toThrow());
  it("throws on an extra closing paren", () => expect(() => evaluate("2+3)")).toThrow());
  it("throws on empty input", () => expect(() => evaluate("")).toThrow());
  it("throws on a blank string", () => expect(() => evaluate("   ")).toThrow());
  it("throws on a trailing operator", () => expect(() => evaluate("2+")).toThrow());
  it("throws on a leading binary operator", () => expect(() => evaluate("*2")).toThrow());
  it("throws on two binary operators in a row", () => expect(() => evaluate("2*/3")).toThrow());
  it("throws on an invalid character", () => expect(() => evaluate("2$3")).toThrow());
});
