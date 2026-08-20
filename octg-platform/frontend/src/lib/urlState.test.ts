import { describe, expect, it } from "vitest";
import { readListParam, writeListParam, writeParam } from "./urlState";

describe("writeParam", () => {
  it("sets a value without mutating the input", () => {
    const before = new URLSearchParams("a=1");
    const after = writeParam(before, "b", "2");
    expect(after.get("b")).toBe("2");
    expect(before.get("b")).toBeNull();
  });

  it("deletes the key when value is null or empty", () => {
    const params = new URLSearchParams("a=1");
    expect(writeParam(params, "a", null).has("a")).toBe(false);
    expect(writeParam(params, "a", "").has("a")).toBe(false);
  });
});

describe("list params", () => {
  it("round-trips multiple values", () => {
    const params = writeListParam(new URLSearchParams(), "status", ["Planned", "Confirmed"]);
    expect(readListParam(params, "status")).toEqual(["Planned", "Confirmed"]);
  });

  it("writing an empty list clears the key", () => {
    const params = writeListParam(new URLSearchParams("status=Planned"), "status", []);
    expect(params.has("status")).toBe(false);
  });
});
