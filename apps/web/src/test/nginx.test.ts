// @vitest-environment node
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { CONTENT_SECURITY_POLICY } from "../../vite.config";

// The Compose image (Dockerfile) serves the build with nginx.conf.  Its
// Content-Security-Policy header must be the app's own policy, plus
// frame-ancestors, which a <meta> policy cannot carry.
const conf = readFileSync(new URL("../../nginx.conf", import.meta.url), "utf8");

function header(name: string): string | undefined {
  const match = conf.match(new RegExp(`add_header ${name} "([^"]*)" always;`));
  return match?.[1];
}

describe("nginx.conf", () => {
  it("sends the app's Content-Security-Policy with frame-ancestors 'none'", () => {
    expect(header("Content-Security-Policy")).toBe(`${CONTENT_SECURITY_POLICY}; frame-ancestors 'none'`);
  });

  it("sends the other protective headers on every response", () => {
    expect(header("X-Content-Type-Options")).toBe("nosniff");
    expect(header("Referrer-Policy")).toBe("no-referrer");
    expect(header("X-Frame-Options")).toBe("DENY");
  });

  it("sets headers once, at server level, so no location drops them", () => {
    // add_header in a location replaces the server's add_header set.
    const locations = conf.split(/\n\s*location /).slice(1);
    for (const block of locations) expect(block).not.toMatch(/add_header/);
  });

  it("forwards /api/ to the API service and falls back to index.html", () => {
    expect(conf).toMatch(/location \/api\/ \{[^}]*set \$api_upstream http:\/\/api:8000;[^}]*proxy_pass \$api_upstream;/s);
    expect(conf).toMatch(/location \/ \{\s*try_files \$uri \/index\.html;/);
  });
});
