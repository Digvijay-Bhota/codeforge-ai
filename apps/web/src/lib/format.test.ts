import { describe, expect, it } from "vitest";
import { formatBytes, formatRelative, formatTimeLeft, safePullRequestUrl, taskTitle } from "./format";

describe("format", () => {
  const now = Date.parse("2026-10-21T12:00:00Z");

  it("describes time left until expiry", () => {
    expect(formatTimeLeft("2026-10-21T14:05:00Z", now)).toBe("2 h 5 min");
    expect(formatTimeLeft("2026-10-23T13:00:00Z", now)).toBe("2 d 1 h");
    expect(formatTimeLeft("2026-10-21T12:00:30Z", now)).toBe("less than a minute");
    expect(formatTimeLeft("2026-10-21T11:00:00Z", now)).toBeNull();
    expect(formatTimeLeft(null, now)).toBeNull();
  });

  it("formats relative times and tolerates missing values", () => {
    expect(formatRelative("2026-10-21T11:57:00Z", now)).toMatch(/3 minutes ago/);
    expect(formatRelative(null, now)).toBe("—");
    expect(formatRelative("not a date", now)).toBe("—");
  });

  it("uses the first line of a request as its title", () => {
    expect(taskTitle("Fix it\nwith details")).toBe("Fix it");
    expect(taskTitle("   ")).toBe("Untitled task");
    expect(taskTitle("x".repeat(200))).toHaveLength(138);
  });

  it("formats sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
  });

  it.each([
    ["https://github.com/acme/w/pull/1", "https://github.com/acme/w/pull/1"],
    ["http://github.com/acme/w/pull/1", null],
    ["https://github.com.evil.example/pull/1", null],
    ["javascript:alert(1)", null],
    ["not a url", null],
    [null, null],
  ])("safePullRequestUrl(%s)", (input, expected) => {
    expect(safePullRequestUrl(input)).toBe(expected);
  });
});
