"""Focused offline tests for V2.0 G1A configuration hardening."""

from __future__ import annotations

import inspect
import os
from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.core.config import (
    build_ai_settings,
    build_application_settings,
    build_auth_settings,
    build_database_settings,
    build_demo_settings,
    build_environment_settings,
    clear_settings_cache,
    settings,
)
from app.core.config_validation import (
    CFG_AI_OLLAMA_INVALID,
    CFG_AI_OPENAI_INVALID,
    CFG_AI_PROVIDER_INVALID,
    CFG_AI_TIMEOUT_INVALID,
    CFG_AUTH_AUDIENCE_INVALID,
    CFG_AUTH_EXPIRY_INVALID,
    CFG_AUTH_ISSUER_INVALID,
    CFG_AUTH_SECRET_INVALID,
    CFG_DATABASE_INVALID,
    CFG_DEMO_BFF_SERVICE_TOKEN_INVALID,
    CFG_DEMO_DATABASE_NAME_INVALID,
    CFG_DEMO_DATABASE_REQUIRED,
    CFG_DEMO_DATABASE_SAME_TARGET,
    CFG_ENV_INVALID,
    CFG_TELEGRAM_DEMO_OWNER_FORBIDDEN,
    ConfigurationError,
)
from app.integrations.telegram.handlers import TelegramCustomerHandlers
from app.integrations.telegram.runner import (
    TelegramRunnerConfigurationError,
    TelegramRunnerSettings,
    validate_runner_configuration,
)
from app.core.security import create_customer_access_token
from app.services.ai.factory import get_ai_provider
from app.services.ai.ollama_provider import OllamaProvider
from app.services.ai.openai_provider import OpenAIProvider


VALID_JWT_SECRET = "g1a-jwt-secret-safe-random-material-2026"
VALID_TELEGRAM_SECRET = "g1a-telegram-identity-safe-random-material"
VALID_TELEGRAM_TOKEN = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
VALID_OPENAI_KEY = "g1a-openai-test-key-safe-material-123456"
VALID_DEMO_BFF_SERVICE_TOKEN = (
    "g1a-demo-bff-service-token-safe-material-2026"
)
MINIMUM_VALID_DEMO_BFF_SERVICE_TOKEN = (
    "a9K2mP7qR4tV8xY3zB6cD1fG5hJ0nL2s"
)
VALID_PRIMARY_DATABASE_URL = (
    "postgresql+psycopg://primary_user:primary_password@localhost:5432/aura"
)
VALID_DEMO_DATABASE_URL = (
    "postgresql+psycopg://demo_user:demo_password@localhost:5432/aura_demo"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def auth_settings(**overrides):
    values = {
        "APP_ENV": "test",
        "AUTH_JWT_SECRET": VALID_JWT_SECRET,
        "AUTH_JWT_ISSUER": "aura",
        "AUTH_JWT_AUDIENCE": "aura-api",
        "AUTH_JWT_EXPIRE_MINUTES": 60,
    }
    values.update(overrides)
    return build_auth_settings(_env_file=None, **values)


def runner_settings(**overrides):
    values = {
        "APP_ENV": "test",
        "TELEGRAM_BOT_TOKEN": VALID_TELEGRAM_TOKEN,
        "TELEGRAM_IDENTITY_SECRET": VALID_TELEGRAM_SECRET,
        "TELEGRAM_CLEAR_WEBHOOK_ON_START": False,
        "TELEGRAM_DROP_PENDING_UPDATES": False,
        "TELEGRAM_POLL_TIMEOUT_SECONDS": 30,
        "TELEGRAM_OWNER_NOTIFICATIONS_ENABLED": False,
        "TELEGRAM_OWNER_COMMANDS_ENABLED": False,
        "TELEGRAM_OWNER_CHAT_ID": None,
        "TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS": 5,
        "TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS": 5,
        "TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS": 10,
        "TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS": 60,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class AppEnvironmentConfigurationTests(unittest.TestCase):
    def test_all_exact_environment_values_are_accepted(self):
        for value in ("development", "test", "demo", "staging", "production"):
            with self.subTest(value=value):
                configured = build_environment_settings(
                    _env_file=None,
                    APP_ENV=value,
                )
                self.assertEqual(configured.APP_ENV, value)

    def test_missing_environment_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError) as raised:
                build_environment_settings(_env_file=None)
        self.assertEqual(str(raised.exception), CFG_ENV_INVALID)

    def test_malformed_environment_is_not_trimmed_or_case_folded(self):
        for value in ("Production", " production", "production ", "prod", ""):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ConfigurationError) as raised:
                    build_environment_settings(_env_file=None, APP_ENV=value)
                self.assertEqual(str(raised.exception), CFG_ENV_INVALID)
                if value:
                    self.assertNotIn(value, str(raised.exception))


