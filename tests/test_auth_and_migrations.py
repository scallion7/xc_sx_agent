"""阶段 22 OIDC/JWT、配置分层与 Alembic 测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from app.config.settings import Settings
from app.infrastructure.migrations.runtime import (
    downgrade_database,
    migration_status,
    upgrade_database,
)
from app.security.providers import OidcJwtProvider, StaticTokenProvider
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text


def key_material(kid: str = "key-1"):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public_jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return private, {"keys": [public_jwk]}


def token(private, *, kid="key-1", **changes):
    now = datetime.now(timezone.utc)
    claims = {
        "iss": "https://issuer.example",
        "aud": "xc-agent",
        "sub": "subject-1",
        "preferred_username": "user-1",
        "scope": "chat:use session:read",
        "roles": ["user"],
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(changes)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": kid})


class OidcProviderTests(unittest.TestCase):
    def setUp(self):
        self.private, self.jwks = key_material()
        self.provider = OidcJwtProvider(
            issuer="https://issuer.example",
            audience="xc-agent",
            jwks_loader=lambda _force: self.jwks,
        )

    def test_valid_token_maps_identity_and_permissions(self):
        context = self.provider.authenticate(token(self.private))
        self.assertEqual(context.user_id, "user-1")
        self.assertEqual(context.provider, "oidc")
        self.assertIn("chat:use", context.permissions)

    def test_admin_role_maps_management_permissions(self):
        context = self.provider.authenticate(token(self.private, roles=["admin"]))
        self.assertEqual(context.role, "admin")
        self.assertIn("rules:manage", context.permissions)

    def test_expired_token_is_rejected_with_stable_code(self):
        expired = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertIsNone(self.provider.authenticate(token(self.private, exp=expired)))
        self.assertEqual(self.provider.last_error_code, "OIDC_TOKEN_EXPIRED")

    def test_missing_scope_is_rejected(self):
        self.assertIsNone(
            self.provider.authenticate(token(self.private, scope="other"))
        )
        self.assertEqual(self.provider.last_error_code, "OIDC_SCOPE_MISSING")

    def test_unconfigured_signing_algorithm_is_rejected(self):
        alternate = jwt.encode(
            {
                "iss": "https://issuer.example",
                "aud": "xc-agent",
                "sub": "subject-1",
                "scope": "chat:use",
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            },
            self.private,
            algorithm="RS384",
            headers={"kid": "key-1"},
        )
        self.assertIsNone(self.provider.authenticate(alternate))
        self.assertEqual(
            self.provider.last_error_code,
            "OIDC_ALGORITHM_NOT_ALLOWED",
        )

    def test_unknown_kid_triggers_forced_refresh(self):
        calls: list[bool] = []

        def loader(force: bool):
            calls.append(force)
            return self.jwks if force else {"keys": []}

        provider = OidcJwtProvider(
            issuer="https://issuer.example",
            audience="xc-agent",
            jwks_loader=loader,
        )
        self.assertIsNotNone(provider.authenticate(token(self.private)))
        self.assertEqual(calls, [False, True])

    def test_cached_key_degrades_when_refresh_fails(self):
        now = datetime.now(timezone.utc)
        fail = False

        def loader(_force: bool):
            if fail:
                raise OSError("offline")
            return self.jwks

        provider = OidcJwtProvider(
            issuer="https://issuer.example",
            audience="xc-agent",
            jwks_loader=loader,
            cache_seconds=1,
            clock=lambda: now,
        )
        self.assertIsNotNone(provider.authenticate(token(self.private)))
        fail = True
        now += timedelta(seconds=2)
        self.assertIsNotNone(provider.authenticate(token(self.private)))

    def test_static_provider_remains_compatible(self):
        provider = StaticTokenProvider("token:user-1", "admin:root")
        self.assertEqual(provider.authenticate("token").user_id, "user-1")
        self.assertEqual(provider.authenticate("admin").role, "admin")


class MigrationAndConfigurationTests(unittest.TestCase):
    def test_empty_sqlite_upgrade_and_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{Path(tmp) / 'empty.db'}"
            upgrade_database(url)
            self.assertTrue(migration_status(url)["up_to_date"])
            tables = set(inspect(create_engine(url)).get_table_names())
            self.assertIn("plan_runs", tables)
            self.assertIn("risk_rule_versions", tables)
            downgrade_database(url, "base")
            self.assertIsNone(migration_status(url)["current"])

    def test_compatible_upgrade_preserves_existing_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{Path(tmp) / 'existing.db'}"
            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(
                    text("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY)")
                )
                connection.execute(text("INSERT INTO legacy_marker (id) VALUES (1)"))
            upgrade_database(url)
            with engine.begin() as connection:
                count = connection.execute(
                    text("SELECT COUNT(*) FROM legacy_marker")
                ).scalar_one()
            self.assertEqual(count, 1)

    def test_production_defaults_fail_closed(self):
        with self.assertRaises(ValidationError):
            Settings(environment="production")

    def test_safe_summary_excludes_secrets(self):
        settings = Settings(
            openai_api_key="super-secret",
            api_admin_tokens="secret:admin",
        )
        encoded = json.dumps(settings.safe_summary())
        self.assertNotIn("super-secret", encoded)
        self.assertNotIn("secret:admin", encoded)


if __name__ == "__main__":
    unittest.main()
