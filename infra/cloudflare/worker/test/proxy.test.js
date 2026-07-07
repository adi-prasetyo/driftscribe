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
    ["GET", "/infra/pending-approvals", true],
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
    ["POST", "/infra/pending-approvals", false],
    // path shape matters
    ["GET", "/trace/a/b", false],
    ["GET", "/infra/pending-approvals/extra", false],
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

describe("chat rate limiter (hackathon A.4)", () => {
  const chatReq = (headers = {}) =>
    req("/chat", {
      method: "POST",
      body: JSON.stringify({ prompt: "hi", workload: "drift" }),
      duplex: "half",
      headers: { "Content-Type": "application/json", ...headers },
    });

  const envWith = (limit) => ({
    DEMO_MODE: "1",
    DEMO_TOKEN: "the-real-token",
    CHAT_RATE_LIMIT: { limit },
  });

  it("429s anonymous POST /chat when the limiter says no — origin never contacted", async () => {
    const seen = stubFetch();
    const limit = vi.fn(async () => ({ success: false }));
    const resp = await worker.fetch(
      chatReq({ "CF-Connecting-IP": "203.0.113.9" }),
      envWith(limit),
    );
    expect(resp.status).toBe(429);
    expect(resp.headers.get("Retry-After")).toBe("60");
    expect((await resp.json()).detail).toMatch(/rate limit/i);
    // keyed on the CF-set client IP, not anything client-controlled
    expect(limit).toHaveBeenCalledWith({ key: "203.0.113.9" });
    // the token was never granted and the origin never saw the request
    expect(seen.url).toBeUndefined();
  });

  it("forwards with token + marker when the limiter allows", async () => {
    const seen = stubFetch();
    const limit = vi.fn(async () => ({ success: true }));
    await worker.fetch(
      chatReq({ "CF-Connecting-IP": "203.0.113.9" }),
      envWith(limit),
    );
    expect(limit).toHaveBeenCalledOnce();
    expect(seen.url).toBe(`https://${ORIGIN}/chat`);
    expect(seen.headers.get("X-DriftScribe-Token")).toBe("the-real-token");
    expect(seen.headers.get("X-DriftScribe-Demo-Anonymous")).toBe("1");
  });

  it("keys on 'unknown' when CF-Connecting-IP is absent (vitest has no CF edge)", async () => {
    stubFetch();
    const limit = vi.fn(async () => ({ success: true }));
    await worker.fetch(chatReq(), envWith(limit));
    expect(limit).toHaveBeenCalledWith({ key: "unknown" });
  });

  it("never consults the limiter when a CF Access JWT is present (operator unthrottled)", async () => {
    const seen = stubFetch();
    const limit = vi.fn(async () => ({ success: false }));
    await worker.fetch(
      chatReq({ "Cf-Access-Jwt-Assertion": "real.jwt.value" }),
      envWith(limit),
    );
    expect(limit).not.toHaveBeenCalled();
    expect(seen.url).toBe(`https://${ORIGIN}/chat`); // forwarded, JWT decides at origin
  });

  it("never consults the limiter on allowlisted GETs (reads stay unthrottled)", async () => {
    const seen = stubFetch();
    const limit = vi.fn(async () => ({ success: false }));
    await worker.fetch(req("/decisions"), envWith(limit));
    expect(limit).not.toHaveBeenCalled();
    expect(seen.headers.get("X-DriftScribe-Token")).toBe("the-real-token");
  });

  it("never consults the limiter when DEMO_TOKEN is unset (nothing to protect)", async () => {
    const seen = stubFetch();
    const limit = vi.fn(async () => ({ success: false }));
    await worker.fetch(chatReq(), {
      DEMO_MODE: "1",
      CHAT_RATE_LIMIT: { limit },
    });
    expect(limit).not.toHaveBeenCalled();
    expect(seen.headers.get("X-DriftScribe-Token")).toBeNull(); // 401s at origin
  });

  it("never consults the limiter outside demo mode", async () => {
    const seen = stubFetch();
    const limit = vi.fn(async () => ({ success: false }));
    await worker.fetch(chatReq(), {
      DEMO_MODE: "0",
      DEMO_TOKEN: "the-real-token",
      CHAT_RATE_LIMIT: { limit },
    });
    expect(limit).not.toHaveBeenCalled();
    expect(seen.url).toBe(`https://${ORIGIN}/chat`);
  });

  it("fails open when the binding is missing (older deploy / local dev)", async () => {
    const seen = stubFetch();
    await worker.fetch(chatReq(), {
      DEMO_MODE: "1",
      DEMO_TOKEN: "the-real-token",
    });
    expect(seen.url).toBe(`https://${ORIGIN}/chat`);
    expect(seen.headers.get("X-DriftScribe-Token")).toBe("the-real-token");
  });

  it("fails open when the limiter throws (limiter outage must not kill the demo)", async () => {
    const seen = stubFetch();
    const limit = vi.fn(async () => {
      throw new Error("limiter unavailable");
    });
    await worker.fetch(chatReq(), envWith(limit));
    expect(seen.url).toBe(`https://${ORIGIN}/chat`);
    expect(seen.headers.get("X-DriftScribe-Token")).toBe("the-real-token");
  });
});
