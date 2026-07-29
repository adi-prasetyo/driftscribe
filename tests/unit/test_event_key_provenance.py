"""ds-b3m — the event key's env-provenance namespace.

``_event_key`` gained an ``env_observed`` flag so an ADK ``/recheck`` whose
Reader Worker read FAILED (and whose ``live_env`` is therefore reconstructed
from the model's own ``proposal.env_diffs``) cannot be served a decision cached
by a run that really did observe the service. The cached decision is returned
BEFORE ``validate()`` runs, so without the split the validator's refusal of an
unobserved env never gets to speak.

Two properties, and BOTH need pinning, because a change that preserved only the
first would look correct and silently invalidate every cached decision in
production:

1. separation — a reconstructed key differs from a grounded one;
2. BACKWARD COMPATIBILITY — a grounded key is byte-identical to what the
   pre-change function produced.

Property 2 is the one an obvious "fix" breaks: stamping ``env_provenance:
"observed"`` on grounded keys would separate the namespaces just as well and
pass every behavioural test, while moving every existing cache entry out from
under its own key.
"""
import hashlib
import json

from agent.main import _event_key

_ARGS = ("manual", "payment-demo", "demo/ops-contract.yaml", "abc123def4567890")
_ENV = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}


def _pre_change_event_key(trigger, service, contract_path, contract_hash, live_env):
    """The algorithm EXACTLY as it stood before ds-b3m, inlined.

    Independent of the implementation on purpose: comparing against a
    re-derivation of the current code would prove nothing, and a golden string
    alone would not say WHY it is that string.
    """
    payload = {
        "trigger": trigger,
        "service": service,
        "contract_path": contract_path,
        "contract_hash": contract_hash,
        "live_env": dict(sorted(live_env.items())),
    }
    h = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return f"{trigger}-{service}-{h}"


def test_a_grounded_key_is_byte_identical_to_the_pre_change_algorithm():
    """No cached decision from a grounded run is orphaned by this deploy."""
    legacy = _pre_change_event_key(*_ARGS, _ENV)
    assert _event_key(*_ARGS, _ENV) == legacy
    assert _event_key(*_ARGS, _ENV, env_observed=True) == legacy


def test_the_default_is_grounded():
    """Omitting the flag must mean "observed". Defaulting the other way would
    move every non-ADK caller into the reconstructed namespace at once."""
    assert _event_key(*_ARGS, _ENV) == _event_key(*_ARGS, _ENV, env_observed=True)


def test_a_reconstructed_key_differs_from_the_grounded_one_for_identical_env():
    """The whole point: identical CONTENT, different provenance, different key.

    Identical content is the realistic case, not a contrived one — the model has
    already seen the reader's result, so an accurate report reconstructs exactly
    the dict the coordinator would have read.
    """
    assert _event_key(*_ARGS, _ENV, env_observed=False) != _event_key(*_ARGS, _ENV)


def test_reconstructed_keys_are_still_stable_among_themselves():
    """Separation must not cost idempotency WITHIN the ungrounded lane: two
    ungrounded retries of the same state still collapse to one decision."""
    assert _event_key(*_ARGS, _ENV, env_observed=False) == _event_key(
        *_ARGS, dict(reversed(list(_ENV.items()))), env_observed=False
    )
