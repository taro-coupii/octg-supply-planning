import { describe, expect, it } from "vitest";
import { ARM_TIMEOUT_MS, press } from "./confirm";

describe("two-step confirm state machine", () => {
  it("first press arms without firing", () => {
    expect(press("idle")).toEqual({ state: "armed", fire: false });
  });

  it("second press fires and returns to idle", () => {
    expect(press("armed")).toEqual({ state: "idle", fire: true });
  });

  it("arm timeout is exactly 4 seconds (spec §3 cross-cutting rule)", () => {
    expect(ARM_TIMEOUT_MS).toBe(4000);
  });
});
