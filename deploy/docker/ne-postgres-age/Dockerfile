# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# The two Chainguard manifests are immutable linux/amd64 members. The
# runtime digest was independently scanned with both required scanners before
# admission; unlike the current official Alpine and Debian PostgreSQL images,
# it contains no unresolved HIGH or CRITICAL finding.
FROM cgr.dev/chainguard/postgres:latest@sha256:4401cf2919e749cd77b634a72e4a677c1a1afc12baddb21faf28df326162472e AS runtime-base

# Optional pre-fetched Apache AGE source, overridden by --build-context.
FROM scratch AS age-src

FROM cgr.dev/chainguard/postgres:latest-dev@sha256:3f12033ccd330632fccfc85435fa7a3f5498132c40127bec07a612322605077f AS age-builder

ENV SOURCE_DATE_EPOCH=1769028835 TZ=UTC

COPY --from=runtime-base /lib/apk/db/installed /tmp/runtime-apk-installed
COPY deploy/docker/ne-postgres-age/builder-apk-packages.lock /tmp/builder-apk-packages.lock

RUN test "$(wc -l < /tmp/builder-apk-packages.lock)" -eq 112 \
    && echo '82c2eeaf1f041a91b326409f46b87f093ec40daf50a697746b71749aa78fd42b  /tmp/builder-apk-packages.lock' | sha256sum -c - \
    && xargs apk add --no-cache < /tmp/builder-apk-packages.lock \
    && awk 'BEGIN { RS=""; FS="\n" } { p=""; v=""; for (i=1; i<=NF; i++) { if ($i ~ /^P:/) p=substr($i,3); else if ($i ~ /^V:/) v=substr($i,3) } print p "=" v }' \
      /lib/apk/db/installed | LC_ALL=C sort > /tmp/builder-apk-packages.txt \
    && test "$(wc -l < /tmp/builder-apk-packages.txt)" -eq 112 \
    && cmp /tmp/builder-apk-packages.lock /tmp/builder-apk-packages.txt \
    && awk 'BEGIN { RS=""; FS="\n" } { p=""; v=""; c=""; for (i=1; i<=NF; i++) { if ($i ~ /^P:/) p=substr($i,3); else if ($i ~ /^V:/) v=substr($i,3); else if ($i ~ /^C:/) c=substr($i,3) } if (p == "" || v == "" || c == "") exit 42; print p "=" v "\t" c }' \
      /lib/apk/db/installed | LC_ALL=C sort > /tmp/builder-apk-content.txt \
    && test "$(wc -l < /tmp/builder-apk-content.txt)" -eq 112 \
    && echo '3a0d62a4bec82b2059400fa083d5f3e6ff6702bf111651bed8a719b1a7c13cb4  /tmp/builder-apk-content.txt' | sha256sum -c -

COPY deploy/docker/ne-postgres-age/fetch-age-src.sh /tmp/fetch-age-src.sh

# The `age-src` stage is empty unless the build passes
# `--build-context age-src=<dir>` with a pre-fetched copy (CI does, from
# actions/cache). Either way fetch-age-src.sh only keeps files matching the
# pinned SHA-512, and the checks below verify the digests and the signature.
RUN --mount=type=bind,from=age-src,target=/tmp/age-prefetched \
    sh /tmp/fetch-age-src.sh /tmp/age-download /tmp/age-prefetched \
    && cp /tmp/age-download/age-KEYS /tmp/age-KEYS \
    && cp /tmp/age-download/apache-age-1.7.0-src.tar.gz /tmp/apache-age.tar.gz \
    && cp /tmp/age-download/apache-age-1.7.0-src.tar.gz.asc /tmp/apache-age.tar.gz.asc \
    && echo "0a79d404d3d00d55d4dcf7b2e010310884acf194e57e37b67c3cb0d79678c5d2dc68ec482c522ed9946f4a3cd4335c936cd7176ead799b30e602741fb115e1f3  /tmp/age-KEYS" \
      | sha512sum -c - \
    && echo "129e64d70ec82e0fb47202145af45915b335048472c9a869129c31e4cb59f15a271a929a36a7abb2822272591e6476786c36908cb35a10cd73f2ecd0a617d3bb  /tmp/apache-age.tar.gz" \
      | sha512sum -c - \
    && echo "5bc1b1eb000ab85d2f9d567591f8a65a57ee503caff77b6b5a06a603c2e6c60e70420fab4c803ed23848f7fb0084016f03e0075fb4a9a43fb90be7d45c721e2a  /tmp/apache-age.tar.gz.asc" \
      | sha512sum -c - \
    && gpg --batch --dearmor < /tmp/age-KEYS > /tmp/age-keyring.gpg \
    && gpgv --status-fd=1 --keyring /tmp/age-keyring.gpg \
      /tmp/apache-age.tar.gz.asc /tmp/apache-age.tar.gz \
      > /tmp/age-signature.status 2>/tmp/age-signature.stderr \
    && grep -F "[GNUPG:] VALIDSIG 429306038E35AC054DBB4B5826B6CD9DCD5B0045 " \
      /tmp/age-signature.status \
    && mkdir -p /tmp/apache-age /age-artifacts/lib /age-artifacts/extension \
    && tar -xzf /tmp/apache-age.tar.gz -C /tmp/apache-age --strip-components=1

