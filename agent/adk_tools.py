"""ADK-facing tool wrappers.

These are sync functions registered as Google ADK tools. They are intentionally
read-only and side-effect-free (except for the network calls they explicitly
make on the agent's behalf). Redaction of secret-named values is handled
downstream at the proposal-validation layer (see `agent.secret_guard`), not
here — these wrappers are trusted boundaries that must not lie about what the
live environment / PRs / contract actually contain.

`load_contract_tool` returns a raw dict (not `OpsContract`) on purpose: the
LLM-driven path lets the agent reason over the parsed YAML shape, while the
deterministic path keeps using `agent.contract.load_contract` which returns
the validated pydantic model.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from github import Github

from agent.cloud_run_client import read_live_env


def read_live_env_tool(service: str, region: str, project: str) -> dict[str, str]:
    """Read the current env block from the latest revision of a Cloud Run service.

    Single source for skip-logic (Secret-Manager-backed entries): delegates to
    `agent.cloud_run_client.read_live_env`.
    """
    return read_live_env(service, region, project)


def call_debug_config_tool(url: str) -> dict[str, Any]:
    """Call the target service's /debug/config endpoint.

    On any failure (HTTP error, timeout, non-JSON body, transport error)
    returns `{"_error": "<message>"}` so the LLM can reason about the failure
    instead of crashing. The catch is intentionally broad per the plan spec.
    """
    try:
        r = httpx.get(url, timeout=5.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001 — broad-catch is the documented contract
        msg = str(e) or type(e).__name__
        return {"_error": msg}


def _list_recent_merged_prs(repo_full: str, days: int, token: str = "") -> list[dict]:
    """List merged PRs whose merged_at falls inside the last `days` days.

    Note on iteration: PyGithub yields PRs sorted by *updated* desc, not
    *merged* desc. A PR can be touched recently (so it appears early) while
    being merged outside the window — we `continue` past those, never `break`,
    so a fresher in-window PR later in the stream is still captured.

    Unauthenticated mode hits the 60/hour anonymous GitHub rate limit, which
    is acceptable for the demo. Note: PyGithub's `Github("")` raises in newer
    versions, so we coerce empty string to `None` before construction.
    """
    g = Github(token or None)
    repo = g.get_repo(repo_full)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict] = []
    for pr in repo.get_pulls(state="closed", sort="updated", direction="desc"):
        if pr.merged_at is None or pr.merged_at < since:
            continue  # see iteration note above — do NOT break
        out.append(
            {
                "title": pr.title or "",
                "body": pr.body or "",
                "url": pr.html_url,
                "merged": True,
            }
        )
    return out


def search_recent_prs_tool(
    repo_full: str,
    keywords: list[str],
    days: int = 7,
    token: str = "",
) -> list[dict]:
    """Find merged PRs in the last `days` days whose title or body contains
    any of `keywords` as an exact word-boundary token.

    Case-sensitive on purpose: mirrors `agent.classifier._strict_pr_match` so
    the LLM-driven path and the deterministic classifier agree on what
    "matches a PR" means. Empty `keywords` returns `[]` without hitting the
    GitHub API.
    """
    if not keywords:
        return []
    prs = _list_recent_merged_prs(repo_full, days, token)
    patterns = [re.compile(rf"\b{re.escape(k)}\b") for k in keywords]
    return [
        pr
        for pr in prs
        if any(p.search(f"{pr['title']} {pr['body']}") for p in patterns)
    ]


def load_contract_tool(path: str) -> dict[str, Any]:
    """Load and return the parsed ops-contract.yaml as a raw dict.

    Returns a dict (not `OpsContract`) by design — see the module docstring.
    """
    return yaml.safe_load(Path(path).read_text())
