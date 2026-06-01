"""Unit tests for the D5-2 content-only ``submit_slice_file`` tool factory.

Phase D5-2: the authority-clean hand-back tool each fan-out slice sub-agent
uses to submit its authored file. The trust property under test is that the
slice's target path is PINNED server-side (captured in a closure) — the
LLM-facing tool exposes ONLY ``content`` (+ optional ``citations``) and can
NEVER influence the path/repo/branch. This mirrors the authority-clean
philosophy of :func:`agent.adk_tools.open_infra_pr_tool` (LLM picks decision
content, routing fields are derived server-side).

The tool only RECORDS into the captured sink; it does not validate/reject
content (empty/size/path policy are enforced later by the barrier via
``validate_file_writes``). Last-write-wins if called twice in a slice.
"""
from __future__ import annotations

import inspect

from agent.fanout import make_submit_slice_file


def test_returned_callable_is_named_submit_slice_file() -> None:
    sink: dict = {}
    tool = make_submit_slice_file("iac/a.tf", sink)
    assert tool.__name__ == "submit_slice_file"


def test_signature_exposes_only_content_and_citations() -> None:
    # Authority-clean: no path/target_path/repo/branch param can reach the LLM.
    sink: dict = {}
    tool = make_submit_slice_file("iac/a.tf", sink)
    params = set(inspect.signature(tool).parameters)
    assert params == {"content", "citations"}
    for forbidden in ("path", "target_path", "repo", "branch", "base"):
        assert forbidden not in params


def test_records_pinned_path_regardless_of_content() -> None:
    sink: dict = {}
    tool = make_submit_slice_file("iac/pinned.tf", sink)
    tool(content='resource "x" "y" {}')
    assert sink["file"]["path"] == "iac/pinned.tf"
    assert sink["file"]["content"] == 'resource "x" "y" {}'


def test_records_pinned_path_even_if_content_mentions_other_path() -> None:
    # The content can say anything; the recorded path is the closure's, period.
    sink: dict = {}
    tool = make_submit_slice_file("iac/pinned.tf", sink)
    tool(content="path: iac/somewhere-else.tf")
    assert sink["file"]["path"] == "iac/pinned.tf"


def test_citations_recorded_when_provided() -> None:
    sink: dict = {}
    tool = make_submit_slice_file("iac/a.tf", sink)
    tool(content="x", citations=["doc-a", "doc-b"])
    assert sink["citations"] == ["doc-a", "doc-b"]


def test_citations_default_to_empty_list_when_omitted() -> None:
    sink: dict = {}
    tool = make_submit_slice_file("iac/a.tf", sink)
    tool(content="x")
    assert sink["citations"] == []


def test_citations_none_normalized_to_empty_list() -> None:
    sink: dict = {}
    tool = make_submit_slice_file("iac/a.tf", sink)
    tool(content="x", citations=None)
    assert sink["citations"] == []


def test_citations_copied_not_aliased() -> None:
    sink: dict = {}
    tool = make_submit_slice_file("iac/a.tf", sink)
    src = ["doc-a"]
    tool(content="x", citations=src)
    src.append("doc-b")  # mutating the caller's list must not affect the sink
    assert sink["citations"] == ["doc-a"]


def test_last_write_wins() -> None:
    sink: dict = {}
    tool = make_submit_slice_file("iac/a.tf", sink)
    tool(content="first")
    tool(content="second", citations=["c"])
    assert sink["file"]["content"] == "second"
    assert sink["citations"] == ["c"]


def test_ack_dict_shape() -> None:
    sink: dict = {}
    tool = make_submit_slice_file("iac/a.tf", sink)
    ack = tool(content="hello")
    assert ack == {"status": "recorded", "path": "iac/a.tf", "bytes": 5}


def test_ack_bytes_is_utf8_byte_length_not_codepoints() -> None:
    # "café" is 4 codepoints but 5 UTF-8 bytes (é -> 2 bytes).
    sink: dict = {}
    tool = make_submit_slice_file("iac/a.tf", sink)
    ack = tool(content="café")
    assert ack["bytes"] == len("café".encode("utf-8")) == 5
    assert ack["bytes"] != len("café")

    # A Japanese character is 3 UTF-8 bytes.
    ack2 = tool(content="あ")
    assert ack2["bytes"] == 3


def test_empty_content_recorded_as_is_not_rejected() -> None:
    sink: dict = {}
    tool = make_submit_slice_file("iac/a.tf", sink)
    ack = tool(content="")
    assert sink["file"] == {"path": "iac/a.tf", "content": ""}
    assert ack == {"status": "recorded", "path": "iac/a.tf", "bytes": 0}


def test_whitespace_content_recorded_as_is_not_rejected() -> None:
    sink: dict = {}
    tool = make_submit_slice_file("iac/a.tf", sink)
    tool(content="   \n\t ")
    assert sink["file"]["content"] == "   \n\t "
