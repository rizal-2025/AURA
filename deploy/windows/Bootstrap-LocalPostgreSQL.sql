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
\prompt -s 'Password for aura_migration_owner: ' aura_migration_password
SELECT format('CREATE ROLE aura_migration_owner LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'aura_migration_password') \gexec
\unset aura_migration_password
\endif

\if :is_test
\prompt -s 'Password for aura_test_runtime (ignored if role exists): ' aura_runtime_password
SELECT format('CREATE ROLE aura_test_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'aura_runtime_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aura_test_runtime') \gexec
SELECT 'CREATE DATABASE aura_test OWNER aura_migration_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'aura_test') \gexec
REVOKE ALL ON DATABASE aura_test FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE aura_test TO aura_test_runtime;
\connect aura_test
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO aura_test_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aura_test_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO aura_test_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aura_test_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO aura_test_runtime;
\unset aura_runtime_password
\elif :is_staging
\prompt -s 'Password for aura_staging_runtime (ignored if role exists): ' aura_runtime_password
SELECT format('CREATE ROLE aura_staging_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'aura_runtime_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aura_staging_runtime') \gexec
SELECT 'CREATE DATABASE aura_demo_staging OWNER aura_migration_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'aura_demo_staging') \gexec
REVOKE ALL ON DATABASE aura_demo_staging FROM PUBLIC;
GRANT CONNECT ON DATABASE aura_demo_staging TO aura_staging_runtime;
\connect aura_demo_staging
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO aura_staging_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aura_staging_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO aura_staging_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aura_staging_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO aura_staging_runtime;
\unset aura_runtime_password
\elif :is_production
\prompt -s 'Password for aura_public_runtime (ignored if role exists): ' aura_runtime_password
SELECT format('CREATE ROLE aura_public_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'aura_runtime_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aura_public_runtime') \gexec
SELECT 'CREATE DATABASE aura_demo_public OWNER aura_migration_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'aura_demo_public') \gexec
REVOKE ALL ON DATABASE aura_demo_public FROM PUBLIC;
GRANT CONNECT ON DATABASE aura_demo_public TO aura_public_runtime;
\connect aura_demo_public
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO aura_public_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aura_public_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO aura_public_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aura_public_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE aura_migration_owner IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO aura_public_runtime;
\unset aura_runtime_password
\else
\echo 'Invalid target. Use test, staging, or production.'
\quit 3
\endif

\echo 'AURA_LOCAL_POSTGRES_BOOTSTRAP_OK'
