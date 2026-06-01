import pytest
from driftscribe_lib.iac_editor_policy import (
    EditorPolicyError, validate_file_writes, validate_branch, validate_base,
    ALLOWED_BRANCH_PREFIX, ALLOWED_BASE, EDITOR_LABEL,
    MAX_FILES, MAX_FILE_BYTES,
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
