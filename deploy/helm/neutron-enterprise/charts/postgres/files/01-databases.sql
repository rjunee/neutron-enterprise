-- Copyright (c) 2026 Ryan Junee.
-- SPDX-License-Identifier: Apache-2.0
--
-- Runs once, at first container start, via the postgres image's
-- docker-entrypoint-initdb.d mechanism. `neutron` is POSTGRES_DB (created by
-- the image); this adds the auxiliary `keycloak` database. `ne` is a superuser
-- in this single-VM pilot image, so no explicit GRANT statements are required.
--
-- NOTE (C12): this file carries NO credentials — the `ne` role's password is
-- supplied at runtime via POSTGRES_PASSWORD (${NE_PILOT_POSTGRES_PASSWORD},
-- resolved from the bundle SecretStore), never here.

CREATE DATABASE keycloak;

-- NE-103c: DBOS owns its own Postgres schema (SPEC §4.1). Keeping it in a dedicated
-- `ne_dbos` database on the same instance keeps workflow/queue state out of the
-- platform schema's backup/migration story; DBOS runs its system-schema migrations
-- at launch(). Both ne-api (DBOSClient) and ne-worker (launch) target this database.
CREATE DATABASE ne_dbos;

-- NE-087 (O-4): OpenFGA owns its own schema in a dedicated `openfga` database on
-- the same single-VM Postgres instance (created only so the `rebac` profile's
-- one-shot `openfga migrate` has a target). Unused when the rebac profile is off.
CREATE DATABASE openfga;
