from pathlib import Path


def test_playwright_config_exists():
    assert Path("tests/e2e/ui/playwright.config.ts").exists()


def test_playwright_targets_chromium_only():
    body = Path("tests/e2e/ui/playwright.config.ts").read_text()
    assert "chromium" in body
    assert "webkit" not in body
    assert "firefox" not in body


def test_transparency_spec_exists():
    assert Path("tests/e2e/ui/tests/transparency.spec.ts").exists()


def test_transparency_spec_uses_correct_sessionstorage_key():
    """Phase 20 fix: key is driftscribe_token (underscore), not driftscribe.token."""
    body = Path("tests/e2e/ui/tests/transparency.spec.ts").read_text()
    assert "driftscribe_token" in body
    assert "driftscribe.token" not in body


def test_transparency_spec_does_not_use_old_auth_header():
    body = Path("tests/e2e/ui/tests/transparency.spec.ts").read_text()
    assert "X-Operator-Token" not in body


def test_transparency_spec_uses_data_testid_selectors():
    body = Path("tests/e2e/ui/tests/transparency.spec.ts").read_text()
    for tid in ("chat-prompt", "chat-submit", "final-response",
                "past-decisions-pane", "past-decision-item",
                "open-trace-button", "historical-banner"):
        assert f'data-testid="{tid}"' in body, f"spec missing selector for {tid}"
