"""Who a portal request is made for: the X-Portal-User-* headers, when they are believed, and what
`set_actor` hands the audit trigger.

    PYTHONPATH=app python -m unittest discover -s tests

No database: the dependency runs inside a throwaway FastAPI app, and `set_actor` is given a session
that only records what it was asked to execute. What the trigger then does with those settings is
database behaviour, checked against a real database — see migration audit_portal_user.
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

# settings refuses to import without these; the values mean nothing here
for _key, _value in {"SERVICE_TOKEN": "test-service-token", "API_TOKEN_PEPPER": "pepper",
                     "MS_WEBHOOK_SECRET": "x"}.items():
    os.environ.setdefault(_key, _value)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from fastapi import Depends, FastAPI            # noqa: E402
from fastapi.testclient import TestClient      # noqa: E402

import api_auth                                  # noqa: E402
from api_auth import (authorize, current_actor, portal_user_from_headers,  # noqa: E402
                      ACTOR_API_KEY, ACTOR_PORTAL, ACTOR_SERVICE)
from Utils.DomainCommon import set_actor, changed_by_user_json  # noqa: E402

SERVICE = {"X-Service-Token": os.environ["SERVICE_TOKEN"]}
USER_ID = "3F2C9A4E-8D1B-4C7A-9E55-0B6D2F1A7C90"
USER = {"X-Portal-User-Id": USER_ID, "X-Portal-User-Email": "anna@example.com",
        "X-Portal-User-Name": "%D0%90%D0%BD%D0%BD%D0%B0%20%D0%98%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2%D0%B0"}
FAKE_KEY = SimpleNamespace(name="partner-key", scopes=["admin"], expires_at=None)


class _RecordingSession:
    """Stands in for an AsyncSession: keeps the parameters of every statement."""

    def __init__(self):
        self.params = []

    async def execute(self, _statement, params=None):
        self.params.append(params)


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/write")
    async def write(token=Depends(authorize("insurance:write"))):
        kind, user = current_actor()
        session = _RecordingSession()
        await set_actor(session, token)
        return {"kind": kind, "user": user.__dict__ if user else None, "db": session.params[0]}

    return app


class PortalUserHeaders(unittest.TestCase):
    def test_cyrillic_name_is_percent_decoded(self):
        user = portal_user_from_headers({k.lower(): v for k, v in USER.items()})
        self.assertEqual(user.name, "Анна Иванова")
        self.assertEqual(user.email, "anna@example.com")
        self.assertEqual(user.id, USER_ID.lower())      # canonical UUID form

    def test_name_is_optional(self):
        user = portal_user_from_headers({"x-portal-user-id": USER_ID})
        self.assertIsNotNone(user)
        self.assertIsNone(user.name)
        self.assertIsNone(user.email)

    def test_no_id_or_bad_id_is_no_user(self):
        self.assertIsNone(portal_user_from_headers({"x-portal-user-email": "a@b.c"}))
        self.assertIsNone(portal_user_from_headers({"x-portal-user-id": "not-a-uuid"}))

    def test_control_characters_are_dropped(self):
        user = portal_user_from_headers({"x-portal-user-id": USER_ID,
                                         "x-portal-user-name": "Anna%0A%0DIvanova"})
        self.assertEqual(user.name, "AnnaIvanova")


class Trust(unittest.TestCase):
    def setUp(self):
        self._lookup = api_auth._lookup_api_token

        async def fake_lookup(_request, key):
            return FAKE_KEY if key == "partner.secret" else None

        api_auth._lookup_api_token = fake_lookup
        self.client = TestClient(_app())

    def tearDown(self):
        api_auth._lookup_api_token = self._lookup

    def test_service_token_with_headers_names_the_user(self):
        body = self.client.post("/write", headers={**SERVICE, **USER}).json()
        self.assertEqual(body["kind"], ACTOR_PORTAL)
        self.assertEqual(body["user"]["name"], "Анна Иванова")
        self.assertEqual(body["db"], {"actor": "service-token", "kind": ACTOR_PORTAL,
                                      "uid": USER_ID.lower(), "email": "anna@example.com",
                                      "name": "Анна Иванова"})

    def test_same_headers_with_an_api_key_are_ignored(self):
        body = self.client.post("/write", headers={"X-Api-Key": "partner.secret", **USER}).json()
        self.assertEqual(body["kind"], ACTOR_API_KEY)
        self.assertIsNone(body["user"])
        self.assertEqual(body["db"], {"actor": "partner-key", "kind": ACTOR_API_KEY,
                                      "uid": "", "email": "", "name": ""})

    def test_service_token_without_headers_is_no_user(self):
        body = self.client.post("/write", headers=SERVICE).json()
        self.assertEqual(body["kind"], ACTOR_SERVICE)
        self.assertIsNone(body["user"])
        self.assertEqual(body["db"]["uid"], "")

    def test_headers_without_credentials_are_refused(self):
        self.assertEqual(self.client.post("/write", headers=USER).status_code, 401)


class Rendering(unittest.TestCase):
    def _row(self, uid=None, email=None, name=None):
        return SimpleNamespace(changed_by_user_id=uid, changed_by_user_email=email,
                               changed_by_user_name=name)

    def test_portal_user(self):
        self.assertEqual(changed_by_user_json(self._row("u-1", "a@b.c", "Анна")),
                         {"id": "u-1", "email": "a@b.c", "name": "Анна"})

    def test_system(self):
        self.assertEqual(changed_by_user_json(self._row(name="System")),
                         {"id": None, "email": None, "name": "System"})

    def test_api_client_is_null(self):
        self.assertIsNone(changed_by_user_json(self._row()))


if __name__ == "__main__":
    unittest.main()