class DemoDatabaseConfigurationTests(unittest.TestCase):
    @staticmethod
    def build(**overrides):
        values = {
            "APP_ENV": "demo",
            "DATABASE_URL": VALID_PRIMARY_DATABASE_URL,
            "DEMO_DATABASE_URL": VALID_DEMO_DATABASE_URL,
            "SQL_ECHO": False,
        }
        values.update(overrides)
        return build_database_settings(_env_file=None, **values)

    def test_demo_database_url_is_selected(self):
        configured = self.build()
        self.assertEqual(configured.APP_ENV, "demo")
        self.assertEqual(configured.DATABASE_URL, VALID_DEMO_DATABASE_URL)

    def test_demo_sql_echo_false_remains_false(self):
        configured = self.build(SQL_ECHO=False)
        self.assertIs(configured.SQL_ECHO, False)

    def test_demo_sql_echo_true_is_forced_false(self):
        configured = self.build(SQL_ECHO=True)
        self.assertIs(configured.SQL_ECHO, False)

    def test_demo_sql_echo_malformed_value_is_rejected(self):
        with self.assertRaises(ConfigurationError) as raised:
            self.build(SQL_ECHO="definitely-not-a-boolean")
        self.assertEqual(str(raised.exception), CFG_DATABASE_INVALID)

    def test_non_demo_sql_echo_behavior_is_preserved(self):
        for app_env in ("development", "test", "staging", "production"):
            with self.subTest(app_env=app_env):
                configured = build_database_settings(
                    _env_file=None,
                    APP_ENV=app_env,
                    DATABASE_URL="sqlite://",
                    SQL_ECHO=True,
                )
                self.assertIs(configured.SQL_ECHO, True)

    def test_demo_database_url_is_required(self):
        for value in (None, ""):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ConfigurationError) as raised:
                    self.build(DEMO_DATABASE_URL=value)
                self.assertEqual(
                    str(raised.exception),
                    CFG_DEMO_DATABASE_REQUIRED,
                )

    def test_demo_never_falls_back_to_primary_database_url(self):
        with self.assertRaises(ConfigurationError) as raised:
            build_database_settings(
                _env_file=None,
                APP_ENV="demo",
                DATABASE_URL=VALID_PRIMARY_DATABASE_URL,
                SQL_ECHO=False,
            )
        self.assertEqual(str(raised.exception), CFG_DEMO_DATABASE_REQUIRED)

    def test_same_database_target_is_rejected_even_with_different_credentials(self):
        cases = (
            VALID_DEMO_DATABASE_URL,
            (
                "postgresql+psycopg://other_user:other_password"
                "@localhost/aura_demo"
            ),
        )
        for primary_url in cases:
            with self.subTest(primary_url_kind=primary_url.split("@", 1)[-1]):
                with self.assertRaises(ConfigurationError) as raised:
                    self.build(DATABASE_URL=primary_url)
                self.assertEqual(
                    str(raised.exception),
                    CFG_DEMO_DATABASE_SAME_TARGET,
                )

    def test_localhost_and_ipv4_loopback_are_same_target(self):
        with self.assertRaises(ConfigurationError) as raised:
            self.build(
                DATABASE_URL=(
                    "postgresql://primary:secret"
                    "@127.0.0.1:5432/aura_demo"
                )
            )
        self.assertEqual(str(raised.exception), CFG_DEMO_DATABASE_SAME_TARGET)

    def test_localhost_and_compressed_ipv6_loopback_are_same_target(self):
        with self.assertRaises(ConfigurationError) as raised:
            self.build(
                DATABASE_URL=(
                    "postgresql://primary:secret@[::1]:5432/aura_demo"
                )
            )
        self.assertEqual(str(raised.exception), CFG_DEMO_DATABASE_SAME_TARGET)

    def test_localhost_and_expanded_ipv6_loopback_are_same_target(self):
        with self.assertRaises(ConfigurationError) as raised:
            self.build(
                DATABASE_URL=(
                    "postgresql://primary:secret"
                    "@[0:0:0:0:0:0:0:1]:5432/aura_demo"
                )
            )
        self.assertEqual(str(raised.exception), CFG_DEMO_DATABASE_SAME_TARGET)

    def test_implicit_and_default_postgresql_ports_are_same_target(self):
        with self.assertRaises(ConfigurationError) as raised:
            self.build(
                DATABASE_URL=(
                    "postgresql://primary:secret@localhost/aura_demo"
                )
            )
        self.assertEqual(str(raised.exception), CFG_DEMO_DATABASE_SAME_TARGET)

    def test_postgresql_drivers_are_same_backend_family(self):
        with self.assertRaises(ConfigurationError) as raised:
            self.build(
                DATABASE_URL=(
                    "postgres://primary:secret@localhost:5432/aura_demo"
                )
            )
        self.assertEqual(str(raised.exception), CFG_DEMO_DATABASE_SAME_TARGET)

    def test_hostname_comparison_is_case_insensitive(self):
        with self.assertRaises(ConfigurationError) as raised:
            self.build(
                DATABASE_URL=(
                    "postgresql://primary:secret@LOCALHOST:5432/aura_demo"
                )
            )
        self.assertEqual(str(raised.exception), CFG_DEMO_DATABASE_SAME_TARGET)

    def test_different_database_names_remain_different_targets(self):
        configured = self.build(
            DATABASE_URL=(
                "postgresql://primary:secret@localhost:5432/aura_demo_two"
            )
        )
        self.assertEqual(configured.DATABASE_URL, VALID_DEMO_DATABASE_URL)

    def test_different_non_loopback_hostnames_remain_different_targets(self):
        configured = self.build(
            DATABASE_URL=(
                "postgresql://primary:secret"
                "@db-one.internal:5432/aura_demo"
            ),
            DEMO_DATABASE_URL=(
                "postgresql://demo:secret"
                "@db-two.internal:5432/aura_demo"
            ),
        )
        self.assertEqual(
            configured.DATABASE_URL,
            "postgresql://demo:secret@db-two.internal:5432/aura_demo",
        )

    def test_different_non_default_ports_remain_different_targets(self):
        configured = self.build(
            DATABASE_URL=(
                "postgresql://primary:secret@localhost:5433/aura_demo"
            )
        )
        self.assertEqual(configured.DATABASE_URL, VALID_DEMO_DATABASE_URL)

    def test_malformed_demo_url_uses_secret_safe_error(self):
        password = "Malformed-Demo-Sentinel-Password-2026"
        url = (
            f"postgresql://demo:{password}"
            "@localhost:not-a-port/aura_demo"
        )
        with self.assertRaises(ConfigurationError) as raised:
            self.build(DEMO_DATABASE_URL=url)
        output = str(raised.exception) + repr(raised.exception)
        self.assertEqual(str(raised.exception), CFG_DATABASE_INVALID)
        self.assertNotIn(url, output)
        self.assertNotIn(password, output)

    def test_demo_database_name_must_contain_demo(self):
        for database_name in ("aura", "aura_production", "postgres"):
            with self.subTest(database_name=database_name):
                url = (
                    "postgresql+psycopg://demo_user:demo_password"
                    f"@localhost:5432/{database_name}"
                )
                with self.assertRaises(ConfigurationError) as raised:
                    self.build(
                        DATABASE_URL=(
                            "postgresql+psycopg://primary_user:primary_password"
                            "@localhost:5432/aura_primary"
                        ),
                        DEMO_DATABASE_URL=url,
                    )
                self.assertEqual(
                    str(raised.exception),
                    CFG_DEMO_DATABASE_NAME_INVALID,
                )

    def test_supported_demo_database_names_are_accepted(self):
        for database_name in ("aura_demo", "demo_aura", "aura-demo", "AURA_DEMO"):
            with self.subTest(database_name=database_name):
                url = (
                    "postgresql+psycopg://demo_user:demo_password"
                    f"@localhost:5432/{database_name}"
                )
                configured = self.build(DEMO_DATABASE_URL=url)
                self.assertEqual(configured.DATABASE_URL, url)

    def test_demo_database_must_be_postgresql(self):
        with self.assertRaises(ConfigurationError) as raised:
            self.build(DEMO_DATABASE_URL="sqlite:///aura_demo.db")
        self.assertEqual(str(raised.exception), CFG_DATABASE_INVALID)

    def test_demo_database_errors_do_not_disclose_url_or_password(self):
        password = "Demo-Sentinel-Password-2026"
        url = (
            f"postgresql+psycopg://demo_user:{password}"
            "@localhost:5432/aura_production"
        )
        with self.assertRaises(ConfigurationError) as raised:
            self.build(DEMO_DATABASE_URL=url)
        output = str(raised.exception) + repr(raised.exception)
        self.assertNotIn(url, output)
        self.assertNotIn(password, output)
        self.assertEqual(str(raised.exception), CFG_DEMO_DATABASE_NAME_INVALID)

    def test_non_demo_environments_keep_primary_database_behavior(self):
        for app_env in ("development", "test", "staging", "production"):
            with self.subTest(app_env=app_env):
                configured = build_database_settings(
                    _env_file=None,
                    APP_ENV=app_env,
                    DATABASE_URL="sqlite://",
                    DEMO_DATABASE_URL=VALID_DEMO_DATABASE_URL,
                    SQL_ECHO=False,
                )
                self.assertEqual(configured.DATABASE_URL, "sqlite://")


