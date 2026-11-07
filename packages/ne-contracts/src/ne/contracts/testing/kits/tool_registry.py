# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ToolRegistry conformance kit (NE-RTSWAP phase (a) — runtime-slot kits).

Precedent: :mod:`ne.contracts.testing.kits.skill_substrate` (capabilities-aware,
factory hooks, ``_require`` skip helper). ``ToolRegistry`` is a SYNC Protocol —
no ``asyncio.run`` here, unlike the async runtime-slot kits.

Deliberate NON-assertions, recorded here so a future binding is not failed by
invented law: re-registration semantics (does a second ``register()`` for the
same name overwrite or raise?) are undocumented by the Protocol and therefore
unasserted; the exact exception TYPE ``get()`` raises on a miss is
binding-owned — the frozen signature returns ``ToolSpec`` (never ``None``), so
the kit asserts only that SOME exception is raised.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from ne.contracts.runtime import ToolRegistry, ToolSpec
from ne.contracts.types import ApprovalTier


class ToolRegistryContractKit:
    """Subclass and override :meth:`create_registry` to prove ToolRegistry conformance."""

    #: The capabilities this suite asserts. A partial binding overrides this to
    #: an honest subset so the gated checks skip rather than fail.
    capabilities: ClassVar[frozenset[str]] = frozenset({"register", "missing"})

    def create_registry(self) -> ToolRegistry:
        """Return a fresh, empty registry."""
        raise NotImplementedError

    def make_spec(self, name: str) -> ToolSpec:
        """Build a valid spec named ``name`` for the register/get/list checks."""
        return ToolSpec(
            name=name,
            description=f"kit tool {name}",
            tier=ApprovalTier.TIER_0,
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )

    def _require(self, cap: str) -> None:
        if cap not in self.capabilities:
            pytest.skip(f"registry does not declare the {cap!r} capability")

    def test_register_then_get_returns_equal_spec(self) -> None:
        self._require("register")
        registry = self.create_registry()
        spec = self.make_spec("read.kit.alpha")
        registry.register(spec)
        # ToolSpec is a frozen pydantic model — equality is well-defined.
        assert registry.get("read.kit.alpha") == spec

    def test_list_tools_contains_registered_specs(self) -> None:
        self._require("register")
        registry = self.create_registry()
        names = ("read.kit.alpha", "read.kit.bravo", "read.kit.charlie")
        for name in names:
            registry.register(self.make_spec(name))
        listed = registry.list_tools()
        assert all(isinstance(spec, ToolSpec) for spec in listed)
        assert set(names) <= {spec.name for spec in listed}

    def test_get_unknown_name_raises(self) -> None:
        self._require("missing")
        registry = self.create_registry()
        with pytest.raises(Exception):  # noqa: B017 - exception TYPE is binding-owned
            registry.get("kit.no.such.tool")
