import { describe, expect, it } from "vitest";

import { pyRound } from "../src/pyround.js";
import { loadFixture, type RoundingFixtures } from "./fixtures.js";

describe("pyRound", () => {
  it("matches CPython on every fixture value", () => {
    const { cases } = loadFixture<RoundingFixtures>("pyround.json");
    expect(cases.length).toBeGreaterThan(2000);
    // Object.is, so 0 and -0 count as different, as they are in Python.
    const mismatches = cases.filter(([x, n, expected]) => !Object.is(pyRound(x, n), expected));
    expect(mismatches).toEqual([]);
  });

  it("rounds the binary value, not the decimal literal", () => {
    // 2.675 is stored as 2.67499999...; Math.round(x * 100) / 100 gives 2.68.
    expect(pyRound(2.675, 2)).toBe(2.67);
  });

  it("sends exact ties to even", () => {
    expect(pyRound(0.5, 0)).toBe(0);
    expect(pyRound(1.5, 0)).toBe(2);
    expect(pyRound(2.5, 0)).toBe(2);
    expect(pyRound(0.125, 2)).toBe(0.12);
  });

  it("keeps the sign of a result that rounds to zero", () => {
    expect(Object.is(pyRound(-0.00001, 4), -0)).toBe(true);
    expect(Object.is(pyRound(-0, 4), -0)).toBe(true);
  });

  it("passes non-finite values through", () => {
    expect(pyRound(Number.NaN, 2)).toBeNaN();
    expect(pyRound(Number.POSITIVE_INFINITY, 2)).toBe(Number.POSITIVE_INFINITY);
  });

  it("rejects ndigits Python's float rounding would treat differently", () => {
    expect(() => pyRound(1.5, -1)).toThrow(RangeError);
    expect(() => pyRound(1.5, 0.5)).toThrow(RangeError);
  });
});
