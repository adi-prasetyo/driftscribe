"""No future-promising adoption copy ("not YET adoptable" papercut, 2026-06-12).

Operator-facing copy states PRESENT capability — the roadmap's copy
discipline (items 10/14/15) — so "not yet adoptable" / "adoptable today"
style promises are banned from every source copy surface. Tests dirs are
not scanned (this file must be free to name the phrases); agent/static/
is build output, refreshed by the coordinator rebake.
"""
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_BANNED = ("not yet adoptable", "adoptable today", "not adoptable types yet")

_SCAN_GLOBS = (
    ("workloads", "**/*.md"),
    ("frontend/src", "**/*.svelte"),
    ("frontend/src", "**/*.ts"),
    ("driftscribe_lib", "**/*.py"),
    ("agent", "**/*.py"),
    ("agent/templates", "**/*.html"),
)


def test_no_future_promising_adoption_copy():
    offenders: list[str] = []
    for base, glob in _SCAN_GLOBS:
        for path in sorted((_REPO_ROOT / base).glob(glob)):
            if "static" in path.parts:
                continue
            # Whitespace-normalize: prompts hard-wrap, so a banned phrase can
            # straddle a newline (Codex 019eb9d9 — the provision prompt's
            # "not yet\n  adoptable" would otherwise escape a plain substring).
            text = " ".join(path.read_text(encoding="utf-8").lower().split())
            for phrase in _BANNED:
                if phrase in text:
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}: {phrase!r}")
    assert not offenders, "\n".join(offenders)
