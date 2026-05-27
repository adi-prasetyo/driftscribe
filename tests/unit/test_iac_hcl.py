"""Focused unit tests for the shared policy-free HCL primitives.

driftscribe_lib.iac_hcl holds the parsing/iteration helpers lifted out of
tools/iac_static_gate.py in Phase B. These tests pin the primitives directly
(the golden-parity test guards equivalence with the gate's old behavior).
"""
from driftscribe_lib import iac_hcl


# --- is_meta_key -------------------------------------------------------------


def test_is_meta_key_true_for_dunder_keys():
    for k in ("__is_block__", "__comments__", "__inline_comments__", "__start_line__"):
        assert iac_hcl.is_meta_key(k) is True


def test_is_meta_key_false_for_real_identifiers():
    for k in ("google", "name", "_private", "__leading_only", "trailing__"):
        assert iac_hcl.is_meta_key(k) is False


def test_is_meta_key_false_for_non_str():
    assert iac_hcl.is_meta_key(123) is False  # type: ignore[arg-type]


# --- unwrap ------------------------------------------------------------------


def test_unwrap_strips_surrounding_quotes():
    assert iac_hcl.unwrap('"hashicorp/google"') == "hashicorp/google"


def test_unwrap_leaves_unquoted_string_untouched():
    assert iac_hcl.unwrap("google") == "google"


def test_unwrap_non_string_passes_through():
    assert iac_hcl.unwrap(42) == 42
    assert iac_hcl.unwrap(None) is None
    assert iac_hcl.unwrap(["a"]) == ["a"]


def test_unwrap_single_quote_char_is_not_stripped():
    # A lone '"' is len 1: too short to be a wrapped scalar.
    assert iac_hcl.unwrap('"') == '"'


# --- block_label -------------------------------------------------------------


def test_block_label_normalizes_quoted_key():
    assert iac_hcl.block_label('"google_cloud_run_v2_service"') == "google_cloud_run_v2_service"


# --- parse_hcl ---------------------------------------------------------------


def test_parse_hcl_valid_returns_dict():
    parsed = iac_hcl.parse_hcl('resource "google_x" "y" { name = "n" }')
    assert isinstance(parsed, dict)
    assert "resource" in parsed


def test_parse_hcl_invalid_returns_none():
    assert iac_hcl.parse_hcl('resource "x" { = = = }') is None


# --- iter_blocks -------------------------------------------------------------


def test_iter_blocks_list_shape():
    parsed = iac_hcl.parse_hcl(
        'resource "google_x" "a" {}\nresource "google_y" "b" {}'
    )
    blocks = iac_hcl.iter_blocks(parsed, "resource")
    assert isinstance(blocks, list)
    assert len(blocks) == 2
    assert all(isinstance(b, dict) for b in blocks)


def test_iter_blocks_missing_kind_returns_empty():
    parsed = iac_hcl.parse_hcl('resource "google_x" "a" {}')
    assert iac_hcl.iter_blocks(parsed, "module") == []


def test_iter_blocks_dict_shape_wrapped_in_list():
    # A dict (not list) under a kind is wrapped into a single-element list.
    assert iac_hcl.iter_blocks({"terraform": {"a": 1}}, "terraform") == [{"a": 1}]


def test_iter_blocks_non_container_returns_empty():
    assert iac_hcl.iter_blocks({"resource": "scalar"}, "resource") == []


# --- iter_typed_blocks -------------------------------------------------------


def test_iter_typed_blocks_yields_type_name_body():
    parsed = iac_hcl.parse_hcl(
        'resource "google_cloud_run_v2_service" "payment_demo" { name = "payment-demo" }'
    )
    out = list(iac_hcl.iter_typed_blocks(parsed, "resource"))
    assert len(out) == 1
    rtype, name, body = out[0]
    assert rtype == "google_cloud_run_v2_service"
    assert name == "payment_demo"
    assert isinstance(body, dict)
    assert iac_hcl.unwrap(body["name"]) == "payment-demo"


def test_iter_typed_blocks_skips_dunder_meta_keys_on_commented_source():
    # Comments make hcl2 inject __comments__/__inline_comments__ meta keys;
    # those must never be yielded as a resource type or name.
    src = (
        "resource \"google_x\" \"y\" {\n"
        "  # a comment that triggers hcl2 metadata\n"
        "  name = \"n\"  # inline\n"
        "}\n"
    )
    parsed = iac_hcl.parse_hcl(src)
    out = list(iac_hcl.iter_typed_blocks(parsed, "resource"))
    # Exactly one real block; no dunder meta key leaked in as a type/name.
    assert [(t, n) for t, n, _ in out] == [("google_x", "y")]
    _, _, body = out[0]
    assert iac_hcl.unwrap(body["name"]) == "n"


def test_iter_typed_blocks_missing_kind_yields_nothing():
    parsed = iac_hcl.parse_hcl('resource "google_x" "y" {}')
    assert list(iac_hcl.iter_typed_blocks(parsed, "data")) == []
