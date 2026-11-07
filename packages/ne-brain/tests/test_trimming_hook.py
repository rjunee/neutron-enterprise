# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the C7 hook algebra (``ne.brain.trimming``) at the seam.

Stub expanders live BELOW the ``GroupExpander`` seam (allowed): these tests pin
the fail-closed label algebra — always contains ``principal.id`` (even against a
broken expander), never ``None``, never ``{}``, sorted/deterministic, acl-key
only, and expander exceptions PROPAGATE (no silent unscoped fallback).
"""

from __future__ import annotations

import pytest

from ne.brain.trimming import PrincipalAclFilter, acl_match_labels
from ne.contracts.types import Principal


class _BrokenExpander:
    """Returns an EMPTY set — the worst-case 'lost all labels' expander."""

    def expand(self, principal: Principal | str) -> frozenset[str]:
        return frozenset()


class _FixedExpander:
    """Returns a fixed label set regardless of input."""

    def __init__(self, labels: frozenset[str]) -> None:
        self._labels = labels

    def expand(self, principal: Principal | str) -> frozenset[str]:
        return self._labels


class _RaisingExpander:
    def expand(self, principal: Principal | str) -> frozenset[str]:
        raise RuntimeError("expander backend down")


def test_acl_match_labels_always_contains_principal_id() -> None:
    p = Principal(id="u-ada", groups=("g-eng",))
    # Even a broken expander that drops every label cannot erase the own id.
    labels = acl_match_labels(p, _BrokenExpander())
    assert "u-ada" in labels
    assert labels  # never empty


def test_acl_match_labels_sorted_and_deterministic() -> None:
    p = Principal(id="u-ada", groups=())
    expander = _FixedExpander(frozenset({"g-zeta", "g-alpha", "u-ada", "g-mid"}))
    labels = acl_match_labels(p, expander)
    assert list(labels) == sorted(labels)
    assert labels == acl_match_labels(p, expander)  # deterministic


def test_acl_match_labels_without_expander_uses_token_claims_only() -> None:
    p = Principal(id="u-x", groups=("g-tok",))
    assert acl_match_labels(p, None) == ("g-tok", "u-x")


def test_acl_match_labels_expander_result_is_unioned_with_id() -> None:
    p = Principal(id="u-ada", groups=())
    expander = _FixedExpander(frozenset({"g-eng"}))  # expander omits the id
    labels = acl_match_labels(p, expander)
    assert set(labels) == {"u-ada", "g-eng"}


def test_hook_never_returns_none_and_never_empty() -> None:
    # Over a broken (empty) expander AND a groupless principal, the mapping is
    # always {'acl': [<nonempty>]}.
    hook = PrincipalAclFilter(_BrokenExpander())
    mapping = hook(Principal(id="u-solo", groups=()))
    assert mapping is not None
    assert mapping == {"acl": ["u-solo"]}

    hook2 = PrincipalAclFilter(_FixedExpander(frozenset({"u-solo", "g-eng"})))
    mapping2 = hook2(Principal(id="u-solo", groups=()))
    assert mapping2 == {"acl": ["g-eng", "u-solo"]}


def test_hook_only_emits_acl_key() -> None:
    hook = PrincipalAclFilter(_FixedExpander(frozenset({"u-x", "g-y"})))
    mapping = hook(Principal(id="u-x", groups=()))
    assert set(mapping.keys()) == {"acl"}


def test_hook_propagates_expander_errors() -> None:
    # Deny-by-erroring: an expander failure must NOT degrade to unscoped/None.
    hook = PrincipalAclFilter(_RaisingExpander())
    with pytest.raises(RuntimeError):
        hook(Principal(id="u-x", groups=()))
    with pytest.raises(RuntimeError):
        acl_match_labels(Principal(id="u-x", groups=()), _RaisingExpander())
