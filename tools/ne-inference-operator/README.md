# ne-inference-operator

Privileged, host-local implementation of ADR-0021. It exposes only bounded
HTTP/1.1 JSON on the four fixed `/v1/*` routes over an owner-controlled Unix
socket, authenticates the peer with Linux
`SO_PEERCRED`, and maps opaque target requests to a boot-validated registry and
a separate root-owned digest receipt. Neither document can select executable
paths, argv, environment, endpoints, or secrets: those are closed Python policy
for the two admitted adapter IDs.

Adapter files are opened with `O_NOFOLLOW`, checked for canonical path,
ownership, mode, link count, and SHA-256 on every run, then executed by
`/proc/self/fd` with fixed argv/environment, no shell, discarded output, a new
process group, and a total deadline. The state journal is owner-only, fsynced,
and hash chained. The root operator is restricted to `AF_UNIX` plus an
`IPAddressDeny=any`/`IPAddressAllow=localhost` IPv4 exception used only by the
exact CPU completion probe; it cannot dial a provider or another non-loopback
address. Vast operations cross a second stable UDS to a
fixed non-root UID through a closed target/operation/transition/deadline frame.
Both peers authenticate with `SO_PEERCRED`, the client carries one absolute
monotonic deadline through the handshake and operation, and neither wire can
carry provider output, an endpoint, a command, argv, or a credential.

The non-root broker independently digest-verifies the Vast CLI and invokes it
through an inherited FD. It runs inside the dedicated
`neutron-inference-provider` network namespace. That namespace has an nftables
default-drop output chain: activation resolves only `console.vast.ai`, binds
those addresses to fixed TLS port 443, installs a namespace/expiry-bound root
receipt, and supplies a fixed `/etc/hosts`; TLS still validates the exact
authority with the system CA. SSH is admitted only by a short-lived grant bound
to the fixed target, root-owned instance id, transition, global IP, port, and
expiry. The request reaches the narrow root nft helper on stdin, never argv or a
log. Missing, expired, malformed, mixed-namespace, or non-allowlisted evidence
fails closed. Missing or invalid `AIR_GAP` also fails closed, and it is reread
immediately before every provider runner and SSH grant. The CPU adapter remains
local.

The systemd unit defaults to `AIR_GAP=true`. Provider network and broker
services are demand-started only and both require the exact fail-closed
AIR_GAP condition; the network script repeats that check before DNS, namespace,
or nft setup. A host operator may explicitly set
`AIR_GAP=false` in the root-owned, provider-group-readable 0640
`/etc/neutron/inference-operator/environment` file when provider control is
authorized; a missing file remains fail-closed. The activation-owned provider
egress receipt expires after 24 hours. The checked-in refresh timer drains the
broker, takes the deployment writer lock, rebuilds the network evidence every
12 hours (plus at most five minutes of jitter), then preserves the stable
socket for later demand activation. Under `AIR_GAP=true` the same path performs
teardown without DNS or provider control. The teardown action removes the
namespace, nft surface, hosts projection, and receipt; rollback therefore fails
closed and cannot mutate the GPU.

GPU readiness loads only the fixed root-owned 0600
`/etc/neutron/secrets/vast-ssh-private-key` through a systemd credential. The
broker sees a private 0400 credential and uses `IdentitiesOnly=yes`. Host keys
persist in the non-root broker's 0600 state file
`/var/lib/neutron/inference-provider/vast-known-hosts`; neither service depends
on a home directory or SSH agent and both keep `ProtectHome=true`.
The bindable `/run/neutron/inference-operator` leaf contains only the UDS. The
systemd socket unit owns its stable inode across service restarts, so the API's
authenticated inode pin remains valid. Singleton locking and transient
credential staging stay outside that mount in the root-only operator state
directory, and the singleton is acquired before journal replay or recovery.

`deploy/inference-operator/install-policy.sh` installs only checked-in
scripts and policy. It refuses to download the Vast CLI and requires the exact
offline `vast-cli` v1.5.2 standalone artifact plus the five pure-Python wheels
admitted by `vastai-offline-lock.json`. Boot validates the code-owned lock,
official signed-tag provenance, complete wheelhouse, and every extracted import
file before admitting the CLI; arbitrary preinstalled executables, versions,
packages, extra files, and system/user site packages fail closed.
`write-cpu-receipt.py` publishes the final
admitted Compose file set, environment file, service semantic hash, separately
captured opaque running-container config label, pinned image and completion
target as a 0600 receipt. The CPU adapter pins the exact container ID around
inspection, refuses redirects, and uses only
`vllm-cpu`, `--no-deps`, `--pull never`, the shared live-upgrade lock, and a
bounded one-token completion. Its stop path never uses `down`, `rm`, `kill`, or
another service. The Vast operator mode exposes only status/start/stop; stop
retains disk, and no provision/destroy/use/SSH/serve/route action is reachable.
