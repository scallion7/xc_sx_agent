"""Issuer/subject isolation and explicit, collision-safe legacy account bindings."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.api import create_app
from app.config.settings import Settings
from app.repositories.session import SqlSessionRepository
from app.security.identities import load_identity_bindings, oidc_user_id
from app.security.providers import (
    OidcJwtProvider,
    StaticTokenProvider,
    build_auth_provider,
)
from app.services.actions import ActionService, digest
from app.web.services import session_reference
from fastapi.testclient import TestClient

from tests.test_auth_and_migrations import key_material, token

ISSUER = "https://issuer.example"


class OidcIdentityTests(unittest.TestCase):
    def setUp(self):
        self.private, self.jwks = key_material()
        self.provider = self.provider_for()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def provider_for(self, issuer=ISSUER, bindings=None):
        return OidcJwtProvider(
            issuer=issuer,
            audience="xc-agent",
            jwks_loader=lambda _: self.jwks,
            identity_bindings=bindings,
        )

    def context(self, subject, username="same-name"):
        return self.provider.authenticate(
            token(self.private, sub=subject, preferred_username=username)
        )

    def config(self, **updates):
        return Settings(
            _env_file=None,
            database_url=f"sqlite:///{self.directory / 'test.db'}",
            memory_enabled=False,
            skills_enabled=False,
            observability_enabled=False,
            openai_api_key="offline",
            **updates,
        )

    def write_map(self, bindings):
        path = self.directory / "identities.json"
        path.write_text(json.dumps({"version": 1, "bindings": bindings}))
        return str(path)

    def test_browser_scope_binds_verified_principal_and_tenant(self):
        first = token(self.private, sub="first", jti="one")
        renewed = token(
            self.private, sub="first", jti="two", preferred_username="renamed"
        )
        other = token(self.private, sub="second", jti="three")

        def read(tenant, credential):
            client = TestClient(
                create_app(self.config(tenant_id=tenant), auth_provider=self.provider)
            )
            return client.get("/v1/auth/session", headers={"X-API-Key": credential})

        original = read("shop-a", first)
        self.assertEqual(original.status_code, 200)
        self.assertEqual(original.headers["cache-control"], "no-store")
        self.assertEqual(original.json(), read("shop-a", renewed).json())
        self.assertNotEqual(original.json(), read("shop-a", other).json())
        self.assertNotEqual(original.json(), read("shop-b", renewed).json())
        self.assertEqual(read("shop-a", "").status_code, 401)

    def test_same_display_name_and_long_subject_prefixes_do_not_collide(self):
        self.assertNotEqual(self.context("alice").user_id, self.context("bob").user_id)
        self.assertNotEqual(
            self.context("s" * 100 + "a", "n" * 100 + "a").user_id,
            self.context("s" * 100 + "b", "n" * 100 + "b").user_id,
        )

    def test_rename_and_path_characters_never_change_authorization_identity(self):
        first = self.context("subject/../alice", "Alice")
        renamed = self.context("subject/../alice", "../../victim")
        self.assertEqual(first.user_id, renamed.user_id)
        self.assertEqual(renamed.display_name, "../../victim")
        self.assertRegex(renamed.user_id, r"^oidc_[0-9a-f]{64}$")
        services = SimpleNamespace(
            config=SimpleNamespace(
                persistence_backend="json", session_dir=str(self.directory)
            )
        )
        reference = Path(session_reference(services, renamed, "private")).resolve()
        self.assertEqual(reference.parent.parent, self.directory.resolve())

    def test_issuer_is_part_of_identity_and_is_not_slash_normalized(self):
        a = self.context("alice")
        other = self.provider_for(ISSUER + "/")
        b = other.authenticate(token(self.private, iss=ISSUER + "/", sub="alice"))
        self.assertIsNotNone(b)
        self.assertNotEqual(a.user_id, b.user_id)
        self.assertIsNone(other.authenticate(token(self.private, sub="alice")))

    def test_empty_subject_is_rejected(self):
        self.assertIsNone(self.context(""))
        self.assertEqual(self.provider.last_error_code, "OIDC_SUBJECT_INVALID")

    def test_http_same_named_subject_cannot_read_or_confirm_another_account(self):
        config = self.config()
        app = create_app(config, auth_provider=self.provider)
        alice = self.context("alice")
        session = f"{alice.user_id}:private"
        SqlSessionRepository(config.database_url, session, alice.user_id).save(
            [{"role": "user", "content": "Alice private message"}], None, None
        )
        actions = ActionService(config.database_url)
        action = actions.propose(
            alice.user_id, session, "create_after_sale_case", {}, digest({})
        )
        with TestClient(app) as client:
            client.headers["X-API-Key"] = token(
                self.private, sub="bob", preferred_username="same-name"
            )
            self.assertEqual(client.get("/v1/sessions/private").status_code, 404)
            self.assertEqual(
                client.get(
                    f"/v1/actions/{action['action_id']}",
                    params={"session_id": "private"},
                ).status_code,
                404,
            )
            self.assertEqual(
                client.post(
                    f"/v1/actions/{action['action_id']}/confirm",
                    json={
                        "session_id": "private",
                        "arguments_hash": action["arguments_hash"],
                    },
                ).status_code,
                404,
            )
        self.assertEqual(
            actions.get(action["action_id"], alice.user_id, session)["status"],
            "pending",
        )

    def test_explicit_binding_preserves_legacy_data_without_username_claiming(self):
        self.provider = self.provider_for(bindings={(ISSUER, "verified-sub"): "user-1"})
        config = self.config()
        app = create_app(config, auth_provider=self.provider)
        SqlSessionRepository(config.database_url, "user-1:legacy", "user-1").save(
            [{"role": "user", "content": "Existing history"}], None, None
        )
        actions = ActionService(config.database_url)
        action = actions.propose(
            "user-1", "user-1:legacy", "create_after_sale_case", {}, digest({})
        )
        with TestClient(app) as client:
            client.headers["X-API-Key"] = token(
                self.private, sub="verified-sub", preferred_username="renamed"
            )
            self.assertEqual(client.get("/v1/sessions/legacy").status_code, 200)
            self.assertEqual(
                client.get(
                    f"/v1/actions/{action['action_id']}",
                    params={"session_id": "legacy"},
                ).status_code,
                200,
            )
            client.headers["X-API-Key"] = token(
                self.private, sub="unbound-sub", preferred_username="user-1"
            )
            self.assertEqual(client.get("/v1/sessions/legacy").status_code, 404)
        self.assertEqual(self.context("verified-sub", "renamed").user_id, "user-1")
        self.assertNotEqual(self.context("unbound-sub", "user-1").user_id, "user-1")

    def test_mapping_validates_unique_subjects_and_safe_unique_legacy_ids(self):
        base = {"issuer": ISSUER, "subject": "alice", "user_id": "legacy"}
        for bindings in (
            [base, base],
            [base, {**base, "subject": "bob"}],
            [{**base, "user_id": "../victim"}],
            [{**base, "user_id": oidc_user_id(ISSUER, "alice")}],
            [{**base, "user_id": "x" * 81}],
            [{**base, "subject": ""}],
            [{**base, "username": "untrusted"}],
        ):
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                load_identity_bindings(self.write_map(bindings))

    def test_reserved_namespace_and_legacy_map_prevent_static_identity_collision(self):
        with self.assertRaisesRegex(ValueError, "STATIC_IDENTITY_RESERVED_FOR_OIDC"):
            StaticTokenProvider("token:" + oidc_user_id(ISSUER, "alice"))
        path = self.write_map(
            [{"issuer": ISSUER, "subject": "alice", "user_id": "user-1"}]
        )
        # Inactive static settings do not stop an explicit OIDC account migration.
        jwks_path = self.directory / "jwks.json"
        jwks_path.write_text(json.dumps(self.jwks))
        config = self.config(
            auth_mode="oidc",
            oidc_issuer=ISSUER,
            oidc_audience="xc-agent",
            oidc_jwks_path=str(jwks_path),
            oidc_identity_map_path=path,
        )
        provider = build_auth_provider(config)
        self.assertEqual(
            provider.authenticate(token(self.private, sub="alice")).user_id, "user-1"
        )
        with self.assertRaisesRegex(ValueError, "STATIC_IDENTITY_RESERVED_FOR_OIDC"):
            build_auth_provider(config.model_copy(update={"auth_mode": "static"}))
        provider = build_auth_provider(
            config.model_copy(
                update={"auth_mode": "static", "api_user_tokens": "token:unrelated"}
            )
        )
        self.assertEqual(provider.authenticate("token").user_id, "unrelated")

    def test_mapping_file_failure_does_not_fall_back_to_unbound_accounts(self):
        with self.assertRaisesRegex(ValueError, "OIDC_IDENTITY_MAP_UNAVAILABLE"):
            build_auth_provider(
                self.config(oidc_identity_map_path=str(self.directory / "missing.json"))
            )

    def test_production_oidc_does_not_enable_or_validate_inactive_demo_tokens(self):
        config = Settings(
            _env_file=None,
            environment="production",
            auth_mode="oidc",
            oidc_issuer=ISSUER,
            oidc_audience="xc-agent",
            oidc_jwks_path="operator-jwks.json",
            openai_api_key="offline-validation-only",
            commerce_seed_demo_data=False,
            database_auto_create=False,
            provider_mode="external",
            provider_webhook_secret="offline-webhook-secret",
        )
        self.assertEqual(config.auth_mode, "oidc")
        with self.assertRaisesRegex(ValueError, "生产环境禁止使用默认开发 Token"):
            Settings(**(config.model_dump() | {"auth_mode": "static"}), _env_file=None)
