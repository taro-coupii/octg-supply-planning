import { beforeEach, describe, expect, it } from "vitest";
import { clearToken, getToken, getUser, setToken, setUser } from "./auth";

describe("auth token helpers", () => {
  beforeEach(() => localStorage.clear());

  it("returns null when no token is stored", () => {
    expect(getToken()).toBeNull();
  });

  it("round-trips a token through setToken/getToken", () => {
    setToken("abc.def");
    expect(getToken()).toBe("abc.def");
  });

  it("clearToken removes the stored token", () => {
    setToken("abc.def");
    clearToken();
    expect(getToken()).toBeNull();
  });

  it("returns null when no user is cached", () => {
    expect(getUser()).toBeNull();
  });

  it("round-trips a user through setUser/getUser", () => {
    setUser({ email: "planner@octg.dev", role: "PLANNER", business_unit_id: "bu-1" });
    expect(getUser()).toEqual({ email: "planner@octg.dev", role: "PLANNER", business_unit_id: "bu-1" });
  });

  it("clearToken also clears the cached user", () => {
    setToken("abc.def");
    setUser({ email: "planner@octg.dev", role: "PLANNER", business_unit_id: "bu-1" });
    clearToken();
    expect(getUser()).toBeNull();
  });

  it("getUser returns null for corrupted JSON", () => {
    localStorage.setItem("octg_user", "not-json");
    expect(getUser()).toBeNull();
  });
});
