# Benchmark: expr-eval (arithmetic expression evaluator)

Implement an evaluator for arithmetic expressions given as strings.

## Module contract (exact — the hidden test suite imports this)

Create `src/index.js` as an **ES module** that exports:

```js
export function evaluate(expression) { /* returns a Number */ }
```

## Grammar & semantics

- **Operators:** `+`, `-`, `*`, `/`, `%` (binary), and unary `+` / `-`.
- **Numbers:** non-negative integers and decimals (e.g. `42`, `1.5`, `0.25`).
- **Parentheses:** `(` … `)` for grouping, arbitrarily nested.
- **Whitespace** between tokens is insignificant and must be ignored.
- **Precedence:** `*`, `/`, `%` bind tighter than `+`, `-`. Unary `-`/`+` bind
  tighter than any binary operator. Parentheses override precedence.
- **Associativity:** binary operators are **left-associative**
  (`10 - 2 - 3` is `5`, `8 / 4 / 2` is `1`).
- Unary operators may stack (`--5` is `5`).
- The result is a JavaScript `Number`.

## Errors (throw an `Error`)

- Division by zero (`/`) or modulo by zero (`%`).
- Malformed input: empty/blank string, unbalanced parentheses, a trailing or
  leading binary operator, two binary operators in a row, or any character that
  is not a digit, `.`, an operator, parenthesis, or whitespace.

Note: `5 / 0` must **throw**, not return `Infinity`.

## Done means

`src/index.js` exports `evaluate`, and it returns the correct number for valid
expressions (respecting precedence, associativity, unary operators, decimals,
and parentheses) and throws for every error case above.
