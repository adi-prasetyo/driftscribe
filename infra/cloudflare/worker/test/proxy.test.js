// Unit tests for the demo-mode proxy. Node 24's undici implements the same
// Request/Headers semantics the Workers runtime uses for the patterns in
// proxy.js (`new Request(url, request)` + mutable headers on a constructed
// Request), so the handler runs as-is under vitest with a stubbed fetch.
import { afterEach, describe, expect, it, vi } from "vitest";

import worker, { demoAllowed } from "../src/proxy.js";

const ORIGIN = "driftscribe-agent-u272wv52kq-an.a.run.app";
const PUBLIC = "https://driftscribe.adp-app.com";

/** Stub global fetch; returns a recorder exposing the forwarded request
 *  as {url, method, headers} regardless of which fetch(...) arg shape the
 *  handler used (passthrough: (url, Request) — demo: (Request)). */
function stubFetch() {
  const seen = {};
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input, init) => {
      if (typeof input === "string") {
        seen.url = input;
        seen.method = init.method;
        seen.headers = init.headers;
        seen.body = init.body;
      } else {
        seen.url = input.url;
        seen.method = input.method;
        seen.headers = input.headers;
        seen.body = input.body;
      }
      return new Response("ok");
    }),
  );
  return seen;
}

afterEach(() => vi.unstubAllGlobals());

const req = (path, opts = {}) => new Request(`${PUBLIC}${path}`, opts);

describe("demoAllowed", () => {
  it.each([
    ["GET", "/decisions", true],
    ["GET", "/infra/graph", true],
    ["GET", "/infra/graph/preview", true],
    ["GET", "/capabilities", true],
    ["GET", "/pause", true],
    ["GET", "/autonomy", true],
    ["GET", "/trace/abc-123", true],
    ["POST", "/chat", true],
    // excluded by design: operator mutations + cost amplification
    ["POST", "/pause", false],
    ["POST", "/autonomy", false],
    ["POST", "/recheck", false],
    ["POST", "/iac-approvals/42", false],
    // method matters
    ["GET", "/chat", false],
    // path shape matters
    ["GET", "/trace/a/b", false],
    ["GET", "/trace/", false],
    ["GET", "/decisions/extra", false],
    ["GET", "/", false],
  ])("%s %s -> %s", (method, path, expected) => {
    expect(demoAllowed(method, path)).toBe(expected);
  });
});

describe("passthrough (DEMO_MODE off)", () => {
  it.each([{ DEMO_MODE: "0" }, {}, undefined])(
    "forwards untouched with env=%o",
    async (env) => {
      const seen = stubFetch();
      await worker.fetch(
        req("/decisions?limit=50", {
          headers: { "X-DriftScribe-Token": "operator-pasted" },
        }),
        env,
      );
      expect(seen.url).toBe(`https://${ORIGIN}/decisions?limit=50`);
      // today's behavior: ALL headers flow through, token included
      expect(seen.headers.get("X-DriftScribe-Token")).toBe("operator-pasted");
    },
  );
});

describe("demo mode", () => {
  const env = { DEMO_MODE: "1", DEMO_TOKEN: "the-real-token" };

  it("injects the secret token + marker on an allowlisted GET", async () => {
    const seen = stubFetch();
    await worker.fetch(req("/decisions?limit=50"), env);
    expect(seen.url).toBe(`https://${ORIGIN}/decisions?limit=50`);
    expect(seen.headers.get("X-DriftScribe-Token")).toBe("the-real-token");
    expect(seen.headers.get("X-DriftScribe-Demo-Anonymous")).toBe("1");
  });

  it("replaces a browser-supplied (forged) token, never forwards it", async () => {
    const seen = stubFetch();
    await worker.fetch(
      req("/capabilities", { headers: { "X-DriftScribe-Token": "forged" } }),
      env,
    );
    expect(seen.headers.get("X-DriftScribe-Token")).toBe("the-real-token");
  });

  it("strips the token on non-allowlisted routes without injecting", async () => {
    const seen = stubFetch();
    await worker.fetch(
      req("/pause", {
        method: "POST",
        headers: { "X-DriftScribe-Token": "forged" },
      }),
      env,
    );
    expect(seen.headers.get("X-DriftScribe-Token")).toBeNull();
    expect(seen.headers.get("X-DriftScribe-Demo-Anonymous")).toBeNull();
  });

  it("strips a spoofed demo-anonymous marker", async () => {
    const seen = stubFetch();
    await worker.fetch(
      req("/autonomy", {
        method: "POST",
        body: "level=full",
        duplex: "half",
        headers: { "X-DriftScribe-Demo-Anonymous": "1" },
      }),
      env,
    );
    expect(seen.headers.get("X-DriftScribe-Demo-Anonymous")).toBeNull();
  });

  it("does not inject when a CF Access JWT is present, and leaves the JWT untouched", async () => {
    const seen = stubFetch();
    await worker.fetch(
      req("/decisions", {
        headers: {
          "Cf-Access-Jwt-Assertion": "real.jwt.value",
          "X-DriftScribe-Token": "stale-browser-token",
        },
      }),
      env,
    );
    expect(seen.headers.get("Cf-Access-Jwt-Assertion")).toBe("real.jwt.value");
    expect(seen.headers.get("X-DriftScribe-Token")).toBeNull();
    expect(seen.headers.get("X-DriftScribe-Demo-Anonymous")).toBeNull();
  });

  it("fails safe when DEMO_TOKEN is unset: sanitized, nothing injected", async () => {
    const seen = stubFetch();
    await worker.fetch(
      req("/decisions", { headers: { "X-DriftScribe-Token": "forged" } }),
      { DEMO_MODE: "1" },
    );
    expect(seen.headers.get("X-DriftScribe-Token")).toBeNull();
    expect(seen.headers.get("X-DriftScribe-Demo-Anonymous")).toBeNull();
  });

  it("injects on POST /chat and preserves the body", async () => {
    const seen = stubFetch();
    const body = JSON.stringify({ prompt: "hi", workload: "drift" });
    await worker.fetch(
      req("/chat", {
        method: "POST",
        body,
        duplex: "half",
        headers: { "Content-Type": "application/json" },
      }),
      env,
    );
    expect(seen.url).toBe(`https://${ORIGIN}/chat`);
    expect(seen.headers.get("X-DriftScribe-Token")).toBe("the-real-token");
    const forwarded = await new Response(seen.body).text();
    expect(forwarded).toBe(body);
  });

  it("matches the allowlist on the normalized path it forwards (no ../ smuggling)", async () => {
    const seen = stubFetch();
    await worker.fetch(req("/chat/../pause", { method: "POST" }), env);
    // new URL() normalized the path before both the match and the forward
    expect(seen.url).toBe(`https://${ORIGIN}/pause`);
    expect(seen.headers.get("X-DriftScribe-Token")).toBeNull();
  });

  it("leaves untouched routes that are unauthenticated by design", async () => {
    const seen = stubFetch();
    await worker.fetch(req("/approvals/abc?t=hmac-token"), env);
    expect(seen.url).toBe(`https://${ORIGIN}/approvals/abc?t=hmac-token`);
    expect(seen.headers.get("X-DriftScribe-Token")).toBeNull();
  });
});