class DemoBFFServiceTokenConfigurationTests(unittest.TestCase):
    @staticmethod
    def build(**overrides):
        values = {
            "APP_ENV": "demo",
            "DEMO_BFF_SERVICE_TOKEN": VALID_DEMO_BFF_SERVICE_TOKEN,
        }
        values.update(overrides)
        return build_demo_settings(_env_file=None, **values)

    def test_demo_requires_service_token(self):
        with self.assertRaises(ConfigurationError) as raised:
            self.build(DEMO_BFF_SERVICE_TOKEN=None)
        self.assertEqual(
            str(raised.exception),
            CFG_DEMO_BFF_SERVICE_TOKEN_INVALID,
        )

    def test_valid_service_token_is_accepted_and_redacted(self):
        configured = self.build()
        self.assertEqual(
            configured.DEMO_BFF_SERVICE_TOKEN.get_secret_value(),
            VALID_DEMO_BFF_SERVICE_TOKEN,
        )
        self.assertNotIn(
            VALID_DEMO_BFF_SERVICE_TOKEN,
            str(configured) + repr(configured),
        )

    def test_exact_minimum_length_service_token_is_accepted(self):
        self.assertEqual(len(MINIMUM_VALID_DEMO_BFF_SERVICE_TOKEN), 32)
        configured = self.build(
            DEMO_BFF_SERVICE_TOKEN=MINIMUM_VALID_DEMO_BFF_SERVICE_TOKEN
        )
        self.assertEqual(
            configured.DEMO_BFF_SERVICE_TOKEN.get_secret_value(),
            MINIMUM_VALID_DEMO_BFF_SERVICE_TOKEN,
        )

    def test_short_service_token_is_rejected(self):
        with self.assertRaises(ConfigurationError) as raised:
            self.build(DEMO_BFF_SERVICE_TOKEN="short-service-token")
        self.assertEqual(
            str(raised.exception),
            CFG_DEMO_BFF_SERVICE_TOKEN_INVALID,
        )

    def test_whitespace_only_service_token_is_rejected(self):
        with self.assertRaises(ConfigurationError) as raised:
            self.build(DEMO_BFF_SERVICE_TOKEN=" " * 40)
        self.assertEqual(
            str(raised.exception),
            CFG_DEMO_BFF_SERVICE_TOKEN_INVALID,
        )

    def test_invalid_token_is_absent_from_exception_representations(self):
        supplied = " private-service-token-sentinel-material-2026 "
        with self.assertRaises(ConfigurationError) as raised:
            self.build(DEMO_BFF_SERVICE_TOKEN=supplied)
        output = str(raised.exception) + repr(raised.exception)
        self.assertNotIn(supplied, output)
        self.assertEqual(
            str(raised.exception),
            CFG_DEMO_BFF_SERVICE_TOKEN_INVALID,
        )

    def test_non_demo_does_not_require_service_token(self):
        for app_env in ("development", "test", "staging", "production"):
            with self.subTest(app_env=app_env):
                configured = build_demo_settings(
                    _env_file=None,
                    APP_ENV=app_env,
                )
                self.assertIsNone(configured.DEMO_BFF_SERVICE_TOKEN)

    def test_application_aggregate_accepts_complete_demo_configuration(self):
        configured = build_application_settings(
            _env_file=None,
            APP_ENV="demo",
            DATABASE_URL=VALID_PRIMARY_DATABASE_URL,
            DEMO_DATABASE_URL=VALID_DEMO_DATABASE_URL,
            DEMO_BFF_SERVICE_TOKEN=VALID_DEMO_BFF_SERVICE_TOKEN,
            AUTH_JWT_SECRET=VALID_JWT_SECRET,
            AUTH_JWT_ISSUER="aura-demo",
            AUTH_JWT_AUDIENCE="aura-demo-api",
            AUTH_JWT_EXPIRE_MINUTES=60,
            AI_PROVIDER="ollama",
            OLLAMA_BASE_URL="http://localhost:11434/v1",
            OLLAMA_MODEL="test-model",
        )
        self.assertEqual(configured.APP_ENV, "demo")
        self.assertNotIn(
            VALID_DEMO_BFF_SERVICE_TOKEN,
            str(configured) + repr(configured),
        )


