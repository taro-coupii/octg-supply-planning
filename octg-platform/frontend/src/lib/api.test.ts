import { afterEach, describe, expect, it, vi } from "vitest";
import { apiGet } from "./api";

describe("apiGet", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("returns parsed JSON on ok responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => [{ id: "1" }] }));
    await expect(apiGet("/products")).resolves.toEqual([{ id: "1" }]);
  });

  it("throws with status on non-ok responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 500 }));
    await expect(apiGet("/products")).rejects.toThrow("GET /products failed: 500");
  });
});