RUN make -C /tmp/apache-age PG_CONFIG=/usr/bin/pg_config with_llvm=no \
    && make -C /tmp/apache-age PG_CONFIG=/usr/bin/pg_config with_llvm=no install \
    && install -m0755 "$(/usr/bin/pg_config --pkglibdir)/age.so" /age-artifacts/lib/age.so \
    && cp -a "$(/usr/bin/pg_config --sharedir)/extension/age.control" \
      "$(/usr/bin/pg_config --sharedir)/extension/age--"*.sql /age-artifacts/extension/ \
    && test "$(sha512sum /tmp/apache-age.tar.gz | awk '{print $1}')" = \
      "129e64d70ec82e0fb47202145af45915b335048472c9a869129c31e4cb59f15a271a929a36a7abb2822272591e6476786c36908cb35a10cd73f2ecd0a617d3bb" \
    && awk 'BEGIN { RS=""; FS="\n" } { p=""; v=""; o=""; l=""; u=""; for (i=1; i<=NF; i++) { if ($i ~ /^P:/) p=substr($i,3); else if ($i ~ /^V:/) v=substr($i,3); else if ($i ~ /^o:/) o=substr($i,3); else if ($i ~ /^L:/) l=substr($i,3); else if ($i ~ /^U:/) u=substr($i,3) } if (u == "") u="https://packages.wolfi.dev/os"; print p "=" v "\t" o "\t" l "\t" u }' \
      /tmp/runtime-apk-installed | LC_ALL=C sort > /age-artifacts/runtime-apk-inventory.tsv \
    && test "$(wc -l < /age-artifacts/runtime-apk-inventory.tsv)" -eq 62 \
    && echo 'ffe2de953ce4575587c9a4083b3af4a7c6832af27248a2ca96ef0e3b8dd6949a  /age-artifacts/runtime-apk-inventory.tsv' | sha256sum -c -

FROM runtime-base AS runtime

LABEL org.opencontainers.image.title="Neutron pilot PostgreSQL with Apache AGE" \
      org.opencontainers.image.version="18.6-age-1.7.0" \
      org.opencontainers.image.base.name="cgr.dev/chainguard/postgres:latest" \
      org.opencontainers.image.base.digest="sha256:4401cf2919e749cd77b634a72e4a677c1a1afc12baddb21faf28df326162472e" \
      org.opencontainers.image.source="https://github.com/rjunee/neutron-enterprise"

COPY --from=age-builder /age-artifacts/lib/age.so /usr/lib/postgresql18/age.so
COPY --from=age-builder /age-artifacts/extension/ /usr/share/postgresql18/extension/
COPY --from=age-builder /age-artifacts/runtime-apk-inventory.tsv /usr/share/ne-postgres-age/runtime-apk-inventory.tsv
COPY deploy/docker/ne-postgres-age/THIRD_PARTY_LICENSES.txt /usr/share/ne-postgres-age/THIRD_PARTY_LICENSES.txt

RUN echo '5760b43b0a285c7bf98b9dba4a7bef3bfb35df9c545e564e8c56ed4790fc59da  /usr/share/ne-postgres-age/THIRD_PARTY_LICENSES.txt' | sha256sum -c -

USER 0
WORKDIR /home/postgres
ENTRYPOINT ["/usr/bin/docker-entrypoint.sh", "postgres"]
