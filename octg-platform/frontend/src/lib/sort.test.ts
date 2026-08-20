import { describe, expect, it } from "vitest";
import { nextSortState, sortRows } from "./sort";

describe("nextSortState", () => {
  it("cycles null -> asc -> desc -> null (third click returns to server order)", () => {
    expect(nextSortState(null)).toBe("asc");
    expect(nextSortState("asc")).toBe("desc");
    expect(nextSortState("desc")).toBeNull();
  });
});

describe("sortRows", () => {
  const rows = [
    { name: "b", qty: 2 },
    { name: "a", qty: null },
    { name: "c", qty: 1 },
  ];

  it("returns server order untouched when dir is null", () => {
    expect(sortRows(rows, "qty", null).map((r) => r.name)).toEqual(["b", "a", "c"]);
  });

  it("sorts asc with nulls last", () => {
    expect(sortRows(rows, "qty", "asc").map((r) => r.name)).toEqual(["c", "b", "a"]);
  });

  it("sorts desc with nulls STILL last", () => {
    expect(sortRows(rows, "qty", "desc").map((r) => r.name)).toEqual(["b", "c", "a"]);
  });

  it("does not mutate the input array", () => {
    sortRows(rows, "qty", "asc");
    expect(rows.map((r) => r.name)).toEqual(["b", "a", "c"]);
  });
});
