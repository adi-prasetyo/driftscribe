import pytest
from driftscribe_lib.iac_editor_policy import (
    EditorPolicyError, validate_file_writes, validate_branch, validate_base,
    validate_title_body,
    ALLOWED_BRANCH_PREFIX, ALLOWED_BASE, EDITOR_LABEL,
    MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_TITLE, MAX_BODY,
)


def _w(path, content="resource x {}\n"):
    return {"path": path, "content": content}


def test_accepts_iac_tf_and_md():
    assert validate_file_writes([_w("iac/cloudrun.tf"), _w("iac/README.md")])


def test_rejects_path_outside_iac():
    with pytest.raises(EditorPolicyError) as e:
        validate_file_writes([_w("agent/main.py")])
    assert e.value.status_code == 403


def test_rejects_absolute_and_traversal():
    for bad in ("/iac/x.tf", "iac/../agent/x.tf", "iac/./../x.tf", "iac//x.tf"):
        with pytest.raises(EditorPolicyError):
            validate_file_writes([_w(bad)])


def test_rejects_non_tf_md_suffix():
    for bad in ("iac/evil.sh", "iac/x.tofu", "iac/x.tf.json", "iac/x.tfvars"):
        with pytest.raises(EditorPolicyError):
            validate_file_writes([_w(bad)])


def test_rejects_foundation_files():
    # backend.tf is NOT protected (backend lives in versions.tf) — do not list it.
    for f in ("iac/versions.tf", "iac/providers.tf", "iac/variables.tf",
              "iac/imports.tf", "iac/.terraform.lock.hcl"):
        with pytest.raises(EditorPolicyError) as e:
            validate_file_writes([_w(f)])
        assert e.value.status_code == 403


def test_rejects_empty_list_dupes_empty_content():
    with pytest.raises(EditorPolicyError):
        validate_file_writes([])
    with pytest.raises(EditorPolicyError):
        validate_file_writes([_w("iac/a.tf"), _w("iac/a.tf")])
    with pytest.raises(EditorPolicyError):
        validate_file_writes([_w("iac/a.tf", content="")])


def test_size_bounds():
    big = "x" * (200_001)
    with pytest.raises(EditorPolicyError):
        validate_file_writes([_w("iac/a.tf", content=big)])     # per-file cap
    with pytest.raises(EditorPolicyError):
        validate_file_writes([_w(f"iac/f{i}.tf") for i in range(33)])  # file-count cap


def test_rejects_control_chars_and_nul():
    # Embedded NUL / newline must be rejected (would slip past splitext/normpath).
    for bad in ("iac/x\x00.tf", "iac/x\n.tf"):
        with pytest.raises(EditorPolicyError) as e:
            validate_file_writes([_w(bad)])
        assert e.value.status_code == 403


def test_rejects_backslash_paths():
    for bad in ("iac\\x.tf", "iac/sub\\..\\x.tf"):
        with pytest.raises(EditorPolicyError) as e:
            validate_file_writes([_w(bad)])
        assert e.value.status_code == 403


def test_accepts_at_limit_file_count():
    writes = [_w(f"iac/f{i}.tf") for i in range(MAX_FILES)]
    assert validate_file_writes(writes)


def test_accepts_at_limit_file_bytes():
    content = "x" * MAX_FILE_BYTES
    assert len(content.encode("utf-8")) == MAX_FILE_BYTES
    assert validate_file_writes([_w("iac/a.tf", content=content)])


def test_branch_rules():
    validate_branch("infra/add-bucket-x-20260601-ab12cd")
    for bad in ("upgrade/x", "infra/", "infra/..", "infra/a b", "infra/" + "z"*300):
        with pytest.raises(EditorPolicyError):
            validate_branch(bad)


def test_base_and_constants():
    validate_base("main")
    with pytest.raises(EditorPolicyError):
        validate_base("dev")
    assert ALLOWED_BRANCH_PREFIX == "infra/" and ALLOWED_BASE == "main"
    assert EDITOR_LABEL == "driftscribe-infra"


# Aggregate-byte ceiling (D1-3 hardening) -------------------------------- #


def test_aggregate_byte_ceiling_rejected():
    # Many files each well under the per-file cap (MAX_FILE_BYTES=200KB) but
    # summing just over the 1MB aggregate ceiling — the 32×200KB amplification
    # mitigation. Each chunk stays well under the per-file cap.
    chunk = "x" * 150_000  # under per-file cap; 7 of them = 1.05MB > 1MB
    writes = [_w(f"iac/f{i}.tf", content=chunk) for i in range(7)]
    total = sum(len(w["content"].encode("utf-8")) for w in writes)
    assert total > MAX_TOTAL_BYTES
    assert all(len(w["content"].encode("utf-8")) <= MAX_FILE_BYTES for w in writes)
    with pytest.raises(EditorPolicyError) as e:
        validate_file_writes(writes)
    assert e.value.status_code == 422


def test_aggregate_byte_ceiling_at_limit_ok():
    # Sum exactly at MAX_TOTAL_BYTES is accepted (boundary is inclusive); each
    # file stays under the per-file cap so only the aggregate rule is exercised.
    # 10 files × 100_000 bytes = 1_000_000 = MAX_TOTAL_BYTES.
    assert MAX_TOTAL_BYTES == 1_000_000
    chunk = "x" * 100_000
    writes = [_w(f"iac/f{i}.tf", content=chunk) for i in range(10)]
    total = sum(len(w["content"].encode("utf-8")) for w in writes)
    assert total == MAX_TOTAL_BYTES
    assert all(len(w["content"].encode("utf-8")) <= MAX_FILE_BYTES for w in writes)
    assert validate_file_writes(writes)


def test_aggregate_byte_ceiling_under_limit_ok():
    writes = [_w("iac/a.tf"), _w("iac/b.tf"), _w("iac/c.tf")]
    assert validate_file_writes(writes)


# Conservative ASCII path-char allowlist (D1-3 hardening) ---------------- #


def test_rejects_non_ascii_path_chars():
    # Fullwidth 'x', zero-width joiner, and a Cyrillic homoglyph all carry a
    # .tf suffix and survive normpath but must be rejected as confusables.
    for bad in (
        "iac/ｘ.tf",          # fullwidth x  → iac/ｘ.tf
        "iac/a‍b.tf",        # zero-width joiner between a and b
        "iac/х.tf",          # Cyrillic 'х' homoglyph of ASCII x
    ):
        with pytest.raises(EditorPolicyError) as e:
            validate_file_writes([_w(bad)])
        assert e.value.status_code == 403


def test_ascii_paths_still_accepted():
    # The new char allowlist must not regress legitimate ASCII paths.
    for ok in ("iac/cloud-run.tf", "iac/sub_dir/x.tf", "iac/a.b.tf", "iac/README.md"):
        assert validate_file_writes([_w(ok)])


# validate_title_body (D1-3 hardening) ----------------------------------- #


def test_validate_title_body_accepts_at_limit():
    # At the limit (codepoint length) is fine for both fields.
    validate_title_body("T" * MAX_TITLE, "B" * MAX_BODY)


def test_validate_title_body_rejects_oversize_title():
    with pytest.raises(EditorPolicyError) as e:
        validate_title_body("T" * (MAX_TITLE + 1), "ok body")
    assert e.value.status_code == 422
    assert "title" in e.value.reason


def test_validate_title_body_rejects_oversize_body():
    with pytest.raises(EditorPolicyError) as e:
        validate_title_body("ok title", "B" * (MAX_BODY + 1))
    assert e.value.status_code == 422
    assert "body" in e.value.reason
