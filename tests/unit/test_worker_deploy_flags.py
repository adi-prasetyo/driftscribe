"""Deploy-flag parity pins for the infra-reader worker.

infra/cloudbuild.infra-reader.yaml's header claims it "mirrors the
infra-reader deploy step in infra/cloudbuild.yaml" — these pins enforce
the claim, and pin the deploy-churn fix (concurrency=8, 2026-06-12 plan).

Comparison semantics (Codex 019eb9ca): flags are compared as a mapping
after normalizing ``${NAME}`` through each file's own ``substitutions``
defaults (so ``--region=${_REGION}`` equals the literal region, and
``--image=...:${_TAG}`` compares equal since both files default ``_TAG``).
``--set-env-vars`` is parsed into key/value pairs: key sets must match
and every VALUE must match except ``IAC_SNAPSHOT_SHA``, which is
legitimately ``${_IAC_SNAPSHOT_SHA}`` vs the ``$COMMIT_SHA`` builtin —
that one key's value is the only thing this pin does not see.
"""
import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _infra_reader_deploy(cloudbuild_path: Path) -> tuple[dict, dict]:
    """Return (flags, env_vars) for the infra-reader gcloud run deploy step."""
    doc = yaml.safe_load(cloudbuild_path.read_text(encoding="utf-8"))
    subs = doc.get("substitutions") or {}

    def _normalize(value: str) -> str:
        return re.sub(
            r"\$\{(\w+)\}", lambda m: subs.get(m.group(1), m.group(0)), value
        )

    for step in doc["steps"]:
        args = step.get("args") or []
        if not (
            step.get("entrypoint") == "gcloud"
            and "deploy" in args
            and "driftscribe-infra-reader" in args
        ):
            continue
        flags: dict[str, str] = {}
        env: dict[str, str] = {}
        for a in args:
            if not isinstance(a, str) or not a.startswith("--"):
                continue
            key, _, value = a.partition("=")
            value = _normalize(value)
            if key == "--set-env-vars":
                env = dict(kv.split("=", 1) for kv in value.split(","))
            else:
                flags[key] = value
        return flags, env
    raise AssertionError(f"no infra-reader deploy step found in {cloudbuild_path}")


def test_infra_reader_deploy_flags_match_between_files():
    t_flags, t_env = _infra_reader_deploy(
        _REPO_ROOT / "infra" / "cloudbuild.infra-reader.yaml"
    )
    f_flags, f_env = _infra_reader_deploy(_REPO_ROOT / "infra" / "cloudbuild.yaml")
    assert t_flags == f_flags
    assert set(t_env) == set(f_env)
    for key in t_env:
        if key == "IAC_SNAPSHOT_SHA":
            continue  # ${_IAC_SNAPSHOT_SHA} vs $COMMIT_SHA builtin — see module doc
        assert t_env[key] == f_env[key], key


def test_infra_reader_concurrency_is_8():
    for fname in ("cloudbuild.infra-reader.yaml", "cloudbuild.yaml"):
        flags, _ = _infra_reader_deploy(_REPO_ROOT / "infra" / fname)
        assert flags.get("--concurrency") == "8", fname
