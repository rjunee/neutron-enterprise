# console

TypeScript/React web console for Neutron Enterprise — agent chat with streaming
citations, brain search, and the approval/audit/eval/KPI ops surfaces.

Status: stub — implemented by NE-061 (foundation) and NE-062 (ops surfaces).

This app is **excluded from the uv workspace** (see `exclude = ["apps/console"]`
in the root `pyproject.toml`): it is a Node/TypeScript project, not a Python
package, so uv must not try to treat it as a workspace member.
