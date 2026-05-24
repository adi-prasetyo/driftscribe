// Passthrough proxy: rewrites the request URL to the Cloud Run hostname
// so Cloud Run accepts the Host header without a custom-domain mapping.
//
// The original request flow:
//   browser -> CF (driftscribe.adp-app.com) -> CF Access auth -> this Worker
//   -> fetch(<rewritten url>, request) -> Cloud Run
//
// `fetch(url, request)` re-issues the request to the new URL; CF derives
// SNI + Host from the rewritten URL, so Cloud Run sees a request it owns.
// All other headers (including Cf-Access-* set by Access) flow through.

const ORIGIN = "driftscribe-agent-u272wv52kq-an.a.run.app";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    url.hostname = ORIGIN;
    url.protocol = "https:";
    url.port = "";
    return fetch(url.toString(), request);
  },
};
