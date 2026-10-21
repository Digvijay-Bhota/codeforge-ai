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

  it("logs requests without query strings or referrers", () => {
    // The OAuth callback carries the authorization code and state in its query
    // string; the default format's "$request" would log both.
    const format = conf.match(/log_format codeforge_noquery ((?:'[^']*'\s*)+);/)?.[1];
    expect(format).toBeDefined();
    expect(format).toContain("$codeforge_request_path");
    for (const variable of ["$request ", '$request"', "$request_uri", "$args", "$query_string", "$http_referer"]) {
      expect(format).not.toContain(variable);
    }
    // The logged path is $request_uri cut at "?" (the query string itself is never captured).
    expect(conf).toContain('map $request_uri $codeforge_request_path {\n    "~^(?<path>[^?]*)(?<query_mark>\\??)" $path$query_mark;\n}');
    // Used by the server, and no location switches to another format.
    expect(conf).toMatch(/\n {4}access_log \/var\/log\/nginx\/access\.log codeforge_noquery;/);
    expect(conf.match(/^\s*access_log /gm)).toHaveLength(2); // the server's, and "off" for /healthz
  });

  it("forwards /api/ to the API service and falls back to index.html", () => {
    expect(conf).toMatch(/location \/api\/ \{[^}]*set \$api_upstream http:\/\/api:8000;[^}]*proxy_pass \$api_upstream;/s);
    expect(conf).toMatch(/location \/ \{\s*try_files \$uri \/index\.html;/);
  });
});
