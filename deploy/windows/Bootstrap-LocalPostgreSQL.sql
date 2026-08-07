\set ON_ERROR_STOP on
\set ECHO none
\set VERBOSITY terse
\if :{?target}
\else
\echo 'Required: -v target=test|staging|production'
\quit 3
\endif

SELECT :'target' = 'test' AS is_test,
       :'target' = 'staging' AS is_staging,
       :'target' = 'production' AS is_production,
       :'target' IN ('test', 'staging', 'production') AS target_valid \gset
\if :target_valid
\else
\echo 'Invalid target. Use test, staging, or production.'
\quit 3
\endif
SELECT NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'aura_migration_owner'
) AS migration_owner_missing \gset

\if :migration_owner_missing
CREATE ROLE aura_migration_owner NOLOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\endif
SELECT rolpassword IS NULL AS migration_owner_password_missing
FROM pg_authid WHERE rolname = 'aura_migration_owner' \gset
\if :migration_owner_password_missing
\password aura_migration_owner
\endif
ALTER ROLE aura_migration_owner LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

\if :is_test
SELECT NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'aura_test_runner'
) AS test_runner_missing \gset
\if :test_runner_missing
CREATE ROLE aura_test_runner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\endif
SELECT rolpassword IS NULL AS test_runner_password_missing
FROM pg_authid WHERE rolname = 'aura_test_runner' \gset
\if :test_runner_password_missing
\password aura_test_runner
\endif
ALTER ROLE aura_test_runner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
SELECT 'CREATE DATABASE aura_test OWNER aura_migration_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'aura_test') \gexec
ALTER DATABASE aura_test OWNER TO aura_migration_owner;
REVOKE ALL ON DATABASE aura_test FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE aura_test TO aura_test_runner;
SELECT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'aura_test_runtime'
) AS obsolete_test_runtime_exists \gset
\if :obsolete_test_runtime_exists
REVOKE ALL ON DATABASE aura_test FROM aura_test_runtime;
\endif
\connect aura_test
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM aura_test_runner;
GRANT USAGE ON SCHEMA public TO aura_test_runner;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM aura_test_runner;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM aura_test_runner;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public REVOKE ALL ON TABLES FROM aura_test_runner;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public REVOKE ALL ON SEQUENCES FROM aura_test_runner;
\if :obsolete_test_runtime_exists
REVOKE ALL ON SCHEMA public FROM aura_test_runtime;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM aura_test_runtime;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM aura_test_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public REVOKE ALL ON TABLES FROM aura_test_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public REVOKE ALL ON SEQUENCES FROM aura_test_runtime;
\endif
\elif :is_staging
SELECT NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'aura_staging_runtime'
) AS staging_runtime_missing \gset
\if :staging_runtime_missing
CREATE ROLE aura_staging_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\endif
SELECT rolpassword IS NULL AS staging_runtime_password_missing
FROM pg_authid WHERE rolname = 'aura_staging_runtime' \gset
\if :staging_runtime_password_missing
\password aura_staging_runtime
\endif
ALTER ROLE aura_staging_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
SELECT 'CREATE DATABASE aura_demo_staging OWNER aura_migration_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'aura_demo_staging') \gexec
ALTER DATABASE aura_demo_staging OWNER TO aura_migration_owner;
REVOKE ALL ON DATABASE aura_demo_staging FROM PUBLIC;
GRANT CONNECT ON DATABASE aura_demo_staging TO aura_staging_runtime;
\connect aura_demo_staging
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO aura_staging_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aura_staging_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO aura_staging_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aura_staging_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO aura_staging_runtime;
\elif :is_production
SELECT NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'aura_public_runtime'
) AS public_runtime_missing \gset
\if :public_runtime_missing
CREATE ROLE aura_public_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\endif
SELECT rolpassword IS NULL AS public_runtime_password_missing
FROM pg_authid WHERE rolname = 'aura_public_runtime' \gset
\if :public_runtime_password_missing
\password aura_public_runtime
\endif
ALTER ROLE aura_public_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
SELECT 'CREATE DATABASE aura_demo_public OWNER aura_migration_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'aura_demo_public') \gexec
ALTER DATABASE aura_demo_public OWNER TO aura_migration_owner;
REVOKE ALL ON DATABASE aura_demo_public FROM PUBLIC;
GRANT CONNECT ON DATABASE aura_demo_public TO aura_public_runtime;
\connect aura_demo_public
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO aura_public_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aura_public_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO aura_public_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aura_public_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO aura_public_runtime;
\else
\echo 'Invalid target. Use test, staging, or production.'
\quit 3
\endif

\echo 'AURA_LOCAL_POSTGRES_BOOTSTRAP_OK'
