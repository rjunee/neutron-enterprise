{{/*
Copyright (c) 2026 Ryan Junee.
SPDX-License-Identifier: Apache-2.0

Named templates shared across the umbrella chart AND every subchart — Helm
gives every chart in the release ONE flat template namespace, so a subchart's
templates/deployment.yaml can call `include "neutron.podSecurityContext" .`
even though `_helpers.tpl` physically lives in the parent chart. This is what
makes hardening parity STRUCTURAL (design §1.5) rather than copy-pasted.
*/}}

{{/*
neutron.podSecurityContext — pod-level securityContext.
Params (dict): uid, gid, fsGroup (optional), openshift (bool)
runAsNonRoot is ALWAYS true. runAsUser/runAsGroup/fsGroup are omitted entirely
under global.openshift=true (restricted-SCC MustRunAsRange rejects a pinned
UID) — the seam is untested until NE-071b, but the template behavior is
implemented now so flipping the flag has a well-defined effect.
*/}}
{{- define "neutron.podSecurityContext" -}}
runAsNonRoot: true
{{- if not .openshift }}
runAsUser: {{ .uid }}
runAsGroup: {{ .gid }}
{{- if .fsGroup }}
fsGroup: {{ .fsGroup }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
neutron.containerSecurityContext — container-level securityContext.
Params (dict): readOnlyRootFilesystem (bool)
The §2.1(3a) sibling sweep (every container hardened): allowPrivilegeEscalation
false, capabilities.drop ALL, seccompProfile RuntimeDefault, always — only
readOnlyRootFilesystem varies per-service.
*/}}
{{- define "neutron.containerSecurityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
seccompProfile:
  type: RuntimeDefault
readOnlyRootFilesystem: {{ .readOnlyRootFilesystem }}
{{- end -}}

{{/*
neutron.imageRef — THIRD-PARTY image reference: optional global.imageRegistry
prefix + repository:tag@digest. Kubernetes resolves by digest so a retagged
registry copy cannot substitute content (C11).
Params (dict): registry, repository, tag, digest
*/}}
{{- define "neutron.imageRef" -}}
{{- if .registry -}}
{{ .registry }}/{{ .repository }}:{{ .tag }}@{{ .digest }}
{{- else -}}
{{ .repository }}:{{ .tag }}@{{ .digest }}
{{- end -}}
{{- end -}}

{{/*
neutron.firstPartyImageRef — the ONE first-party ne-app image (ne-api, and
post-103c ne-worker). `global.image.repository` is used VERBATIM, never
prefixed with global.imageRegistry: `ne release push` writes the
FULLY-QUALIFIED repository (including the customer registry host) into the
render-time overlay, so double-prefixing would corrupt the reference. Fails
closed (template-level backstop behind the values.schema.json digest
requirement) when global.image.digest is empty, naming `ne release push` as
the fix.
Params: the root context (.), so it can reach .Values.global.
*/}}
{{- define "neutron.firstPartyImageRef" -}}
{{- if not .Values.global.image.digest -}}
{{- fail "global.image.digest is empty — the first-party ne-app image is not pinned. Run `ne release push` to capture a registry digest, then re-render (C11)." -}}
{{- end -}}
{{ .Values.global.image.repository }}:{{ .Values.global.image.tag }}@{{ .Values.global.image.digest }}
{{- end -}}

{{/*
neutron.keycloakHost — the auth vhost (default auth.<hostname>), mirroring the
pilot Caddyfile's second vhost (design §1.9), host-based (no :8443 — the
Ingress controller is the sole TLS terminator). Reads global.ingress (not a
top-level `ingress` key) so this helper resolves identically whether called
from the umbrella chart's own templates or from a subchart's — Helm only
auto-propagates the `global:` key into every subchart's `.Values`.
Params: the root context (.)
*/}}
{{- define "neutron.keycloakHost" -}}
{{- .Values.global.ingress.keycloakHost | default (printf "auth.%s" .Values.global.hostname) -}}
{{- end -}}
