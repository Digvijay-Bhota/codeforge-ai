import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mockApi, renderApp, signedIn, USER } from "../test/helpers";
import { loadSession, rememberLoginState } from "./session";

vi.mock("../lib/navigation", () => ({ navigateAway: vi.fn() }));
const { navigateAway } = await import("../lib/navigation");

const TOKEN = { access_token: "jwt-new", token_type: "bearer", expires_in: 3600, user: USER };

beforeEach(() => vi.mocked(navigateAway).mockReset());

describe("sign-in", () => {
  it("sends signed-out visitors to the sign-in page", async () => {
    mockApi({});
    renderApp("/tasks/abc");
    expect(await screen.findByRole("button", { name: /sign in with github/i })).toBeInTheDocument();
  });

  it("starts GitHub login with this app's callback and remembers the state", async () => {
    const api = mockApi({
      "GET /auth/github/login": {
        body: { authorization_url: "https://github.com/login/oauth/authorize?state=s1", state: "s1" },
      },
    });
    renderApp("/login");
    await userEvent.click(screen.getByRole("button", { name: /sign in with github/i }));
    await waitFor(() => expect(navigateAway).toHaveBeenCalledWith("https://github.com/login/oauth/authorize?state=s1"));
    expect(api.calls[0]?.query.get("redirect_uri")).toBe(`${window.location.origin}/auth/callback`);
    expect(window.sessionStorage.getItem("codeforge.login.state")).toBe("s1");
  });

  it("refuses a non-https sign-in address", async () => {
    mockApi({ "GET /auth/github/login": { body: { authorization_url: "javascript:alert(1)", state: "s1" } } });
    renderApp("/login");
    await userEvent.click(screen.getByRole("button", { name: /sign in with github/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("unexpected sign-in address");
    expect(navigateAway).not.toHaveBeenCalled();
  });
});

describe("callback", () => {
  it("completes a login this tab started and returns to the requested page", async () => {
    rememberLoginState("s1", "/tasks/abc");
    const api = mockApi({
      "GET /auth/github/callback": { body: TOKEN },
      "GET /auth/me": { body: USER },
      "GET /tasks/abc": { status: 404, body: { detail: "Task not found" } },
      "GET /tasks/abc/publication": { status: 404 },
      "GET /tasks/abc/events": { body: [] },
    });
    renderApp("/auth/callback?code=c1&state=s1");
    expect(await screen.findByText("Task not found")).toBeInTheDocument();
    const [exchange] = api.callsTo("GET", "/auth/github/callback");
    expect(Object.fromEntries(exchange!.query)).toEqual({ code: "c1", state: "s1" });
    expect(api.callsTo("GET", "/auth/github/callback")).toHaveLength(1); // the code is used once
    expect(loadSession()?.token).toBe("jwt-new");
  });

  it("refuses a callback this tab did not start (login CSRF)", async () => {
    const api = mockApi({ "GET /auth/github/callback": { body: TOKEN } });
    renderApp("/auth/callback?code=attacker-code&state=attacker-state");
    expect(await screen.findByRole("alert")).toHaveTextContent("not started from this browser tab");
    expect(api.calls).toHaveLength(0);
    expect(loadSession()).toBeNull();
  });

  it("explains a cancelled GitHub authorization", async () => {
    rememberLoginState("s1", "/");
    mockApi({});
    renderApp("/auth/callback?error=access_denied&state=s1");
    expect(await screen.findByRole("alert")).toHaveTextContent("cancelled on GitHub");
  });

  it("shows the API's reason when the exchange fails", async () => {
    rememberLoginState("s1", "/");
    mockApi({ "GET /auth/github/callback": { status: 400, body: { detail: "Invalid or expired OAuth state" } } });
    renderApp("/auth/callback?code=c1&state=s1");
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid or expired OAuth state");
    expect(loadSession()).toBeNull();
  });
});

describe("session", () => {
  it("sends the stored session's token on the very first requests after a reload", async () => {
    // Regression: the client used to learn the token in an effect that ran
    // after the page's own first requests, which then failed with 401.
    signedIn(USER, "jwt-stored");
    const authorized = (call: { headers: Record<string, string> }) =>
      call.headers.Authorization === "Bearer jwt-stored"
        ? { body: { total: 0, limit: 20, offset: 0, tasks: [] } }
        : { status: 401, body: { detail: "Missing or invalid Authorization header" } };
    const api = mockApi({ "GET /auth/me": (call) => (authorized(call).status ? authorized(call) : { body: USER }), "GET /tasks": authorized });
    renderApp("/");
    expect(await screen.findByText("No tasks yet")).toBeInTheDocument();
    expect(api.calls.every((call) => call.headers.Authorization === "Bearer jwt-stored")).toBe(true);
    expect(loadSession()?.token).toBe("jwt-stored");
  });


  it("signs out on request and on an expired token", async () => {
    signedIn();
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks": { status: 401, body: { detail: "Token expired" } },
    });
    renderApp("/");
    expect(await screen.findByText(/your session ended/i)).toBeInTheDocument();
    expect(loadSession()).toBeNull();
  });

  it("sign out clears the session", async () => {
    signedIn();
    mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": { body: { total: 0, limit: 20, offset: 0, tasks: [] } } });
    renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("button", { name: /sign in with github/i })).toBeInTheDocument();
    expect(loadSession()).toBeNull();
  });
});
