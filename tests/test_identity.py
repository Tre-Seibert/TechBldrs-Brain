from __future__ import annotations

import unittest

import jwt

from app.config import Settings
from app.identity import resolve_actor

SECRET = "test-forward-user-info-jwt-secret"


def _make_settings(**overrides: object) -> Settings:
    return Settings(flow_mode="stub", **overrides)


def _sign(claims: dict[str, object], secret: str = SECRET) -> str:
    return jwt.encode(claims, secret, algorithm="HS256")


class ResolveActorTests(unittest.TestCase):
    def test_jwt_email_wins_over_raw_header(self) -> None:
        settings = _make_settings(brain_user_jwt_secret=SECRET)
        token = _sign({"sub": "abc", "email": "Tech@Example.com", "role": "user"})
        resolved = resolve_actor(
            settings,
            user_jwt=token,
            user_email_header="someone-else@example.com",
            fallback_actor=None,
        )
        self.assertTrue(resolved.verified)
        self.assertEqual(resolved.email, "tech@example.com")
        self.assertEqual(resolved.label, "tech@example.com")

    def test_unsigned_email_header_ignored_when_secret_configured(self) -> None:
        settings = _make_settings(brain_user_jwt_secret=SECRET)
        resolved = resolve_actor(
            settings,
            user_jwt=None,
            user_email_header="spoofed@example.com",
            fallback_actor="lab-local",
        )
        self.assertFalse(resolved.verified)
        self.assertIsNone(resolved.email)
        self.assertEqual(resolved.label, "lab-local")

    def test_invalid_jwt_does_not_verify_and_ignores_header(self) -> None:
        settings = _make_settings(brain_user_jwt_secret=SECRET)
        bad_token = _sign({"sub": "abc", "email": "tech@example.com"}, secret="wrong-secret")
        resolved = resolve_actor(
            settings,
            user_jwt=bad_token,
            user_email_header="spoofed@example.com",
            fallback_actor="lab-local",
        )
        self.assertFalse(resolved.verified)
        self.assertIsNone(resolved.email)

    def test_no_secret_configured_falls_back_to_raw_header_unverified(self) -> None:
        settings = _make_settings(brain_user_jwt_secret="")
        resolved = resolve_actor(
            settings,
            user_jwt=None,
            user_email_header="Lab.Tech@Example.com",
            fallback_actor="lab-local",
        )
        self.assertFalse(resolved.verified)
        self.assertEqual(resolved.email, "lab.tech@example.com")

    def test_no_secret_and_no_header_uses_fallback_label(self) -> None:
        settings = _make_settings(brain_user_jwt_secret="")
        resolved = resolve_actor(
            settings,
            user_jwt=None,
            user_email_header=None,
            fallback_actor="x-brain-actor-label",
        )
        self.assertFalse(resolved.verified)
        self.assertIsNone(resolved.email)
        self.assertEqual(resolved.label, "x-brain-actor-label")

    def test_oauth_email_claim_override(self) -> None:
        settings = _make_settings(brain_user_jwt_secret=SECRET, oauth_email_claim="preferred_username")
        token = _sign({"sub": "abc", "preferred_username": "tech@example.com"})
        resolved = resolve_actor(
            settings,
            user_jwt=token,
            user_email_header=None,
            fallback_actor=None,
        )
        self.assertTrue(resolved.verified)
        self.assertEqual(resolved.email, "tech@example.com")


if __name__ == "__main__":
    unittest.main()
