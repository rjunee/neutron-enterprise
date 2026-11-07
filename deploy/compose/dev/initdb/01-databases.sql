-- Copyright (c) 2026 Ryan Junee.
-- SPDX-License-Identifier: Apache-2.0
--
-- Runs once, at first container start, via the postgres image's
-- docker-entrypoint-initdb.d mechanism. Creates the additional databases
-- needed by other dev-stack services. `nedev` is a superuser in this dev
-- image, so no explicit GRANT statements are required.

CREATE DATABASE keycloak;
CREATE DATABASE langfuse;
CREATE DATABASE openfga;
