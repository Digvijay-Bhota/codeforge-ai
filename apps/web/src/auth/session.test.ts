import { describe, expect, it } from "vitest";
import { USER } from "../test/helpers";
import { loadSession, rememberLoginState, safeReturnTo, saveSession, sessionFromToken, takeLoginState } from "./session";

describe("session storage", () => {
  it("round-trips a session until it expires", () => {
    const session = sessionFromToken({ access_token: "t", token_type: "bearer", expires_in: 60, user: USER }, 1_000);
    expect(session.expiresAt).toBe(61_000);
    saveSession(session);
    expect(loadSession(2_000)).toEqual(session);
    expect(loadSession(61_000)).toBeNull();
    // An expired entry is removed, not kept around.
    expect(window.sessionStorage.getItem("codeforge.session")).toBeNull();
  });

  it("discards malformed entries", () => {
    window.sessionStorage.setItem("codeforge.session", "{not json");
    expect(loadSession()).toBeNull();
    window.sessionStorage.setItem("codeforge.session", JSON.stringify({ token: "", expiresAt: Date.now() + 1000, user: USER }));
    expect(loadSession()).toBeNull();
  });

  it("hands out the login state once", () => {
    rememberLoginState("state-1", "/tasks/abc");
    expect(takeLoginState()).toEqual({ state: "state-1", returnTo: "/tasks/abc" });
    expect(takeLoginState()).toEqual({ state: null, returnTo: "/" });
  });
});

describe("safeReturnTo", () => {
  it.each([
    ["/tasks/1?x=1", "/tasks/1?x=1"],
    ["/", "/"],
    ["//evil.example/path", "/"],
    ["/\\evil.example", "/"],
    ["https://evil.example", "/"],
    ["javascript:alert(1)", "/"],
    ["/login", "/"],
    ["/auth/callback?code=x", "/"],
    [null, "/"],
  ])("%s -> %s", (input, expected) => {
    expect(safeReturnTo(input)).toBe(expected);
  });
});
