"""Polling + parsing helpers for E2E tests."""
import time
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs

import httpx


class PollTimeout(AssertionError):
    pass


def wait_for(
    predicate: Callable[[], Any],
    *,
    timeout: float = 60.0,
    interval: float = 2.0,
    description: str = "condition",
) -> Any:
    deadline = time.monotonic() + timeout
    last_value = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval)
    raise PollTimeout(f"timed out waiting for {description} (last={last_value!r})")


def wait_for_trace_complete(
    client: httpx.Client, trace_id: str, *, timeout: float = 120.0
) -> dict:
    """Poll GET /trace/{trace_id} until response['complete'] is True.

    /trace/{id} returns {trace_id, events, decision, complete, fetched_from_cache}.
    The 'complete' flag means observed-stability has elapsed (_STABILITY_GRACE_S=30s
    in the coordinator); 120s timeout = grace + log tail + ADK slow path headroom.
    """
    def _check():
        resp = client.get(f"/trace/{trace_id}")
        if resp.status_code != 200:
            return None
        body = resp.json()
        return body if body.get("complete") else None

    return wait_for(_check, timeout=timeout, interval=3.0, description=f"trace {trace_id} complete")


def parse_approval_url(text: str) -> tuple[str, str, str]:
    """Extract (full_url, approval_id, token) from coordinator response text.

    URL shape: {COORDINATOR_URL}/approvals/{id}?t=<token>.
    Both id and t are required. Uses urlparse (clearer than regex).
    """
    import re
    match = re.search(r"https?://\S+/approvals/[A-Za-z0-9_-]+\?t=[A-Za-z0-9_.\-]+", text)
    if not match:
        raise AssertionError(
            f"expected approval URL '.../approvals/<id>?t=<token>'; got: {text[:500]}"
        )
    full_url = match.group(0).rstrip(".,)\"'")
    parsed = urlparse(full_url)
    path_parts = parsed.path.rstrip("/").split("/")
    approval_id = path_parts[-1]
    token_list = parse_qs(parsed.query).get("t", [])
    if not token_list:
        raise AssertionError(f"approval URL missing ?t= query param: {full_url}")
    return full_url, approval_id, token_list[0]


def find_approval_url_in_trace_events(events: list[dict]) -> tuple[str, str, str] | None:
    """Walk a /trace events list looking for an approval URL.

    Preferred over reading from /chat reply because:
    - tool_result events carry the worker's structured output (where the URL is
      synthesized);
    - the LLM's free-form reply may paraphrase or omit it.
    """
    for ev in events:
        # Stringify the event and scan — keeps the helper resilient to schema
        # tweaks in the events shape.
        try:
            text = repr(ev)
        except Exception:
            continue
        if "/approvals/" in text and "?t=" in text:
            try:
                return parse_approval_url(text)
            except AssertionError:
                continue
    return None