class JwtConfigurationTests(unittest.TestCase):
    def test_valid_auth_configuration_is_accepted(self):
        configured = auth_settings()
        self.assertEqual(configured.AUTH_JWT_SECRET, VALID_JWT_SECRET)
        self.assertEqual(configured.AUTH_JWT_EXPIRE_MINUTES, 60)

    def test_invalid_secret_forms_are_rejected_without_value_disclosure(self):
        values = (
            "short",
            "z" * 513,
            " " + VALID_JWT_SECRET,
            VALID_JWT_SECRET + " ",
            " " * 32,
            "safe-secret-material-1234567890\nxx",
            "REPLACE_WITH_A_RANDOM_SECRET_OF_AT_LEAST_32_CHARACTERS",
            "CHANGE_ME_CHANGE_ME_CHANGE_ME_CHANGE_ME",
            "abcd" * 8,
        )
        for value in values:
            with self.subTest(kind=repr(value[:12])):
                with self.assertRaises(ConfigurationError) as raised:
                    auth_settings(AUTH_JWT_SECRET=value)
                self.assertEqual(str(raised.exception), CFG_AUTH_SECRET_INVALID)
                self.assertNotIn(value, str(raised.exception))

    def test_issuer_and_audience_bounds_are_strict(self):
        cases = (
            ("AUTH_JWT_ISSUER", None, CFG_AUTH_ISSUER_INVALID),
            ("AUTH_JWT_ISSUER", " aura", CFG_AUTH_ISSUER_INVALID),
            ("AUTH_JWT_ISSUER", "aura\nissuer", CFG_AUTH_ISSUER_INVALID),
            ("AUTH_JWT_ISSUER", "i" * 129, CFG_AUTH_ISSUER_INVALID),
            ("AUTH_JWT_AUDIENCE", None, CFG_AUTH_AUDIENCE_INVALID),
            ("AUTH_JWT_AUDIENCE", "aura-api ", CFG_AUTH_AUDIENCE_INVALID),
            ("AUTH_JWT_AUDIENCE", "aura\tapi", CFG_AUTH_AUDIENCE_INVALID),
            ("AUTH_JWT_AUDIENCE", "a" * 129, CFG_AUTH_AUDIENCE_INVALID),
        )
        for field, value, code in cases:
            with self.subTest(field=field, kind=repr(value)):
                with self.assertRaises(ConfigurationError) as raised:
                    auth_settings(**{field: value})
                self.assertEqual(str(raised.exception), code)
                if isinstance(value, str) and value:
                    self.assertNotIn(value, str(raised.exception))

    def test_deployed_environments_reject_development_identity_defaults(self):
        for app_env in ("demo", "staging", "production"):
            with self.subTest(app_env=app_env):
                with self.assertRaises(ConfigurationError) as raised:
                    auth_settings(APP_ENV=app_env)
                self.assertEqual(str(raised.exception), CFG_AUTH_ISSUER_INVALID)

    def test_deployed_environment_accepts_explicit_identity_labels(self):
        configured = auth_settings(
            APP_ENV="production",
            AUTH_JWT_ISSUER="aura.example.production",
            AUTH_JWT_AUDIENCE="aura.example.api",
        )
        self.assertEqual(configured.APP_ENV, "production")

    def test_expiry_is_a_strict_bounded_integer(self):
        for value in (True, False, 1.0, "1.0", 0, "0", -1, "-1", 1441, " 60"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ConfigurationError) as raised:
                    auth_settings(AUTH_JWT_EXPIRE_MINUTES=value)
                self.assertEqual(str(raised.exception), CFG_AUTH_EXPIRY_INVALID)
        self.assertEqual(
            auth_settings(AUTH_JWT_EXPIRE_MINUTES="1440").AUTH_JWT_EXPIRE_MINUTES,
            1440,
        )

    def test_auth_settings_and_lazy_facade_are_immutable(self):
        configured = auth_settings()
        for field, value in (
            ("AUTH_JWT_SECRET", "different-safe-secret-material-123456789"),
            ("AUTH_JWT_ISSUER", "different-issuer"),
            ("AUTH_JWT_AUDIENCE", "different-audience"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    setattr(configured, field, value)
                self.assertNotEqual(getattr(configured, field), value)

        with self.assertRaises(TypeError):
            settings.AUTH_JWT_SECRET = "raw-bypass-secret-material-123456789"

    def test_runtime_jwt_secret_reuses_complete_validator(self):
        invalid_values = (
            "prefix-change_me-suffix-safe-material-12345",
            " runtime-safe-secret-material-123456789",
            "Ab3!xY7?z" * 4,
            "safe-secret-material-1234\u200b567890",
            "x" * 513,
        )
        for secret in invalid_values:
            configured = SimpleNamespace(
                APP_ENV="test",
                AUTH_JWT_SECRET=secret,
                AUTH_JWT_ISSUER="aura",
                AUTH_JWT_AUDIENCE="aura-api",
                AUTH_JWT_EXPIRE_MINUTES=60,
            )
            with (
                self.subTest(kind=secret[:8]),
                patch("app.core.security.get_auth_settings", return_value=configured),
                self.assertRaises(RuntimeError) as raised,
            ):
                create_customer_access_token(
                    __import__("uuid").uuid4(),
                    1,
                )
            self.assertNotIn(secret, str(raised.exception))

    def test_secret_and_aggregate_representations_are_redacted(self):
        jwt_secret = "jwt-S3nt1nel-8fH2kL9mQ7vB4xD6pR1s"
        api_key = "api-S3nt1nel-7gK4mN8qW2yT6vC9"
        database_url = "postgresql://user:Db-S3nt1nel-Pass@localhost/aura"
        configured = build_application_settings(
            _env_file=None,
            APP_ENV="test",
            DATABASE_URL=database_url,
            AUTH_JWT_SECRET=jwt_secret,
            AUTH_JWT_ISSUER="aura",
            AUTH_JWT_AUDIENCE="aura-api",
            AUTH_JWT_EXPIRE_MINUTES=60,
            AI_PROVIDER="openai",
            OPENAI_API_KEY=api_key,
            OPENAI_MODEL="gpt-test",
        )

        representations = (
            repr(configured.auth),
            str(configured.auth),
            repr(configured.ai),
            str(configured.ai),
            repr(configured.database),
            str(configured.database),
            repr(configured),
            str(configured),
        )
        for output in representations:
            self.assertNotIn(jwt_secret, output)
            self.assertNotIn(api_key, output)
            self.assertNotIn(database_url, output)
            self.assertNotIn("Db-S3nt1nel-Pass", output)

    def test_configuration_exception_never_contains_supplied_secret(self):
        sentinel = "S3nt1nel-invalid-change_me-secret-material"
        with self.assertRaises(ConfigurationError) as raised:
            auth_settings(AUTH_JWT_SECRET=sentinel)
        self.assertNotIn(sentinel, str(raised.exception))
        self.assertNotIn(sentinel, repr(raised.exception))


class TelegramRunnerConfigurationTests(unittest.TestCase):
    def test_bot_token_validation_is_exact_and_secret_safe(self):
        invalid_values = (
            None,
            " " + VALID_TELEGRAM_TOKEN,
            VALID_TELEGRAM_TOKEN + " ",
            VALID_TELEGRAM_TOKEN + "\n",
            "REPLACE_WITH_TELEGRAM_BOT_TOKEN",
            "not-a-token",
            "1234:abcdefghijklmnopqrstuvwxyzABCDE",
            "1" * 257,
        )
        for value in invalid_values:
            with self.subTest(kind=repr(value)):
                with self.assertRaises(TelegramRunnerConfigurationError) as raised:
                    validate_runner_configuration(
                        runner_settings(TELEGRAM_BOT_TOKEN=value)
                    )
                self.assertEqual(str(raised.exception), "CFG_TELEGRAM_TOKEN_INVALID")
                if isinstance(value, str) and value:
                    self.assertNotIn(value, str(raised.exception))

    def test_identity_secret_uses_strong_secret_policy(self):
        for value in (
            "short",
            " " + VALID_TELEGRAM_SECRET,
            VALID_TELEGRAM_SECRET + "\0",
            "REPLACE_WITH_RANDOM_TELEGRAM_IDENTITY_SECRET",
            "telegram" * 4,
            "x" * 513,
        ):
            with self.subTest(kind=repr(value[:12])):
                with self.assertRaises(TelegramRunnerConfigurationError) as raised:
                    validate_runner_configuration(
                        runner_settings(TELEGRAM_IDENTITY_SECRET=value)
                    )
                self.assertEqual(
                    str(raised.exception),
                    "CFG_TELEGRAM_IDENTITY_INVALID",
                )
                self.assertNotIn(value, str(raised.exception))

    def test_owner_id_is_conditionally_required_for_independent_flags(self):
        disabled = validate_runner_configuration(runner_settings())
        self.assertIsNone(disabled.owner_chat_id)
        for flag in (
            "TELEGRAM_OWNER_NOTIFICATIONS_ENABLED",
            "TELEGRAM_OWNER_COMMANDS_ENABLED",
        ):
            with self.subTest(flag=flag):
                with self.assertRaises(TelegramRunnerConfigurationError) as raised:
                    validate_runner_configuration(
                        runner_settings(**{flag: True})
                    )
                self.assertEqual(str(raised.exception), "CFG_TELEGRAM_OWNER_INVALID")
                configured = validate_runner_configuration(
                    runner_settings(
                        **{flag: "true"},
                        TELEGRAM_OWNER_CHAT_ID="987654",
                    )
                )
                self.assertEqual(configured.owner_chat_id, 987654)

    def test_demo_rejects_owner_notifications_and_commands(self):
        disabled = validate_runner_configuration(
            runner_settings(APP_ENV="demo")
        )
        self.assertFalse(disabled.owner_notifications_enabled)
        self.assertFalse(disabled.owner_commands_enabled)

        for flag in (
            "TELEGRAM_OWNER_NOTIFICATIONS_ENABLED",
            "TELEGRAM_OWNER_COMMANDS_ENABLED",
        ):
            with self.subTest(flag=flag):
                with self.assertRaises(
                    TelegramRunnerConfigurationError
                ) as raised:
                    validate_runner_configuration(
                        runner_settings(
                            APP_ENV="demo",
                            TELEGRAM_OWNER_CHAT_ID="987654",
                            **{flag: True},
                        )
                    )
                self.assertEqual(
                    str(raised.exception),
                    CFG_TELEGRAM_DEMO_OWNER_FORBIDDEN,
                )

    def test_flags_and_numeric_options_are_strict(self):
        for value in (" true ", "TRUE", 1, None):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TelegramRunnerConfigurationError):
                    validate_runner_configuration(
                        runner_settings(TELEGRAM_CLEAR_WEBHOOK_ON_START=value)
                    )
        for value in (True, 0, -1, 1.0, "1.0", "030", 61):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TelegramRunnerConfigurationError):
                    validate_runner_configuration(
                        runner_settings(TELEGRAM_POLL_TIMEOUT_SECONDS=value)
                    )

    def test_runner_environment_is_required_and_not_normalized(self):
        for value in (None, "Test", " test", "test "):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TelegramRunnerConfigurationError) as raised:
                    validate_runner_configuration(runner_settings(APP_ENV=value))
                self.assertEqual(str(raised.exception), CFG_ENV_INVALID)

    def test_handler_has_no_global_identity_secret_fallback(self):
        parameter = inspect.signature(TelegramCustomerHandlers).parameters[
            "identity_secret"
        ]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            TelegramCustomerHandlers()

    def test_error_does_not_disclose_token_secret_or_owner_id(self):
        owner_id = "987654321012345"
        token = "999999999:private-token-material-private"
        secret = "private-telegram-identity-material-12345"
        with self.assertRaises(TelegramRunnerConfigurationError) as raised:
            validate_runner_configuration(
                runner_settings(
                    TELEGRAM_BOT_TOKEN=token + " ",
                    TELEGRAM_IDENTITY_SECRET=secret,
                    TELEGRAM_OWNER_NOTIFICATIONS_ENABLED=True,
                    TELEGRAM_OWNER_CHAT_ID=owner_id,
                )
            )
        output = str(raised.exception)
        self.assertNotIn(token, output)
        self.assertNotIn(secret, output)
        self.assertNotIn(owner_id, output)

    def test_runner_configuration_is_immutable_and_secret_repr_is_redacted(self):
        configured = validate_runner_configuration(runner_settings())
        for field, value in (
            ("bot_token", "999999999:OtherSafeTokenMaterial12345"),
            ("identity_secret", "different-identity-secret-material-123456"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(FrozenInstanceError):
                    setattr(configured, field, value)

        output = repr(configured) + str(configured)
        self.assertNotIn(VALID_TELEGRAM_TOKEN, output)
        self.assertNotIn(VALID_TELEGRAM_SECRET, output)

        raw = TelegramRunnerSettings(
            _env_file=None,
            **vars(runner_settings()),
        )
        with self.assertRaises(ValidationError):
            raw.TELEGRAM_BOT_TOKEN = "999999999:OtherSafeTokenMaterial12345"
        raw_output = repr(raw) + str(raw)
        self.assertNotIn(VALID_TELEGRAM_TOKEN, raw_output)
        self.assertNotIn(VALID_TELEGRAM_SECRET, raw_output)

    def test_realistic_token_and_exact_component_boundaries(self):
        accepted = (
            "12345:" + "A" * 20,
            "1234567890:AAE9_Real-BotFatherStyleToken123456",
            "9" * 20 + ":" + "Z" * 128,
        )
        for token in accepted:
            with self.subTest(length=len(token)):
                configured = validate_runner_configuration(
                    runner_settings(TELEGRAM_BOT_TOKEN=token)
                )
                self.assertEqual(configured.bot_token, token)

        rejected = (
            "1234:" + "A" * 20,
            "9" * 21 + ":" + "A" * 20,
            "12345:" + "A" * 19,
            "12345:" + "A" * 129,
        )
        for token in rejected:
            with self.subTest(length=len(token)):
                with self.assertRaises(TelegramRunnerConfigurationError):
                    validate_runner_configuration(
                        runner_settings(TELEGRAM_BOT_TOKEN=token)
                    )


class AIConfigurationTests(unittest.TestCase):
    def test_provider_timeout_is_bounded(self):
        for invalid in (0, 31, "01", "1.5", True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ConfigurationError) as raised:
                    build_ai_settings(
                        _env_file=None,
                        APP_ENV="test",
                        AI_PROVIDER="openai",
                        OPENAI_API_KEY=VALID_OPENAI_KEY,
                        OPENAI_MODEL="test-model",
                        AI_PROVIDER_TIMEOUT_SECONDS=invalid,
                    )
                self.assertEqual(str(raised.exception), CFG_AI_TIMEOUT_INVALID)

    def test_exact_provider_names_are_accepted(self):
        ollama = build_ai_settings(
            _env_file=None,
            APP_ENV="test",
            AI_PROVIDER="ollama",
            OLLAMA_BASE_URL="http://localhost:11434/v1",
            OLLAMA_MODEL="test-model",
        )
        openai = build_ai_settings(
            _env_file=None,
            APP_ENV="test",
            AI_PROVIDER="openai",
            OPENAI_API_KEY=VALID_OPENAI_KEY,
            OPENAI_MODEL="test-model",
        )
        self.assertEqual(ollama.AI_PROVIDER, "ollama")
        self.assertEqual(openai.AI_PROVIDER, "openai")

    def test_unknown_or_normalized_provider_names_are_rejected(self):
        for value in (None, "OpenAI", "OLLAMA", " openai", "openai ", "unknown"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ConfigurationError) as raised:
                    build_ai_settings(
                        _env_file=None,
                        APP_ENV="test",
                        AI_PROVIDER=value,
                    )
                self.assertEqual(str(raised.exception), CFG_AI_PROVIDER_INVALID)

    def test_openai_requires_nonplaceholder_key_and_valid_model(self):
        for value in (None, "", " ", "REPLACE_WITH_OPENAI_API_KEY_IF_USED", "dummy-key"):
            with self.subTest(key=repr(value)):
                with self.assertRaises(ConfigurationError) as raised:
                    build_ai_settings(
                        _env_file=None,
                        APP_ENV="test",
                        AI_PROVIDER="openai",
                        OPENAI_API_KEY=value,
                        OPENAI_MODEL="test-model",
                    )
                self.assertEqual(str(raised.exception), CFG_AI_OPENAI_INVALID)
                if isinstance(value, str) and value:
                    self.assertNotIn(value, str(raised.exception))
        for model in ("", " model", "model\n", "m" * 129):
            with self.subTest(model=repr(model)):
                with self.assertRaises(ConfigurationError):
                    build_ai_settings(
                        _env_file=None,
                        APP_ENV="test",
                        AI_PROVIDER="openai",
                        OPENAI_API_KEY=VALID_OPENAI_KEY,
                        OPENAI_MODEL=model,
                    )

    def test_ollama_url_structure_is_strict(self):
        invalid = (
            None,
            "ftp://localhost/model",
            "http:///missing-host",
            "http://user:password@localhost:11434/v1",
            "http://localhost:11434/v1?secret=value",
            "http://localhost:11434/v1#fragment",
            " http://localhost:11434/v1",
        )
        for value in invalid:
            with self.subTest(url=value):
                with self.assertRaises(ConfigurationError) as raised:
                    build_ai_settings(
                        _env_file=None,
                        APP_ENV="test",
                        AI_PROVIDER="ollama",
                        OLLAMA_BASE_URL=value,
                    )
                self.assertEqual(str(raised.exception), CFG_AI_OLLAMA_INVALID)
                if isinstance(value, str) and value:
                    self.assertNotIn(value, str(raised.exception))

        for model in (None, "", " model", "model\n", "m" * 129):
            with self.subTest(model=repr(model)):
                with self.assertRaises(ConfigurationError) as raised:
                    build_ai_settings(
                        _env_file=None,
                        APP_ENV="test",
                        AI_PROVIDER="ollama",
                        OLLAMA_BASE_URL="http://localhost:11434/v1",
                        OLLAMA_MODEL=model,
                    )
                self.assertEqual(str(raised.exception), CFG_AI_OLLAMA_INVALID)

    def test_deployed_remote_http_is_rejected_but_loopback_is_allowed(self):
        for app_env in ("staging", "production"):
            with self.subTest(app_env=app_env):
                with self.assertRaises(ConfigurationError):
                    build_ai_settings(
                        _env_file=None,
                        APP_ENV=app_env,
                        AI_PROVIDER="ollama",
                        OLLAMA_BASE_URL="http://ollama.internal:11434/v1",
                        OLLAMA_MODEL="test-model",
                    )
                configured = build_ai_settings(
                    _env_file=None,
                    APP_ENV=app_env,
                    AI_PROVIDER="ollama",
                    OLLAMA_BASE_URL="http://127.0.0.1:11434/v1",
                    OLLAMA_MODEL="test-model",
                )
                self.assertEqual(configured.AI_PROVIDER, "ollama")

        configured_ipv6 = build_ai_settings(
            _env_file=None,
            APP_ENV="production",
            AI_PROVIDER="ollama",
            OLLAMA_BASE_URL="http://[::1]:11434/v1",
            OLLAMA_MODEL="test-model",
        )
        self.assertEqual(configured_ipv6.OLLAMA_BASE_URL, "http://[::1]:11434/v1")

    def test_provider_construction_is_offline_and_has_no_fallback(self):
        ollama_config = build_ai_settings(
            _env_file=None,
            APP_ENV="test",
            AI_PROVIDER="ollama",
            OLLAMA_BASE_URL="http://localhost:11434/v1",
            OLLAMA_MODEL="test-model",
        )
        openai_config = build_ai_settings(
            _env_file=None,
            APP_ENV="test",
            AI_PROVIDER="openai",
            OPENAI_API_KEY=VALID_OPENAI_KEY,
            OPENAI_MODEL="test-model",
        )
        with patch("app.services.ai.ollama_provider.AsyncOpenAI") as client:
            self.assertIsInstance(get_ai_provider(ollama_config), OllamaProvider)
            client.assert_called_once_with(
                base_url="http://localhost:11434/v1",
                api_key="ollama",
                timeout=20,
                max_retries=0,
            )
        with patch("app.services.ai.openai_provider.AsyncOpenAI") as client:
            self.assertIsInstance(get_ai_provider(openai_config), OpenAIProvider)
            client.assert_called_once_with(
                api_key=VALID_OPENAI_KEY,
                timeout=20,
                max_retries=0,
            )
        with self.assertRaises(ConfigurationError) as raised:
            get_ai_provider(SimpleNamespace(APP_ENV="test", AI_PROVIDER="other"))
        self.assertEqual(str(raised.exception), CFG_AI_PROVIDER_INVALID)

    def test_ai_settings_are_immutable_and_api_key_repr_is_redacted(self):
        configured = build_ai_settings(
            _env_file=None,
            APP_ENV="test",
            AI_PROVIDER="openai",
            OPENAI_API_KEY=VALID_OPENAI_KEY,
            OPENAI_MODEL="test-model",
        )
        with self.assertRaises(ValidationError):
            configured.OPENAI_API_KEY = "different-openai-safe-key-material-12345"
        output = repr(configured) + str(configured)
        self.assertNotIn(VALID_OPENAI_KEY, output)


class ConfigurationStringHardeningTests(unittest.TestCase):
    def test_unicode_control_and_format_characters_are_rejected(self):
        secret_cases = (
            "safe-secret-material-1234567890\u0085xx",
            "safe-secret-material-1234567890\u200bxx",
            "safe-secret-material-1234567890\u202exx",
        )
        for value in secret_cases:
            with self.subTest(codepoint=hex(ord(value[-3]))):
                with self.assertRaises(ConfigurationError) as raised:
                    auth_settings(AUTH_JWT_SECRET=value)
                self.assertEqual(str(raised.exception), CFG_AUTH_SECRET_INVALID)

        for field, value, code in (
            ("AUTH_JWT_ISSUER", "aura\u200bissuer", CFG_AUTH_ISSUER_INVALID),
            ("AUTH_JWT_AUDIENCE", "aura\u202eaudience", CFG_AUTH_AUDIENCE_INVALID),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ConfigurationError) as raised:
                    auth_settings(**{field: value})
                self.assertEqual(str(raised.exception), code)

    def test_ordinary_unicode_label_is_accepted(self):
        configured = auth_settings(
            AUTH_JWT_ISSUER="aura-produksi-Indonesia-é",
            AUTH_JWT_AUDIENCE="pelanggan-AURA-日本",
        )
        self.assertEqual(configured.AUTH_JWT_ISSUER, "aura-produksi-Indonesia-é")
        self.assertEqual(configured.AUTH_JWT_AUDIENCE, "pelanggan-AURA-日本")

    def test_embedded_placeholders_and_long_repetition_are_rejected(self):
        values = (
            "prefix-CHANGE_ME-suffix-safe-material-12345",
            "prefix-your_secret-suffix-safe-material-1234",
            "prefix-ChAnGeMe-suffix-safe-material-123456",
            "prefix-replace-me-suffix-safe-material-1234",
            "prefix-example-suffix-safe-material-123456",
            "Ab3!xY7?z" * 4,
        )
        for value in values:
            with self.subTest(kind=value[:14]):
                with self.assertRaises(ConfigurationError):
                    auth_settings(AUTH_JWT_SECRET=value)

    def test_random_looking_secret_remains_accepted(self):
        value = "Q7!mZ2#vL9@pR4$xT8&nC6*kB3^sF5uH8"
        self.assertEqual(auth_settings(AUTH_JWT_SECRET=value).AUTH_JWT_SECRET, value)

    def test_database_settings_are_immutable_and_url_repr_is_redacted(self):
        database_url = "postgresql://user:UniquePassword@localhost/aura"
        configured = build_database_settings(
            _env_file=None,
            DATABASE_URL=database_url,
            SQL_ECHO=False,
        )
        with self.assertRaises(ValidationError):
            configured.DATABASE_URL = "sqlite://"
        self.assertNotIn(database_url, repr(configured) + str(configured))


class TestEnvironmentIsolationTests(unittest.TestCase):
    def tearDown(self):
        clear_settings_cache()

    def test_tests_package_does_not_bootstrap_process_environment(self):
        initializer = PROJECT_ROOT / "tests" / "__init__.py"
        if initializer.exists():
            content = initializer.read_text(encoding="utf-8")
            self.assertNotIn("setdefault", content)
            self.assertNotIn("os.environ", content)

    def test_environment_patch_is_restored_and_cache_is_cleared(self):
        original = os.environ.get("AURA_G1A_ISOLATION_SENTINEL")
        clear_settings_cache()
        with patch.dict(
            os.environ,
            {
                "AURA_G1A_ISOLATION_SENTINEL": "scoped",
                "APP_ENV": "test",
            },
        ):
            clear_settings_cache()
            self.assertEqual(os.environ["AURA_G1A_ISOLATION_SENTINEL"], "scoped")
            self.assertEqual(
                build_environment_settings(_env_file=None).APP_ENV,
                "test",
            )
        clear_settings_cache()
        self.assertEqual(os.environ.get("AURA_G1A_ISOLATION_SENTINEL"), original)

    def test_clean_subprocess_requires_explicit_app_environment(self):
        environment = dict(os.environ)
        environment.pop("APP_ENV", None)
        missing = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "from app.core.config import build_environment_settings;"
                    "build_environment_settings(_env_file=None)"
                ),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn(CFG_ENV_INVALID, missing.stderr)

        environment["APP_ENV"] = "test"
        explicit = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "from app.core.config import build_environment_settings;"
                    "print(build_environment_settings(_env_file=None).APP_ENV)"
                ),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual(explicit.stdout.strip(), "test")


