# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ONLY way a test may address a Qdrant server, and the janitor that owns it.

⛔ THIS IS AN OUTAGE FIX, NOT A CONVENIENCE.

2026-09-10 — the live demo's Qdrant went ``status: red``. Its optimizer reported
``Failed to remove segment: Too many open files (os error 24)`` and every write
failed ``Can't write WAL: "no segments available"``; the corpus seed of that
night's deploy failed as a direct result. The cause was **985 orphaned test
collections** in the LIVE store (986 total; exactly one real: ``ne_chunks``).
Segments x collections had exhausted the process's file-descriptor ceiling.

Every one of those orphans came from a test, through two defects that compound:

1. ``os.environ.get("NE_QDRANT_URL", <the deployed store's own address>)`` — a
   DEFAULT, with a skip that fired only when that address was
   UNREACHABLE. On a box running the demo stack that port IS the deployed
   vector store and it is very much reachable, so a plain
   ``pytest packages/ne-brain`` wrote into production. **REACHABILITY IS NOT
   CONSENT** — the identical lesson the OPA incidents taught
   (``packages/ne-mcp/tests/test_opa_pdp_mutation_guardian.py``), and the
   identical arming expression.
2. **No cleanup at all.** ``create_store()`` minted ``kit_<uuid4>`` per kit case
   and never dropped it. Nothing deleted anything, on any path — pass, fail or
   error.

THE FIX IS STRUCTURAL, NOT PROCEDURAL, AND THE STRUCTURE IS *CONSENT BY NAMING*.

⭐ **An address only exists here if one of the two TEST-CONSENT environment
variables is holding it right now** (:data:`TEST_CONSENT_URL_VARS` — and NOT the
deployed application's own ``NE_API_QDRANT_URL``, which is production
configuration, not a test's permission). :func:`server_reachable` will not probe
an address the environment does not name, :class:`OwnedQdrantCollections` refuses
to be built on one, and :func:`sweep_named_server` takes no address at all. So a
**defaulted READ** of the variable — the 2026-09-10 arming expression, and any
re-spelling of it in any module including one nobody has written yet — **cannot
take effect**: the address it produces is one the operator did not name, and every
door onto a server in this package is shut against it.

⭐ **AND NAMING ALONE WAS NOT ENOUGH — 2026-09-11, THE FOURTH ROUND.** A test can
forge the naming without ever spelling an address, and without tripping any of the
guardian's four source rules::

    KEY = "NE_QDRANT_URL"                              # an indirect NAME node, so
    os.environ[KEY] = os.environ["NE_API_QDRANT_URL"]  # the write rule sees nothing
    owner = OwnedQdrantCollections(qdrant_server_url())

Two indirections at once: the variable being written is a ``Name``, not a string
literal, and the value is *copied from the deployed application's own production
setting*. Measured on the round-3 code, all four source rules returned ``False``
and the constructor was accepted. A SOURCE SCAN CAN ONLY EVER CATCH SPELLINGS
SOMEONE THOUGHT OF; four rounds of finding the next indirection is the approach
losing.

⛔ **SO THE LOAD-BEARING GUARANTEE IS NOW A RUNTIME REFUSAL, NOT A SOURCE RULE.**
The address is knowable at the moment a door is handed it, and so is the
deployment's: :func:`deployment_qdrant_addresses` reads the values of every
Qdrant-address variable the ``deploy/`` tree configures **that is present in this
process's environment right now**, and every door refuses an address that matches
one (:func:`deployment_qdrant_url_vars` derives the NAMES from ``deploy/**``; they
are never hand-listed). Comparison is on the normalised endpoint — scheme-less,
trailing slash, letter case, implied port, and every NUMERIC host reduced to its
canonical address by ADDRESS SEMANTICS (:func:`_canonical_host`, which
:func:`is_loopback_host` is the loopback question asked of) rather than by a list
of spellings — because an equality check a trailing slash defeats is the same
defect one spelling over. It does not matter which variable is holding the
address, or how it got there.

⭐ **ROUND 5 WAS THAT DEFECT, IN THE PART THAT CALLED ITSELF SPELLING-INDEPENDENT.**
The normalisation was a five-element frozen set — ``localhost``, ``127.0.0.1``,
``0.0.0.0``, ``::``, ``::1`` — and BOTH refusal rules stood on it, so a host
outside it was neither equal to the deployment's value nor recognised as loopback
and every door opened. ``localhost.``, ``127.1``, ``2130706433``, ``127.0.0.2``
and ``::ffff:127.0.0.1`` all reach a 127.0.0.1 listener and none of them was in
the set. A set of spellings is the same losing move one layer down from the source
rules, so the set is gone: an address is parsed the way a socket parses it
(``inet_aton`` shorthand included) and classified by ``is_loopback``, and only the
two RFC 6761 NAMES that no parser can decide are still listed.

⭐ **AND ROUND 7 WAS THE SAME SHAPE AGAIN, ONE SET OUT.** Round 5 gave ONE NAMED
SET — loopback — real address semantics and left "everything else" on string
equality: ``_endpoint`` folded a loopback host to a canonical token and passed
every other IP literal through as a RAW STRING. Measured on that code with
``NE_API_QDRANT_URL=http://10.0.0.1:6333``, the consent values
``http://167772161:6333`` (the same address as one integer) and
``http://10.0.0.01:6333`` (a zero-padded octet) were NOT refused, and neither were
the ``inet_aton`` shorthand ``10.1``, the hex form ``0x0a.0.0.1``, or
``::ffff:10.0.0.1``; the fully expanded IPv6 spelling of a compressed deployment
address was not refused either. Once the refusal returns ``None`` the whole chain
opens — ``qdrant_server_url`` hands the URL back, ``OwnedQdrantCollections``
accepts it, and :func:`sweep_named_server`'s BULK DELETE is aimed at it. **The fix
was not a sixth special case:** the numeric branch is no longer a set at all. Every
host that parses as an address — in any IPv4 or IPv6 spelling — is reduced to that
address's canonical form, so there is no "everything else" left to be one spelling
behind. What remains outside it is NAMES, and that is a boundary this module cannot
cross without a resolver rather than a spelling it has not got to yet.

⚠️ **WHAT THIS STILL DOES NOT COVER, BECAUSE AN OVERSTATED GUARANTEE IS WORSE THAN
A NARROW ONE.**

1. **A deployment variable that is not in the environment names nothing.** The
   refusal compares against variables *present in this process*. A pytest run that
   inherits no deployment configuration cannot be told that the loopback address on
   Qdrant's own port is the live store — and it must not be told so by a HARDCODED
   address either, because CI's own Qdrant service container answers on exactly
   that address (``.github/workflows/ci.yml``, the ``Test`` step) and
   ``deploy/docker/ne-app/README.md`` spells it too. **No address literal can tell
   the live store from CI's throwaway one; only the environment can.** Partially
   mitigated: when ANY deployment Qdrant variable *is* present, a LOOPBACK address
   is refused whatever that variable holds, which covers the case where one store
   has two addresses — its compose-network name inside, the host port it publishes
   outside.
2. **An operator who exports ``NE_QDRANT_URL`` at the live store, in a shell with
   no deployment variable in it, is obeyed.** That is the consent this design is
   deliberately built on, and nothing here can distinguish it from the intended
   use.
3. **THE REFUSAL IS ADDRESS-EQUALITY PLUS LOOPBACK. WHAT IT IS INDIFFERENT TO IS
   SPELLING; WHAT IT IS NOT INDIFFERENT TO IS IDENTITY.** Stated as the line it
   actually draws, which is between NUMERIC hosts and NAMES:

   *Covered — one token per address, so two spellings of one address always
   compare equal.* Every host that parses as an IP address is reduced by
   :func:`_canonical_host` to that address's canonical form. For IPv4 that means
   the dotted quad regardless of how it was written, including the ``inet_aton``
   shorthands (``10.1``), the one-integer form (``167772161``), octal and
   hexadecimal octets (``0012.0.0.1``, ``0x0a.0.0.1``), zero padding
   (``10.0.0.01``) and IPv4-mapped IPv6 (``::ffff:10.0.0.1``). For IPv6 it means
   the lowercase RFC 5952 compressed form, so expanded, partially expanded,
   bracketed and upper-case spellings agree. A trailing root dot is stripped
   first. Loopback and the unspecified addresses fold one step further, to a
   single token, so ``127.0.0.2``, ``127.1``, ``2130706433``, ``0.0.0.0``, ``::``,
   ``::1`` and ``::ffff:127.0.0.1`` are all one endpoint — plus the only two NAMES
   a parser can decide without a resolver, ``localhost`` and ``*.localhost``
   (RFC 6761 §6.3).

   *NOT covered, and not coverable here, because deciding it needs a resolver and
   nothing in this module may reach one.* **Every one of these reaches the live
   store and every one of them is ACCEPTED:**

   * another NAME that points at this machine — ``ip6-localhost`` from a distro's
     ``/etc/hosts``, this box's own hostname, a CNAME, or any DNS record an
     operator aimed at 127.0.0.1;
   * another ADDRESS of this machine — a LAN address, a docker bridge address, a
     container's address on the compose network when this process is on it. These
     are canonicalised now, but canonicalising an address is not knowing it is an
     interface of this host, and that is a different question this module does not
     ask;
   * a CNAME or a second DNS name for the deployment's own host, or the deployment's
     name against its own address — two NAMES, or a name and an address, never
     compare equal here, only two spellings of the same numeric host do;
   * a proxy or port-forward in front of the deployed store on an address that is
     neither loopback nor the configured one — a different address really is a
     different endpoint, and the refusal has no way to see through the hop;
   * a different PORT or PATH of the same server: the endpoint is the triple, by
     design, because the deployment's own port is what distinguishes it;
   * an address string this module cannot parse at all (``_endpoint`` returns
     ``None``, so nothing is refused) — narrow in practice because an HTTP client
     cannot dial one either, which is why unbracketed IPv6 is not exploitable.

   The residual protection for all of them is rule 1 only: if a deployment variable
   in this process is holding a string that normalises to the same endpoint, it is
   refused; otherwise nothing here sees it. ⛔ Do not restate the covered half as
   "every spelling" without the word NUMERIC in the same sentence — that elision is
   what made this paragraph wrong in rounds 1, 3, 4, 5 and 6, and the recurring
   defect in this file is the over-claim rather than any one gap.

4. **THE DEPLOYMENT VARIABLES ARE DERIVED BY A NAME HEURISTIC, AND A HEURISTIC IS
   NOT AN ENUMERATION.** :func:`deployment_qdrant_url_vars` reads ``deploy/**`` and
   keeps identifiers that contain ``QDRANT``/``VECTOR`` and END with one of
   :data:`_ADDRESS_SUFFIXES`. Round 6 found it missing a name that is in the tree
   TODAY — the bare ``QDRANT_URL`` of ``deploy/compose/dev/seed/seed.sh``, excluded
   by a leading ``[A-Z]`` — along with ``NE_API_QDRANT_DSN`` and
   ``NE_API_VECTOR_HOSTNAME``. Those three are fixed. **What is NOT fixed, and
   cannot be by this approach:** a deployment variable whose name contains neither
   token (``NE_API_STORE_URL``, ``EMBEDDINGS_BACKEND``, ``RETRIEVER_TARGET``), or
   whose name ends in a word the suffix list does not have. Such a variable is
   derived by nothing, so an inherited value of it activates NEITHER refusal rule
   and this module cannot tell it from an unrelated string. Qdrant's own
   double-underscore self-configuration (``QDRANT__SERVICE__HOST``) is excluded
   deliberately — it is a BIND address, not a target. ⭐ The only way to retire
   this limit is to stop guessing names: have the deploy path write its Qdrant
   address into a file the test session reads, so the refusal compares against a
   value the deployment stated rather than one a pattern found.

5. **Naming is only consent if the OPERATOR did the naming**, and nothing at
   runtime tells an exported variable from one the process set itself::

       os.environ.setdefault("NE_QDRANT_URL", _A + _B)   # every door then opens
       os.environ["NE_QDRANT_URL"] = _A + _B             # ...and so does this one

   Both are accepted, correctly, by the code below: by the time it looks, the
   environment really does name the address. **That hole is closed by a SOURCE
   rule** — ``test_no_first_party_source_writes_a_test_consent_variable`` in the
   guardian — **not by this module**, so it is only ever as wide as the guardian's
   globs, and the round-4 bypass above is exactly a spelling those globs missed.
   A forged naming that points somewhere OTHER than the deployment is still only
   covered by the source rule.

So, explicitly, which half carries which guarantee, and stated as narrowly as it
is true: **the runtime refusal is the load-bearing half.** It compares ADDRESSES,
so it is indifferent to how the caller obtained the string — a new variable, a new
indirection, a value copied out of production — and indifferent to how a NUMERIC
host is SPELLED, in every IPv4 and IPv6 spelling of one address. **It is not
indifferent to which address or name is used to reach the same server**: a second
name for the deployment's host, a name against its own address, or a second
interface address, is a different endpoint and is not refused (residual limit 3
above). Do not restate this as "cannot be routed around", and do not drop the word
NUMERIC from the spelling claim — that is the elision this file has now shipped
five times.
**The guardian's source rules
(``packages/ne-brain/tests/test_qdrant_live_store_guardian.py``) are an early,
cheap signal in front of it**, valuable because they fail at review time rather
than at run time, but they can only ever catch the forms someone wrote down.
Do not read a green guardian as proof that no path to the live store exists.

The second half is ownership: a test may not build a Qdrant client against a URL;
it asks :class:`OwnedQdrantCollections` for one, and every collection name it
hands out is registered for teardown before it can be created — and is forgotten
**only once its delete has actually succeeded**, so a transient teardown failure
leaves the name behind to be retried and logged instead of silently orphaned.

⭐ **AND THE OWNERSHIP HALF HAD TWO HOLES OF ITS OWN, FOUND IN ROUND 6.**

* :meth:`OwnedQdrantCollections.collection` refuses a prefix the sweep cannot
  recognise — but only for names a test ASKS it for. An owned client could be
  handed any name at all, producing a collection registered for no teardown and
  matching no sweep pattern. :meth:`OwnedQdrantCollections.client` now returns a
  client that refuses to CREATE a name this process did not mint
  (:class:`UnownedQdrantCollection`). ⚠️ It guards CREATION only, on clients this
  janitor handed out; a test that constructs its own vendor client against the
  same URL is outside it, and that path is held by the guardian's rule 1 — a
  source rule, and therefore only as wide as its globs.
* :func:`sweep_named_server` read ``NE_QDRANT_URL`` alone while BOTH members of
  :data:`TEST_CONSENT_URL_VARS` grant ownership. ``NE_TEST_QDRANT_URL`` is the one
  ``.github/workflows/ci.yml`` sets on the promoted source-authority step — a
  REQUIRED gate that mints ``authority_<uuid4>`` collections — so a killed worker
  there left orphans the advertised recovery never looked for. It now sweeps every
  address :func:`consented_server_urls` returns. ⚠️ That is still exactly two
  variables, read at session end: a store a killed run reached through a variable
  that is no longer set is swept by nothing here.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import functools
import ipaddress
import logging
import os
import pathlib
import re
import urllib.parse
import uuid

from qdrant_client import AsyncQdrantClient, QdrantClient

__all__ = [
    "NE_QDRANT_URL_VAR",
    "NE_TEST_QDRANT_URL_VAR",
    "SWEEPABLE_COLLECTION_PREFIXES",
    "TEST_CONSENT_URL_VARS",
    "DeploymentQdrantServer",
    "OwnedQdrantCollections",
    "UnnamedQdrantServer",
    "UnownedQdrantCollection",
    "consented_server_urls",
    "deployment_qdrant_addresses",
    "deployment_qdrant_url_vars",
    "is_loopback_host",
    "operator_named_servers",
    "qdrant_server_url",
    "retry_pending_drops",
    "server_reachable",
    "sweep_named_server",
]

logger = logging.getLogger(__name__)

#: The env var naming a Qdrant server for the live-parked proofs.
#:
#: ⛔ IT HAS NO DEFAULT, AND IT NEVER GETS ONE AGAIN. A default is what turns
#: "the operator named a store" into "whatever answers the deployment's port".
NE_QDRANT_URL_VAR = "NE_QDRANT_URL"

#: The env var naming the promoted source-authority gate's OWN service container
#: (``.github/workflows/ci.yml``, on that step alone).
NE_TEST_QDRANT_URL_VAR = "NE_TEST_QDRANT_URL"

#: ⭐ THE COMPLETE SET OF VARIABLES THAT GRANT A TEST CONSENT. ENUMERATED, NOT
#: PATTERN-MATCHED.
#:
#: ⛔ A PATTERN IS NOT CONSENT. The first cut of this rule accepted every
#: ``NE_*QDRANT_URL`` variable — which includes **``NE_API_QDRANT_URL``, the
#: DEPLOYED APPLICATION'S OWN PRODUCTION SETTING** (``deploy/compose/pilot/
#: compose.yaml``, ``deploy/helm/**``). On a box running the demo stack that
#: variable holds the live store's address, so a run with only production
#: configuration in its environment had its "consent" granted by production
#: configuration, and :class:`OwnedQdrantCollections` opened onto the very store
#: the 985 orphans landed in. Consent has to mean *a variable that exists in order
#: to grant test consent*, and neither of these two configures anything that runs
#: in production:
#:
#: * ``NE_QDRANT_URL`` — the live-parked proofs; named by ci.yml's ``Test`` step,
#:   pointing at that job's own service container.
#: * ``NE_TEST_QDRANT_URL`` — the promoted source-authority gate, likewise.
#:
#: Adding a third is a deliberate act, and
#: ``test_qdrant_live_store_guardian.py`` asserts this set stays disjoint from
#: every Qdrant variable ``deploy/**`` configures.
TEST_CONSENT_URL_VARS: tuple[str, ...] = (NE_QDRANT_URL_VAR, NE_TEST_QDRANT_URL_VAR)

#: The collection-name prefixes tests may mint, and therefore the ONLY prefixes
#: :func:`sweep_named_server` will delete.
#:
#: ⛔ ONE LIST, BOTH JOBS — ON PURPOSE. :meth:`OwnedQdrantCollections.collection`
#: REFUSES a prefix that is not here, so the sweep can never fall behind the tests
#: the way a hand-maintained cleanup list does. Four of these named the 985
#: orphans of 2026-09-10 (``kit_``, ``kw_``, ``spine_``, ``rag_public_live_``).
#: Adding a prefix is a deliberate act that arms the sweep for it in the same
#: edit; ``ne_chunks`` and every other real collection can match neither.
SWEEPABLE_COLLECTION_PREFIXES: tuple[str, ...] = (
    "authority",
    "guardian",
    "kit",
    "kw",
    "kw_e2e",
    "rag_public_live",
    "spine",
)

#: ``<sanctioned prefix>_<uuid4 hex>`` and nothing else is ever deletable in bulk.
_SWEEPABLE_NAME_RE = re.compile(
    "^(?:" + "|".join(re.escape(p) for p in SWEEPABLE_COLLECTION_PREFIXES) + ")_[0-9a-f]{32}$"
)


class UnnamedQdrantServer(RuntimeError):
    """Raised when code tries to address a Qdrant the operator never named.

    This is the structural half of the 2026-09-10 fix. A defaulted address is
    exactly an address no environment variable holds, so it lands here instead of
    landing in the deployed store.
    """


class DeploymentQdrantServer(RuntimeError):
    """Raised when a NAMED address turns out to be the DEPLOYMENT's own store.

    The 2026-09-11 round-4 bypass: a test copies ``NE_API_QDRANT_URL`` into a
    consent variable, so by the time :func:`operator_named_servers` looks, the
    environment genuinely names the address — and the address is production's.
    Naming was satisfied; the store was still the live one.

    Distinct from :class:`UnnamedQdrantServer` on purpose: "nobody named this" and
    "this is the deployment wearing a test's name" are different failures and need
    different messages. Unnamed is checked FIRST, so an address that is both keeps
    reporting the older, broader reason.
    """


class UnownedQdrantCollection(RuntimeError):
    """Raised when a client from :meth:`OwnedQdrantCollections.client` is asked to
    CREATE a collection whose name this process did not mint.

    :meth:`OwnedQdrantCollections.collection` refuses a prefix the session sweep
    cannot recognise — but that gate only covers names it was ASKED for. A test
    holding an owned client could always pass a name of its own to
    ``create_collection`` and get a collection that is registered for nothing and
    recognised by nothing: invisible to :meth:`OwnedQdrantCollections.drop_all`
    (which deletes the registry) AND to :func:`sweep_named_server` (which matches
    :data:`SWEEPABLE_COLLECTION_PREFIXES` plus a uuid4). That is an orphan by
    construction, on the live-parked path, and no source rule is needed to close
    it: the client itself refuses.
    """


# --------------------------------------------------------------------------- #
# ⭐ THE RUNTIME REFUSAL — the load-bearing guarantee. See the module docstring
# for what it does and does not cover, and why the source rules are not it.
# --------------------------------------------------------------------------- #

#: Matches the NAME of an environment variable that configures a Qdrant address.
#:
#: ⛔ USED ONLY TO REFUSE, NEVER TO GRANT. A pattern cannot be consent — that is
#: the mistake :data:`TEST_CONSENT_URL_VARS` documents — but it can perfectly well
#: withhold it, and the failure mode of an over-broad refusal is a parked test with
#: a loud reason, not a write into production.
#:
#: ⚠️ The leading ``[A-Z]`` this used to require meant a BARE ``QDRANT_URL`` did not
#: match — and ``deploy/compose/dev/seed/seed.sh`` sets exactly that. One character
#: of pattern, one whole variable missed; see :data:`_DEPLOYMENT_ADDRESS_VAR_RE`,
#: which had the same bug, and residual limit 4 for what a NAME pattern still
#: cannot do.
_QDRANT_URL_VAR_RE = re.compile(r"[A-Z0-9_]*QDRANT[A-Z0-9_]*URL")

#: Words that END the name of a variable holding an address, most specific first.
#:
#: ⚠️ A LIST, AND THEREFORE NOT EXHAUSTIVE — said here rather than left implied.
#: ``NE_API_QDRANT_DSN`` was missed by the first four entries; whatever word the
#: next chart invents will be missed by these sixteen. Residual limit 5 in the
#: module docstring states the consequence instead of this file claiming a closure
#: it cannot have. ``HOSTNAME`` precedes ``HOST`` because the alternation is
#: first-match and the trailing ``(?![A-Z0-9_])`` would otherwise reject
#: ``..._HOSTNAME`` outright.
_ADDRESS_SUFFIXES: tuple[str, ...] = (
    "URL",
    "URI",
    "ENDPOINTS",
    "ENDPOINT",
    "ADDRESS",
    "ADDR",
    "HOSTNAME",
    "HOST",
    "DSN",
    "SERVER",
    "TARGET",
    "LOCATION",
    "ORIGIN",
    "CONNECTION",
    "CONN",
    "BASE",
)

#: The same job over ``deploy/**``, deliberately WIDER than the pattern above.
#:
#: ⭐ IF THE TWO WERE THE SAME REGEX THE DERIVATION WOULD BE DECORATION: every name
#: it found would already be matched by :data:`_QDRANT_URL_VAR_RE` against the
#: environment, and reading ``deploy/**`` would buy nothing. It is wider instead, so
#: a deployment variable that does not happen to be spelled ``*QDRANT*URL*``
#: — ``NE_API_VECTOR_ENDPOINT``, say — is still refused the day it is added to a
#: chart, with no edit to this file. Breadth is safe here for the same reason as
#: above: this regex can only ever withhold permission.
#:
#: ⭐ ROUND 6 WIDENED IT TWICE, BECAUSE IT MISSED A NAME THAT IS ACTUALLY IN THE
#: TREE. It required a character BEFORE ``QDRANT``, so the bare ``QDRANT_URL`` in
#: ``deploy/compose/dev/seed/seed.sh`` derived nothing at all and an inherited
#: ``QDRANT_URL`` activated NEITHER refusal rule; and its four suffixes missed
#: ``NE_API_QDRANT_DSN`` and ``NE_API_VECTOR_HOSTNAME``. Both are fixed — and the
#: fix is still a NAME HEURISTIC, so residual limit 4 now says what it cannot see
#: rather than this comment claiming every deployment variable is derived.
#:
#: ⚠️ Qdrant's own double-underscore self-configuration (``QDRANT__SERVICE__HOST``)
#: is deliberately NOT matched: it is the address the SERVER binds to, not one a
#: client dials, and treating it as a deployment address would make ``present``
#: non-empty — and therefore refuse every loopback store — inside the container
#: that runs Qdrant itself.
_DEPLOYMENT_ADDRESS_VAR_RE = re.compile(
    r"(?<![A-Z0-9_])"
    r"(?:[A-Z0-9]+_)*"
    r"(?:QDRANT|VECTORDB|VECTORS|VECTOR)"
    r"(?:_[A-Z0-9]+)*?"
    r"_?(?:" + "|".join(_ADDRESS_SUFFIXES) + r")"
    r"(?![A-Z0-9_])"
)

#: The single token every recognised loopback spelling is folded to, so two
#: addresses that reach the same local server compare equal.
_LOOPBACK_CANON = "localhost"

#: The loopback NAMES — i.e. the ones that are not addresses at all.
#:
#: ⚠️ THIS LIST IS NOT THE GUARANTEE, AND IT IS NOT COMPLETE. It cannot be: a name
#: means an address only to a resolver, and this module does not resolve names.
#: What IS decidable without one is RFC 6761 §6.3, which reserves ``localhost`` and
#: every name under ``.localhost`` for the loopback interface — so exactly those two
#: forms are recognised. ``ip6-localhost`` out of a distro's ``/etc/hosts``, this
#: box's own hostname, a CNAME, or any DNS name an operator has pointed at
#: 127.0.0.1 reach the same Qdrant and are NOT recognised here. See residual limit 3
#: in the module docstring; the ADDRESS half below is the part that is exhaustive.
_LOOPBACK_NAMES = frozenset({"localhost"})

#: RFC 6761 §6.3: ``anything.localhost`` is the loopback interface too.
_LOOPBACK_NAME_SUFFIX = ".localhost"

#: Ports implied by a scheme, so ``http://q`` and ``http://q:80`` compare equal.
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _shorthand_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """``127.1`` / ``2130706433`` / ``0x7f.0.0.1`` -> what a socket would dial.

    ⛔ ``ipaddress.ip_address`` REJECTS EVERY ONE OF THESE, AND EVERY HTTP CLIENT
    ACCEPTS THEM. That gap is the round-5 defect in one sentence: ``127.1`` is not
    "not an address", it is ``127.0.0.1`` written short, and a check that only knows
    the dotted-quad spelling waves it through. ``inet_aton`` — and therefore glibc,
    and therefore anything that opens a socket — reads one to four parts where the
    LAST part fills every byte the leading ones did not, each part decimal, octal
    (leading ``0``) or hexadecimal (leading ``0x``).

    Parsed here rather than through ``socket.inet_aton`` so that no string can ever
    be handed to something that might resolve it: this runs at collection time in
    every live-parked module, and a DNS lookup there is exactly the kind of I/O this
    janitor exists to keep out. ``None`` means "not an IPv4 address in any
    spelling", which includes every hostname.
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values: list[int] = []
    for part in parts:
        lowered = part.lower()
        try:
            if lowered.startswith("0x"):
                value = int(lowered, 16)
            elif lowered.startswith("0") and len(lowered) > 1:
                value = int(lowered, 8)
            else:
                value = int(lowered, 10)
        except ValueError:
            return None
        if value < 0:
            return None
        values.append(value)
    *leading, last = values
    if any(value > 0xFF for value in leading):
        return None
    if last >= 1 << (8 * (4 - len(leading))):
        return None
    packed = last
    for index, value in enumerate(leading):
        packed |= value << (8 * (3 - index))
    return ipaddress.IPv4Address(packed)


def _as_ip_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``host`` as the address a socket would dial, or ``None`` if it is a NAME.

    Both spelling families are covered: everything ``ipaddress`` parses (dotted
    quad, IPv6, IPv4-mapped IPv6) and everything only ``inet_aton`` parses
    (:func:`_shorthand_ipv4`). An IPv4-mapped IPv6 address is reduced to the IPv4
    address it carries, because ``::ffff:127.0.0.1`` reaches the same listener
    ``127.0.0.1`` does.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return _shorthand_ipv4(host)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_loopback_host(host: str) -> bool:
    """Does ``host`` address the machine this process is running on?

    ⭐ WHAT THIS COVERS, EXACTLY, BECAUSE THE FROZEN SET IT REPLACES CLAIMED MORE
    THAN IT DELIVERED AND THAT OVER-CLAIM IS THE RECURRING DEFECT IN THIS FILE.

    Covered — and covered by ADDRESS SEMANTICS, so exhaustively rather than by
    enumeration:

    * every address in ``127.0.0.0/8``, not just ``127.0.0.1`` (``127.0.0.2`` is a
      different address and the same machine);
    * every shorthand spelling of those (``127.1``, ``2130706433``, ``0x7f.0.0.1``),
      because :func:`_shorthand_ipv4` reads them the way a socket does;
    * ``::1``, and IPv4-mapped forms such as ``::ffff:127.0.0.1``;
    * the unspecified addresses ``0.0.0.0`` and ``::``, which a client dials as
      loopback;
    * a trailing root dot on any of the above;
    * the names ``localhost`` and ``*.localhost`` (RFC 6761 §6.3).

    It is the loopback QUESTION asked of :func:`_canonical_host`, which is the one
    parse in this file — so the source rules that call this and the runtime refusal
    that normalises through :func:`_endpoint` cannot drift apart again.

    ⛔ NOT covered, and it cannot be without resolving names: any OTHER name that
    points here — ``ip6-localhost``, the box's own hostname, a CNAME, a DNS record
    an operator aimed at 127.0.0.1 — and any address on another local interface
    (a LAN address of this host, a docker bridge address). Those reach the same
    Qdrant and this function returns ``False`` for them. Residual limit 3 in the
    module docstring says so in the same words; do not restate this as "every
    spelling of loopback" anywhere.
    """
    return _canonical_host(host) == _LOOPBACK_CANON


def _canonical_host(host: str) -> str | None:
    """``host`` -> the ONE token every NUMERIC spelling of that host must share.

    ⭐ ROUND 7. Round 5 gave a single NAMED SET — loopback — real address semantics
    and left everything else on string equality, which is the frozen set's defect
    one layer out: with ``NE_API_QDRANT_URL=http://10.0.0.1:6333``, the consent
    values ``http://167772161:6333`` and ``http://10.0.0.01:6333`` are the SAME
    ADDRESS and compared unequal, so every door opened — including the sweep, which
    is a bulk delete. The fix is not another special case: EVERY numeric host is
    folded through the one parser, so no address family is "everything else".

    Three outcomes, and the boundary between them is the only thing this decides:

    * ``None`` — there is no host at all (empty, or a bare root dot).
    * :data:`_LOOPBACK_CANON` — an address :func:`_as_ip_address` reads as loopback
      or unspecified, or one of the two RFC 6761 loopback NAMES.
    * otherwise — a NUMERIC host as ``ipaddress`` canonicalises it (dotted quad for
      v4, lowercase compressed form for v6, so ``2001:0db8:0:0:0:0:0:1`` and
      ``2001:db8::1`` are one token), or, when it parses as no address at all, the
      NAME itself, lowercased and root-dot-stripped and otherwise untouched.

    ⛔ A NAME IS NEVER RESOLVED — not here and nowhere in this module. This runs at
    collection time in every live-parked module and ``socket`` is deliberately not
    imported. So two different NAMES for one host, and a name against the address it
    resolves to, still compare UNEQUAL; that is residual limit 3 in the module
    docstring and it is unchanged by this round. What IS now exhaustive is the
    NUMERIC half: two spellings of one IP literal always produce one token.
    """
    # Normalised here rather than in each caller, because :func:`is_loopback_host`
    # is public and a caller that hands it a raw host out of a regex match must get
    # the same answer as one that hands it a parsed URL's hostname.
    host = host.strip().lower().rstrip(".")
    if not host:
        return None
    address = _as_ip_address(host)
    if address is not None:
        if address.is_loopback or address.is_unspecified:
            return _LOOPBACK_CANON
        # ``compressed`` is ``ipaddress``'s canonical spelling: the dotted quad for
        # v4 (so integer, octal, hex and zero-padded forms all land on it) and the
        # lowercase RFC 5952 form for v6 (so expanded and compressed agree).
        return address.compressed
    if host in _LOOPBACK_NAMES or host.endswith(_LOOPBACK_NAME_SUFFIX):
        return _LOOPBACK_CANON
    return host


def _repo_root() -> pathlib.Path:
    """``packages/ne-brain/src/ne/brain/testing/qdrant_live.py`` -> the checkout."""
    return pathlib.Path(__file__).resolve().parents[6]


def _deploy_dir() -> pathlib.Path | None:
    """``deploy/``, or ``None`` when it is not on disk (an installed wheel)."""
    deploy = _repo_root() / "deploy"
    return deploy if deploy.is_dir() else None


@functools.cache
def _qdrant_url_vars_under(root: str) -> frozenset[str]:
    """Every Qdrant-address variable NAME mentioned anywhere under ``root``.

    Cached: the tree does not change while a pytest session runs, and this is on
    the path of every door. Tests that move the tree call ``cache_clear()``.
    """
    found: set[str] = set()
    for path in sorted(pathlib.Path(root).rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found.update(_DEPLOYMENT_ADDRESS_VAR_RE.findall(text))
    return frozenset(found)


def deployment_qdrant_url_vars() -> frozenset[str]:
    """Every Qdrant-address variable the ``deploy/`` tree configures.

    ⛔ DERIVED, NOT ENUMERATED — for the same reason
    ``test_the_deployed_configurations_variables_are_not_test_consent`` derives its
    set: a hand-written list is a list of the variables someone remembered, and
    this module has now been defeated four times by something nobody remembered.
    Add a Qdrant variable to a Helm chart or a compose file and it is refused here
    in the same commit, with no edit to this file.

    :data:`TEST_CONSENT_URL_VARS` is subtracted so the module can never refuse its
    own consent variables; the guardian asserts separately that the two sets are
    disjoint in the tree anyway, which is where that overlap should be caught.

    ⚠️ Empty when ``deploy/`` is not on disk — an installed wheel ships no deploy
    tree, and there the runtime refusal contributes nothing. The guardian asserts
    this is non-vacuous in a checkout.
    """
    deploy = _deploy_dir()
    if deploy is None:
        return frozenset()
    return _qdrant_url_vars_under(str(deploy)) - frozenset(TEST_CONSENT_URL_VARS)


def deployment_qdrant_addresses() -> dict[str, str]:
    """``{variable: address}`` for every deployment Qdrant variable SET RIGHT NOW.

    The environment, not the tree, is what makes an address the deployment's: the
    tree only says which variables carry one. A variable matching
    :data:`_QDRANT_URL_VAR_RE` that the tree does not mention is included too —
    an unknown variable holding a Qdrant address is not a test's consent either,
    and refusing it costs nothing.

    ⛔ The consent variables are excluded, and that is the ONLY exclusion. There is
    no allowlist to repopulate and no way to name a variable that is exempt.
    """
    consent = frozenset(TEST_CONSENT_URL_VARS)
    deployed = deployment_qdrant_url_vars()
    found: dict[str, str] = {}
    for name, raw in os.environ.items():
        if name in consent:
            continue
        if name not in deployed and _QDRANT_URL_VAR_RE.fullmatch(name) is None:
            continue
        if value := raw.strip():
            found[name] = value
    return found


def _endpoint(url: str | None) -> tuple[str, int | None, str] | None:
    """``url`` reduced to ``(host, port, path)``, or ``None`` if it names no host.

    Normalises the differences a naive ``==`` is defeated by: a missing scheme
    (round 1 found a scheme-less ``NE_QDRANT_URL`` reaching the deployed store), a
    trailing slash, letter case, a trailing root dot on the host, the scheme's
    implied port, and — through :func:`_canonical_host` — EVERY spelling of a
    NUMERIC host, loopback or not.

    ⭐ THAT LAST PART IS ROUND 7. Until it, only loopback was canonicalised and every
    other IP literal stayed a raw string, so with the deployment on ``10.0.0.1`` the
    consent values ``167772161`` and ``10.0.0.01`` were different endpoints and every
    door opened, the sweep's bulk delete included. There is now no "everything else"
    branch: a host either parses as an address, in which case its canonical form is
    the token, or it is a name.

    ⛔ IT DOES NOT NORMALISE NAMES — against addresses or against each other — and it
    never will: two different names for one host, or a name and the address it
    resolves to, compare UNEQUAL here unless both are in the loopback family.
    Deciding that needs a resolver, and nothing in this module may reach one.
    Residual limit 3 in the module docstring is the same statement from the
    refusal's side.
    """
    if not url:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = "http://" + candidate
    try:
        parts = urllib.parse.urlsplit(candidate)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:  # an unparseable port, e.g. "http://h:notaport"
        return None
    # One call, one parse: the trailing DNS root label, the loopback family and the
    # canonical form of every other numeric host are all decided in there.
    canonical = _canonical_host(host)
    if canonical is None:
        return None
    host = canonical
    if port is None:
        port = _DEFAULT_PORTS.get(parts.scheme.lower())
    return (host, port, parts.path.rstrip("/"))


def _deployment_refusal(url: str | None) -> str | None:
    """Why ``url`` must not be addressed because it is the DEPLOYMENT's, else ``None``.

    Two rules, both address-based and therefore indifferent to how the caller came
    by the string:

    1. **The address IS a deployment address.** Some deployment variable in this
       environment holds the same normalised endpoint. That is the round-4 bypass
       exactly: the value was copied out of ``NE_API_QDRANT_URL``.
    2. **A deployment is configured here and the address is LOOPBACK.** One store
       has two addresses — ``http://qdrant:6333`` on the compose network, and the
       loopback address on the host that published its port — so rule 1 alone
       misses the published-port spelling. When any deployment Qdrant variable is
       present, this process is running beside a deployment and a loopback Qdrant
       is presumed to be it. LOOPBACK here means exactly what
       :func:`is_loopback_host` decides — every address spelling, two names — and
       explicitly NOT another name or another interface address that reaches this
       same machine.

       ⚠️ This rule is why it matters that CI sets NO deployment variable: its own
       throwaway service container answers on that same loopback address and must
       stay addressable. The discriminator is the environment, never the address —
       no hardcoded address could tell the two apart, which is why there is none.
    """
    endpoint = _endpoint(url)
    if endpoint is None:
        return None
    present = deployment_qdrant_addresses()
    if not present:
        return None
    holders = sorted(name for name, value in present.items() if _endpoint(value) == endpoint)
    if holders:
        return (
            f"{url!r} is the address the DEPLOYED application is configured with "
            f"({', '.join(holders)}). A consent variable holding it is production "
            "configuration wearing a test's name — which is how a test grants itself "
            "permission to write to the live store without ever spelling its address."
        )
    if endpoint[0] == _LOOPBACK_CANON:
        return (
            f"{url!r} is a LOOPBACK Qdrant, and this process is configured for a "
            f"deployment ({', '.join(sorted(present))}). A deployed store is reachable "
            "both on its compose network and on the host port it publishes, so a "
            "loopback address here is presumed to be it. Unset the deployment "
            "variable(s) if this really is a throwaway test server."
        )
    return None


def operator_named_servers() -> frozenset[str]:
    """Every Qdrant address a TEST-CONSENT variable is holding right now.

    Consent is a property of the ENVIRONMENT, not of the string. That is what
    makes a hardcoded fallback inert: it can be written, but it names nothing.

    ⛔ ONLY :data:`TEST_CONSENT_URL_VARS` count — see that constant. A variable
    that merely *happens to hold a Qdrant address* (``NE_API_QDRANT_URL`` is the
    deployed app's own) is production configuration, and production configuration
    is not a test's permission to write.
    """
    return frozenset(
        value.strip()
        for name in TEST_CONSENT_URL_VARS
        if (value := os.environ.get(name, "")).strip()
    )


def qdrant_server_url() -> str | None:
    """The Qdrant server the operator NAMED, or ``None`` if they named none.

    ⛔ ...AND ``None`` ALSO WHEN WHAT THEY NAMED IS THE DEPLOYMENT'S OWN STORE.
    This is the callers' gate: every live-parked module decides whether to run by
    asking here, so refusing at this seam turns the round-4 bypass into an ordinary
    park instead of a write into production. The reason is logged at ERROR — a
    silent skip is how a hole stays open for four rounds.
    """
    named = os.environ.get(NE_QDRANT_URL_VAR, "").strip()
    if not named:
        return None
    refusal = _deployment_refusal(named)
    if refusal is not None:
        logger.error(
            "%s is set, but it is REFUSED: %s Parking as if no server were named.",
            NE_QDRANT_URL_VAR,
            refusal,
        )
        return None
    return named


def consented_server_urls() -> tuple[str, ...]:
    """Every address a TEST-CONSENT variable names AND the refusal does not reject.

    ⛔ THE SWEEP USED TO READ ONE VARIABLE OUT OF TWO, AND THE OTHER ONE IS WHERE
    THE REQUIRED GATE RUNS. :data:`TEST_CONSENT_URL_VARS` has two members and both
    grant ownership — ``NE_TEST_QDRANT_URL`` is the one
    ``.github/workflows/ci.yml`` sets on the promoted source-authority step, which
    runs ``test_immutable_artifact_store_postgres_qdrant.py`` and mints
    ``authority_<uuid4>`` collections through :class:`OwnedQdrantCollections`. But
    :func:`sweep_named_server` took its target from :func:`qdrant_server_url`,
    which reads :data:`NE_QDRANT_URL_VAR` alone — so a killed worker running the
    REQUIRED gate left orphans on a server the advertised session recovery never
    looked at. The recovery mechanism had the orphan-accumulation bug.

    Addresses are de-duplicated on the NORMALISED endpoint (:func:`_endpoint`), so
    two variables spelling one store are swept once; an address neither variable
    can parse is kept as itself rather than silently dropped.

    ⚠️ Still only these two variables, and still only what they hold RIGHT NOW —
    this reads consent, it does not discover servers. A store a killed run reached
    through a variable that is no longer set is not swept by anything.
    """
    urls: list[str] = []
    seen: set[tuple[str, int | None, str] | str] = set()
    for name in TEST_CONSENT_URL_VARS:
        candidate = os.environ.get(name, "").strip()
        if not candidate:
            continue
        refusal = _deployment_refusal(candidate)
        if refusal is not None:
            logger.error(
                "%s is set, but the sweep REFUSES it: %s Sweeping nothing there.",
                name,
                refusal,
            )
            continue
        key: tuple[str, int | None, str] | str = _endpoint(candidate) or candidate
        if key in seen:
            continue
        seen.add(key)
        urls.append(candidate)
    return tuple(urls)


def _named_or_none(url: str | None) -> str | None:
    """``url`` if it is both NAMED and not the deployment's own, else ``None``.

    ⛔ TWO REFUSALS, CHECKED IN THAT ORDER. Naming first, so an address that is
    neither named nor the deployment's keeps reporting the older, broader reason;
    then :func:`_deployment_refusal`, which is the load-bearing one because it
    compares ADDRESSES — so no re-spelling of an address it already knows gets
    past it, while a DIFFERENT address for the same server still does (module
    docstring, residual limit 3).
    """
    if not url:
        return None
    candidate = url.strip()
    if candidate in operator_named_servers():
        refusal = _deployment_refusal(candidate)
        if refusal is None:
            return candidate
        logger.error("refusing to address Qdrant: %s", refusal)
        return None
    logger.warning(
        "refusing to address Qdrant at %r: no test-consent variable (%s) names it. "
        "An address that is not in the environment is a DEFAULT, and a default is "
        "what put 985 orphaned test collections in the live store on 2026-09-10. "
        "The deployed app's own NE_API_QDRANT_URL is NOT consent.",
        candidate,
        ", ".join(TEST_CONSENT_URL_VARS),
    )
    return None


def server_reachable(url: str | None) -> bool:
    """Is the NAMED Qdrant answering? Unset, unnamed or malformed is never reachable.

    Reachability is a LIVENESS check on a store the operator already consented
    to by naming it. It is not, and must never again be, the consent itself — so
    an address the environment does not hold returns ``False`` without a socket
    ever being opened, however it came to be passed in — and neither does a NAMED
    address that :func:`_deployment_refusal` identifies as the deployment's own.
    ⛔ That second refusal matters most here: this is the one door that opens a
    socket, so probing the live store is itself part of the damage.

    Probed through ``qdrant_client`` rather than a bare HTTP GET: ne-brain's C2
    egress guardian (``tests/test_c2_egress_guardian.py``) allows an outbound HTTP
    client library in ``embedder.py`` and ``reranker.py`` ONLY, and extending that
    allowlist for a test helper would trade a real guard for a convenience.

    ⚠️ The CONSTRUCTOR is inside the ``try``. ``QdrantClient(url=...)`` parses the
    URL eagerly and raises ``ValueError: Unknown scheme`` on a malformed one; with
    it outside, a typo'd ``NE_QDRANT_URL`` turned every module-level park in this
    package into a COLLECTION ERROR — which under ``-n auto`` is the "Different
    tests were collected between gw0 and gwN" flake class this module exists to
    remove. Callers get a parked skip, which is what a bad address deserves.
    """
    named = _named_or_none(url)
    if named is None:
        return False
    client: QdrantClient | None = None
    try:
        client = QdrantClient(url=named, timeout=2, check_compatibility=False)
        client.get_collections()
        return True
    except Exception:
        return False
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


class OwnedQdrantCollections:
    """Hands out Qdrant clients and collection names, and drops what it handed out.

    A name is registered by :meth:`collection` BEFORE the caller creates it, so a
    test that dies between naming and creating still gets a (harmless) delete
    attempt, and a test that dies after creating still gets the real one. Drive it
    from a fixture whose teardown calls :meth:`drop_all` in a ``finally`` — never
    from an inline delete at the end of a happy path, which is exactly the shape
    that leaked 985 collections into production.

    Construction requires an address a TEST-CONSENT variable NAMES: see
    :data:`TEST_CONSENT_URL_VARS` and :func:`operator_named_servers`. A caller that
    got its URL from a hardcoded fallback — or from the deployed application's own
    ``NE_API_QDRANT_URL`` — raises :class:`UnnamedQdrantServer` here rather than
    opening a socket onto whatever is listening.

    ⭐ AND NAMING IS CHECKED SECOND-CLASS TO THE ADDRESS. Even when a consent
    variable does hold the address, it is refused with
    :class:`DeploymentQdrantServer` if it is the deployment's own store — the
    round-4 bypass copied ``NE_API_QDRANT_URL`` into ``NE_QDRANT_URL`` and
    satisfied every naming check on the way in. See :func:`_deployment_refusal`.
    """

    def __init__(self, url: str) -> None:
        named = _named_or_none(url)
        if named is None:
            # A NAMED address that was refused anyway is the round-4 case, and it
            # deserves its own exception and its own message rather than being
            # reported as something nobody named.
            if (url or "").strip() in operator_named_servers():
                refusal = _deployment_refusal(url)
                if refusal is not None:
                    raise DeploymentQdrantServer(
                        f"refusing to own collections on {url!r}: {refusal} Consent by "
                        "naming is satisfied here and it is STILL not consent — the "
                        "address itself is production's."
                    )
            raise UnnamedQdrantServer(
                f"refusing to own collections on {url!r}: none of "
                f"{TEST_CONSENT_URL_VARS} holds that address. Name the server you "
                "consent to writing to; a default is how the live store acquired 985 "
                "orphaned collections, and the deployed app's own NE_API_QDRANT_URL "
                "is production configuration, not test consent."
            )
        self._url = named
        self._names: list[str] = []

    @property
    def url(self) -> str:
        return self._url

    @property
    def pending(self) -> tuple[str, ...]:
        """Names registered and not yet confirmed deleted. Empty means CLEAN."""
        return tuple(self._names)

    def client(self) -> AsyncQdrantClient:
        """A client for the owned server, which may only CREATE names this owner minted.

        The caller closes it, as always. See :class:`UnownedQdrantCollection` for
        why creation is guarded here rather than trusted to callers: every current
        live-server module names its collection through :meth:`collection`, and
        this is what keeps that true for one nobody has written yet.
        """
        return _OwnedClient(self, self._url)

    def owns(self, name: str) -> bool:
        """Did THIS owner mint ``name``? Registered-and-not-yet-dropped counts."""
        return name in self._names

    def collection(self, prefix: str) -> str:
        """Register and return a unique collection name under ``prefix``.

        ``prefix`` must be one of :data:`SWEEPABLE_COLLECTION_PREFIXES`, so every
        name this hands out is one the session sweep can also recover.
        """
        if prefix not in SWEEPABLE_COLLECTION_PREFIXES:
            raise ValueError(
                f"collection prefix {prefix!r} is not in SWEEPABLE_COLLECTION_PREFIXES "
                f"{SWEEPABLE_COLLECTION_PREFIXES}. Add it there in the same edit, so a "
                "worker killed mid-test leaves a name the sweep can still recognise."
            )
        name = f"{prefix}_{uuid.uuid4().hex}"
        self._names.append(name)
        return name

    def drop_all(self) -> None:
        """Delete every registered collection, and FORGET ONLY WHAT REALLY WENT.

        Teardown must not mask the test's own failure, so this never raises. But
        it must not lie either: the previous shape drained the registry *before*
        attempting anything and swallowed every error, so a transient timeout — or
        the very ``status: red`` store this module exists to prevent — left the
        collections behind, forgot their names, and reported success. That is the
        orphan-accumulation mechanism itself.

        So: a name is removed from the registry only when its delete SUCCEEDED (a
        404 counts — a name registered but never created is already gone; a
        ``False`` RETURN is a rejection and emphatically does not), and anything
        left is kept in :attr:`pending` and logged at WARNING with the server and
        the names. Calling twice retries exactly what is left.

        ⭐ AND SOMETHING ACTUALLY CALLS IT TWICE. Keeping a name in
        :attr:`pending` is only a retry if the owner outlives the fixture that
        made it, so an owner with survivors registers itself here and
        :func:`retry_pending_drops` — wired into both Qdrant conftests'
        ``pytest_sessionfinish`` — drains it at the end of the session. Without
        that, "kept for retry" meant "dropped on the floor with a log line".

        A fresh client is built inside this call's own loop: ``AsyncQdrantClient``
        is loop-affine (see ``test_qdrant_store_unit.py``) and the test's client
        belongs to a loop that has already closed.
        """
        names = list(self._names)
        if not names:
            return
        try:
            survivors = asyncio.run(self._delete_each(names))
        except Exception as exc:  # teardown never masks the test
            survivors = names
            logger.warning(
                "Qdrant teardown could not run against %s (%r); %d collection(s) "
                "remain registered and are STILL PRESENT on the server: %s",
                self._url,
                exc,
                len(survivors),
                ", ".join(survivors),
            )
        else:
            if survivors:
                logger.warning(
                    "Qdrant teardown failed to drop %d of %d collection(s) on %s; "
                    "they are LEAKED until dropped: %s",
                    len(survivors),
                    len(names),
                    self._url,
                    ", ".join(survivors),
                )
        self._names = survivors
        _remember_or_forget_leaks(self)

    async def _delete_each(self, names: list[str]) -> list[str]:
        """Delete each name; return the ones that are NOT confirmed gone."""
        # `check_compatibility=False`: this client only issues deletes, and the
        # version handshake is a second round trip that warns loudly when the
        # server is gone — which, in teardown after a failed test, it may be.
        client = AsyncQdrantClient(url=self._url, check_compatibility=False)
        survivors: list[str] = []
        try:
            for name in names:
                try:
                    accepted = await client.delete_collection(name)
                except Exception as exc:  # classified below, not swallowed
                    if not _means_already_absent(exc):
                        survivors.append(name)
                        logger.warning(
                            "Qdrant delete_collection(%r) on %s failed: %r",
                            name,
                            self._url,
                            exc,
                        )
                    continue
                # ⛔ A REJECTION IS NOT AN EXCEPTION. ``delete_collection`` is
                # annotated ``-> bool`` and RETURNS ``False`` when the server
                # declines the operation (a busy/red store declines rather than
                # raising). Ignoring that return is the same forget-what-still-
                # exists bug as swallowing the exception was, one seam over.
                if not accepted:
                    survivors.append(name)
                    logger.warning(
                        "Qdrant delete_collection(%r) on %s was REJECTED (returned "
                        "%r); the collection is STILL PRESENT.",
                        name,
                        self._url,
                        accepted,
                    )
        finally:
            with contextlib.suppress(Exception):
                await client.close()
        return survivors


class _OwnedClient(AsyncQdrantClient):
    """An ``AsyncQdrantClient`` that refuses to CREATE an unminted collection.

    ⛔ EVERY OTHER CALL PASSES STRAIGHT THROUGH. This is not a sandbox and does not
    pretend to be one: reads, upserts, searches and deletes are the vendor's, and a
    test that wants to drop something it does not own still can. The one thing it
    stops is the one thing that produces an ORPHAN — creating a collection under a
    name that neither :meth:`OwnedQdrantCollections.drop_all` nor
    :func:`sweep_named_server` will ever look for.

    ⚠️ What it does NOT close, because a client cannot: a test that builds its own
    ``AsyncQdrantClient`` against the same URL creates whatever it likes. That path
    is closed by the guardian's rule 1 instead
    (``test_no_test_builds_its_own_qdrant_server_client``, an EXACT set comparison
    against an EMPTY allowlist), which is a source rule and therefore only as wide
    as its globs.
    """

    def __init__(self, owner: OwnedQdrantCollections, url: str) -> None:
        # ``check_compatibility=False`` for the same reason ``_delete_each`` passes
        # it: the vendor's version handshake is an extra round trip issued FROM THE
        # CONSTRUCTOR, so merely asking the janitor for a client dialled the server.
        super().__init__(url=url, check_compatibility=False)
        self._ne_owner = owner

    def _assert_owned(self, collection_name: str, verb: str) -> None:
        if self._ne_owner.owns(collection_name):
            return
        raise UnownedQdrantCollection(
            f"refusing to {verb} Qdrant collection {collection_name!r} on "
            f"{self._ne_owner.url}: this process never minted it. Ask "
            "OwnedQdrantCollections.collection(<prefix>) for the name — a name it "
            "did not hand out is registered for no teardown and matches no sweep "
            "pattern, which is an orphaned collection by construction."
        )

    async def create_collection(
        self, collection_name: str, *args: object, **kwargs: object
    ) -> bool:
        self._assert_owned(collection_name, "create")
        return await super().create_collection(collection_name, *args, **kwargs)  # type: ignore[arg-type]

    async def recreate_collection(
        self, collection_name: str, *args: object, **kwargs: object
    ) -> bool:
        self._assert_owned(collection_name, "re-create")
        return await super().recreate_collection(collection_name, *args, **kwargs)  # type: ignore[arg-type]


#: Owners that finished :meth:`OwnedQdrantCollections.drop_all` with survivors.
#:
#: ⛔ PROCESS-LOCAL AND NAME-EXACT. It holds only names THIS process minted, so
#: draining it can never touch a sibling xdist worker's collections — unlike
#: :func:`sweep_named_server`, which is bulk and therefore controller-only.
_LEAKING_OWNERS: list[OwnedQdrantCollections] = []


def _remember_or_forget_leaks(owner: OwnedQdrantCollections) -> None:
    """Track ``owner`` for :func:`retry_pending_drops` iff it still has survivors."""
    present = any(known is owner for known in _LEAKING_OWNERS)
    if owner.pending and not present:
        _LEAKING_OWNERS.append(owner)
    elif not owner.pending and present:
        _LEAKING_OWNERS[:] = [known for known in _LEAKING_OWNERS if known is not owner]


def retry_pending_drops() -> tuple[str, ...]:
    """Re-attempt every delete that failed, and return what is STILL leaked.

    A fixture's ``finally`` gets exactly one attempt at a store that may have been
    briefly unreachable — or ``status: red``, which is the case this module exists
    for. One attempt against a store that is failing is indistinguishable from no
    teardown at all, so the owner is kept (see :data:`_LEAKING_OWNERS`) and drained
    once more at session end, when the pressure that made the first attempt fail
    has usually gone.

    Safe on ANY xdist process: it deletes named collections this process minted and
    nothing else. Returns the names it could still not drop, so a caller can say so
    rather than exiting green over a leak.
    """
    for owner in list(_LEAKING_OWNERS):
        owner.drop_all()
    return tuple(name for owner in _LEAKING_OWNERS for name in owner.pending)


def _means_already_absent(exc: BaseException) -> bool:
    """Is this failure "there was nothing to delete" rather than "it is still there"?

    Only a 404 / "not found" qualifies. A connection error emphatically does NOT:
    treating one as success is how a name gets forgotten while its collection
    lives on.
    """
    if getattr(exc, "status_code", None) == 404:
        return True
    text = str(exc).lower()
    return "not found" in text or "doesn't exist" in text or "does not exist" in text


def sweep_named_server() -> list[str]:
    """Drop leftover ``<test prefix>_<uuid4>`` collections. Returns what it dropped.

    ⛔ IT TAKES NO ADDRESS, ON PURPOSE. The servers are whatever
    :data:`TEST_CONSENT_URL_VARS` name, or nothing at all — the sweep can never be
    aimed at a store that was discovered, guessed or defaulted, because there is
    no parameter to aim. With neither variable set it returns ``[]`` having opened
    no socket, and a variable holding the DEPLOYMENT's own address is dropped from
    the list by :func:`consented_server_urls` — a bulk delete aimed at production
    is the worst of the doors to get wrong.

    ⭐ **EVERY consent-named server, not the first one.** Round 6: this read
    :func:`qdrant_server_url` — i.e. ``NE_QDRANT_URL`` alone — while ownership is
    granted by EITHER variable, and ``NE_TEST_QDRANT_URL`` is the one CI sets on
    the promoted source-authority step. A worker killed running that REQUIRED gate
    left ``authority_<uuid4>`` collections on a server this recovery never
    visited. See :func:`consented_server_urls`.

    ``finally``-based teardown cannot survive a killed xdist worker, a cancelled
    CI job or a ``SIGKILL``, and those paths are how orphans accumulate in silence
    between green runs. This is the recovery for them: it deletes only names
    matching :data:`SWEEPABLE_COLLECTION_PREFIXES` followed by a uuid4 hex, so a
    real collection (``ne_chunks``) cannot match no matter who calls it. That
    prefix set is the same one :meth:`OwnedQdrantCollections.collection` mints
    from and the same one it REFUSES to mint outside of, and
    :meth:`OwnedQdrantCollections.client` refuses to CREATE a collection this
    process did not mint — so a live-server collection whose name the sweep cannot
    recognise cannot be created through the janitor at all.

    ⚠️ Call it ONCE PER SESSION, from the xdist CONTROLLER (no ``workerinput``),
    at session FINISH. Called from a worker mid-session it would delete
    collections its siblings are still using — which is why it is wired from
    ``pytest_sessionfinish`` behind a ``workerinput`` check in both Qdrant
    conftests, and from nowhere else: the controller's ``sessionfinish`` runs
    after every worker has exited, so no sibling can still hold a collection.
    Per-name retry of THIS process's own failed deletes is a different job with a
    different blast radius — that is :func:`retry_pending_drops`, and it is safe on
    any worker.
    """
    dropped: list[str] = []
    for url in consented_server_urls():
        dropped.extend(_sweep_one_server(url))
    return dropped


def _sweep_one_server(url: str) -> list[str]:
    """:func:`sweep_named_server` against ONE already-consented, already-checked
    address. Never raises, for the reason stated below."""
    dropped: list[str] = []
    # ⛔ NEVER RAISES. This is wired into ``pytest_sessionfinish``; an exception
    # there is an INTERNALERROR that buries the run's real result, and an
    # unreachable server at session end is ordinary (the named store went away, the
    # CI service container is already stopping). Best effort, but loud.
    try:
        client = QdrantClient(url=url, timeout=30, check_compatibility=False)
    except Exception as exc:
        logger.warning("sweep could not build a client for %s: %r", url, exc)
        return dropped
    try:
        names = [c.name for c in client.get_collections().collections]
        for name in names:
            if not _SWEEPABLE_NAME_RE.fullmatch(name):
                continue
            try:
                accepted = client.delete_collection(name)
            except Exception as exc:  # a sweep is best effort, but loud
                if not _means_already_absent(exc):
                    logger.warning("sweep could not drop %r on %s: %r", name, url, exc)
                continue
            # Same rejection seam as ``_delete_each``: a ``False`` return means the
            # collection is still there, so reporting it dropped would be the
            # janitor telling the operator a leak was cleaned up when it was not.
            if not accepted:
                logger.warning(
                    "sweep's delete of %r on %s was REJECTED (returned %r); it is STILL PRESENT.",
                    name,
                    url,
                    accepted,
                )
                continue
            dropped.append(name)
    except Exception as exc:
        logger.warning(
            "sweep could not list collections on %s (%r); orphaned test collections "
            "may remain there.",
            url,
            exc,
        )
    finally:
        with contextlib.suppress(Exception):
            client.close()

    if dropped:
        logger.warning(
            "swept %d orphaned test collection(s) from %s: %s. Each one is teardown "
            "that did not run — a killed worker, a cancelled job, or a drop_all that "
            "logged a failure nobody read.",
            len(dropped),
            url,
            ", ".join(sorted(dropped)),
        )
    return dropped


# --------------------------------------------------------------------------- #
# The guardian's arming switch. ⛔ DO NOT MAKE THIS TOLERANT.
# --------------------------------------------------------------------------- #

#: The source-scanning guardian for everything above.
_GUARDIAN = "test_qdrant_live_store_guardian.py"

#: Identifiers that park a test. None of them may appear in the guardian.
_SKIP_IDENTIFIERS = frozenset({"skip", "skipif", "xfail", "importorskip", "SkipTest"})


#: Names by which a ``conftest.py`` can drop a file from collection entirely.
_COLLECT_IGNORES = ("collect_ignore", "collect_ignore_glob")


def _guardian_dir() -> pathlib.Path | None:
    """``packages/ne-brain/tests``, or ``None`` when it is not on disk at all."""
    tests = pathlib.Path(__file__).resolve().parents[4] / "tests"
    return tests if tests.is_dir() else None


def _guardian_path() -> pathlib.Path | None:
    """The guardian file, or ``None`` when the tests tree is not on disk at all.

    ``src/ne/brain/testing/qdrant_live.py`` -> ``packages/ne-brain/tests/...``.
    An installed wheel ships no ``tests`` directory, so the check no-ops there;
    a checkout that HAS one and is missing the guardian is a disarm.
    """
    tests = _guardian_dir()
    return tests / _GUARDIAN if tests is not None else None


def _assert_nothing_deselects_the_guardian(tests: pathlib.Path) -> None:
    """Refuse to load if a config file can drop the guardian from COLLECTION.

    ⛔ THE THIRD SPELLING OF "SWITCHED OFF", AND THE QUIETEST. A marker is
    visible, a module-level ``skip`` is caught by the AST walk below — and
    ``collect_ignore = ["test_qdrant_live_store_guardian.py"]`` in the sibling
    ``conftest.py`` takes the file from 12 tests to **zero** while satisfying both:
    the file is present and its AST is clean. The run then reports green with
    nothing watching it.

    There is no legitimate use of ``collect_ignore*`` anywhere in this repo, and a
    guardian cannot verify its own collection, so the mere PRESENCE of either name
    in a ``conftest.py`` on the path from the guardian's directory to the repo root
    is refused — as is the guardian's own filename appearing in a root pytest
    config, which is where a ``--ignore`` in ``addopts`` would live.

    ⚠️ Honest limit: a ``--ignore``/``--deselect`` typed on the COMMAND LINE is not
    checked and cannot be. This closes the checked-in spellings.
    """
    root = pathlib.Path(__file__).resolve().parents[6]
    directory = tests
    while True:
        conftest = directory / "conftest.py"
        if conftest.is_file():
            text = conftest.read_text(encoding="utf-8")
            for name in _COLLECT_IGNORES:
                if name in text:
                    raise RuntimeError(
                        f"{conftest} mentions {name!r}. That is how "
                        f"{_GUARDIAN} gets dropped from collection while still "
                        "passing every liveness check; this module does not load "
                        "beside it."
                    )
        if directory == root or directory == directory.parent:
            break
        directory = directory.parent

    stem = _GUARDIAN.removesuffix(".py")
    for config in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"):
        path = root / config
        if path.is_file() and stem in path.read_text(encoding="utf-8"):
            raise RuntimeError(
                f"{path} names {stem!r}. A pytest config has no business singling "
                "out the outage guardian; the only reason to is to stop it running."
            )


def _assert_guardian_is_armed() -> None:
    """Refuse to load if the outage guardian has been deleted or switched off.

    ⛔ THIS RUNS AT IMPORT, FROM THE ONE MODULE EVERY QDRANT TEST GOES THROUGH.

    A guardian cannot self-check that it is running: a module-level skip means its
    own assertions never execute, and the file reports ``skipped`` — green, silent,
    and total. The check therefore has to live somewhere the skip cannot reach, and
    ``ne.brain.testing.qdrant_live`` is that place: both ``conftest.py`` files that
    hand out Qdrant fixtures import it at collection time, so disabling the
    guardian takes down the collection of every package that could touch a Qdrant
    instead of quietly removing the only thing watching them.

    THREE spellings of "switched off" are refused here, because a review found the
    first version covered only one of them: the file is DELETED, the file PARKS
    (a marker or a module-level skip call — the AST walk), and the file is DROPPED
    FROM COLLECTION by a ``collect_ignore`` in a conftest or a config that names it
    (:func:`_assert_nothing_deselects_the_guardian`). The third took the guardian
    from 12 tests to zero while passing the other two checks.
    """
    tests = _guardian_dir()
    if tests is None:
        return
    _assert_nothing_deselects_the_guardian(tests)
    path = tests / _GUARDIAN
    if not path.is_file():
        raise RuntimeError(
            f"{_GUARDIAN} is missing from {path.parent}. It is the evidence floor for "
            "the 2026-09-10 live-vector-store outage; this module does not load "
            "without it."
        )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        found = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
        if found in _SKIP_IDENTIFIERS:
            raise RuntimeError(
                f"{_GUARDIAN} references {found!r} and would park instead of running. "
                "It guards the defect class that took the live vector store down on "
                "2026-09-10 and must run on every job; it is not skippable."
            )


_assert_guardian_is_armed()
