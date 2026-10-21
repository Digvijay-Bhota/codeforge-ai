import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi } from "../test/helpers";
import { ApiError, apiRequest, configureApiClient } from "./client";

afterEach(() => configureApiClient({ getToken: () => null, onUnauthorized: () => undefined }));

describe("apiRequest", () => {
  it("sends the session token and drops empty query values", async () => {
    configureApiClient({ getToken: () => "jwt-1", onUnauthorized: vi.fn() });
    const { calls } = mockApi({ "GET /tasks": { body: { total: 0 } } });
    await apiRequest("/tasks", { query: { status: "", repository: "acme/w", limit: 20, offset: null } });
    const [call] = calls;
    expect(call?.headers.Authorization).toBe("Bearer jwt-1");
    expect(Object.fromEntries(call!.query)).toEqual({ repository: "acme/w", limit: "20" });
  });

  it("never sends the token to the login endpoints", async () => {
    configureApiClient({ getToken: () => "jwt-1", onUnauthorized: vi.fn() });
    const { calls } = mockApi({ "GET /auth/github/login": { body: {} } });
    await apiRequest("/auth/github/login", { authenticated: false });
    expect(calls[0]?.headers.Authorization).toBeUndefined();
  });

  it("surfaces the API's detail message", async () => {
    mockApi({ "POST /x": { status: 409, body: { detail: "Publication approval expired" } } });
    await expect(apiRequest("/x", { method: "POST", body: {} })).rejects.toMatchObject({
      status: 409,
      message: "Publication approval expired",
    });
  });

  it("explains validation errors and bare failures", async () => {
    mockApi({
      "POST /v": { status: 422, body: { detail: [{ msg: "String should match pattern" }] } },
      "GET /down": { status: 503 },
    });
    await expect(apiRequest("/v", { method: "POST", body: {} })).rejects.toThrow("String should match pattern");
    await expect(apiRequest("/down")).rejects.toThrow("The CodeForge API had a problem");
  });

  it("ends the session on 401", async () => {
    const onUnauthorized = vi.fn();
    configureApiClient({ getToken: () => "jwt-1", onUnauthorized });
    mockApi({ "GET /auth/me": { status: 401, body: { detail: "Token expired" } } });
    await expect(apiRequest("/auth/me")).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("reports an unreachable API plainly", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(apiRequest("/tasks")).rejects.toMatchObject({ status: 0, message: expect.stringContaining("Can't reach") });
  });
});