class StartupBoundaryTests(unittest.TestCase):
    @staticmethod
    def fastapi_environment():
        environment = dict(os.environ)
        for name in tuple(environment):
            if name.startswith("TELEGRAM_"):
                environment.pop(name)
        environment.update(
            {
                "APP_ENV": "test",
                "DATABASE_URL": "sqlite://",
                "AUTH_JWT_SECRET": VALID_JWT_SECRET,
                "AUTH_JWT_ISSUER": "aura",
                "AUTH_JWT_AUDIENCE": "aura-api",
                "AUTH_JWT_EXPIRE_MINUTES": "60",
                "AI_PROVIDER": "ollama",
                "OLLAMA_BASE_URL": "http://localhost:11434/v1",
                "OLLAMA_MODEL": "test-model",
            }
        )
        return environment

    def test_fastapi_import_does_not_require_telegram_configuration(self):
        result = subprocess.run(
            [sys.executable, "-B", "-c", "from app.main import app; print(app.title)"],
            cwd=os.getcwd(),
            env=self.fastapi_environment(),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AURA", result.stdout)

    def test_database_import_does_not_require_auth_ai_or_telegram(self):
        environment = self.fastapi_environment()
        environment.update(
            {
                "AUTH_JWT_SECRET": "invalid",
                "AI_PROVIDER": "invalid",
                "TELEGRAM_BOT_TOKEN": "invalid",
                "TELEGRAM_IDENTITY_SECRET": "invalid",
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "from app.db.database import engine; print(engine.echo)",
            ],
            cwd=os.getcwd(),
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("False", result.stdout)

    def test_migration_import_does_not_require_auth_ai_or_telegram(self):
        environment = self.fastapi_environment()
        environment.update(
            {
                "AUTH_JWT_SECRET": "invalid",
                "AI_PROVIDER": "invalid",
                "TELEGRAM_BOT_TOKEN": "invalid",
                "TELEGRAM_IDENTITY_SECRET": "invalid",
            }
        )
        migration_path = PROJECT_ROOT / "migrations" / "add_support_tickets.py"
        probe = (
            "import importlib.util\n"
            f"path = {str(migration_path)!r}\n"
            "spec = importlib.util.spec_from_file_location('g1a_migration_probe', path)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "print('MIGRATION_IMPORT_OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", probe],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "MIGRATION_IMPORT_OK")

    def test_startup_failure_contains_only_safe_code(self):
        environment = self.fastapi_environment()
        raw_secret = "private-invalid-secret"
        environment["AUTH_JWT_SECRET"] = raw_secret
        result = subprocess.run(
            [sys.executable, "-B", "-c", "from app.main import app"],
            cwd=os.getcwd(),
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(CFG_AUTH_SECRET_INVALID, result.stderr)
        self.assertNotIn(raw_secret, result.stdout + result.stderr)

    def test_invalid_auth_fails_before_ai_provider_construction(self):
        environment = self.fastapi_environment()
        raw_secret = "short-invalid-startup-secret"
        environment["AUTH_JWT_SECRET"] = raw_secret
        probe = (
            "import openai\n"
            "def provider_probe(*args, **kwargs):\n"
            " print('PROVIDER_CONSTRUCTED')\n"
            " raise RuntimeError('provider should not be constructed')\n"
            "openai.AsyncOpenAI = provider_probe\n"
            "from app.main import app\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", probe],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(CFG_AUTH_SECRET_INVALID, combined)
        self.assertNotIn("PROVIDER_CONSTRUCTED", combined)
        self.assertNotIn(raw_secret, combined)

    def test_invalid_ai_fails_before_provider_construction(self):
        environment = self.fastapi_environment()
        environment["AI_PROVIDER"] = "invalid"
        probe = (
            "import openai\n"
            "def provider_probe(*args, **kwargs):\n"
            " print('PROVIDER_CONSTRUCTED')\n"
            " raise RuntimeError('provider should not be constructed')\n"
            "openai.AsyncOpenAI = provider_probe\n"
            "from app.main import app\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", probe],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(CFG_AI_PROVIDER_INVALID, combined)
        self.assertNotIn("PROVIDER_CONSTRUCTED", combined)


if __name__ == "__main__":
    unittest.main()
