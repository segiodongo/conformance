#   Copyright 2026 UCP Authors
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""Unit tests for the platform's credentials in the base test.

The platform key, the agent profile that publishes it, the --auth_header
credential and the credential-free headers the authentication test sends.
"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from absl.testing import flagsaver
from framework import base_test, signatures
from framework.agent_profile_server import AgentProfileServer
from framework.base_test import BaseIntegrationTest, auth_header, platform_key


class PlatformKeyTest(unittest.TestCase):
  """platform_key: one per process, from the flag or fresh."""

  def setUp(self) -> None:
    """Forget the cached key between tests."""
    platform_key.cache_clear()
    self.addCleanup(platform_key.cache_clear)

  def test_fresh_key_is_es256_and_stable_within_the_process(self) -> None:
    """Without a flag, a fresh ES256 key is generated once and reused."""
    with (
      flagsaver.flagsaver(platform_key_file=None),
      mock.patch.dict(os.environ),
    ):
      os.environ.pop("FLAGS_platform_key_file", None)
      key = platform_key()
      self.assertEqual(key.algorithm, "ES256")
      self.assertIs(platform_key(), key)

  def test_key_file_is_loaded(self) -> None:
    """A private JWK file names the key the suite signs with."""
    generated = signatures.generate_signing_key()
    numbers = generated.private_key.private_numbers()
    private_jwk = dict(
      generated.jwk,
      d=signatures.b64url(numbers.private_value.to_bytes(32, "big")),
    )
    with tempfile.TemporaryDirectory() as folder:
      path = Path(folder) / "platform.jwk"
      path.write_text(json.dumps(private_jwk))
      with flagsaver.flagsaver(platform_key_file=str(path)):
        loaded = platform_key()
    self.assertEqual(loaded.jwk, generated.jwk)


class AgentProfileKeysTest(unittest.TestCase):
  """The agent profile publishes the platform key under both field names."""

  def test_keys_are_published(self) -> None:
    """keys[] and signing_keys[] both carry the JWK; none when no key."""
    jwk = signatures.generate_signing_key().jwk
    with_key = AgentProfileServer(port=0, webhook_port=8284, keys=[jwk])
    profile = with_key.render_profile()
    self.assertEqual(profile["keys"], [jwk])
    self.assertEqual(profile["signing_keys"], [jwk])
    without = AgentProfileServer(port=0, webhook_port=8284).render_profile()
    self.assertNotIn("keys", without)
    self.assertNotIn("signing_keys", without)


class AuthHeaderTest(unittest.TestCase):
  """--auth_header: parsed as 'Name: value', sent, and left out on demand."""

  def test_parsing(self) -> None:
    """A well-formed value splits once; a malformed one is an error."""
    with flagsaver.flagsaver(auth_header="X-API-Key: secret:with:colons"):
      self.assertEqual(auth_header(), ("X-API-Key", "secret:with:colons"))
    with flagsaver.flagsaver(auth_header=None), mock.patch.dict(os.environ):
      os.environ.pop("FLAGS_auth_header", None)
      self.assertIsNone(auth_header())
    with (
      flagsaver.flagsaver(auth_header="no-separator"),
      self.assertRaises(ValueError),
    ):
      auth_header()

  def test_headers_with_and_without_credentials(self) -> None:
    """get_headers carries the credential; credential_free_headers doesn't."""
    case = BaseIntegrationTest()
    with flagsaver.flagsaver(auth_header="Authorization: Bearer t0k3n"):
      full = case.get_headers()
      bare = case.credential_free_headers()
    self.assertEqual(full["Authorization"], "Bearer t0k3n")
    self.assertEqual(full["request-signature"], "test")
    self.assertNotIn("Authorization", bare)
    self.assertNotIn("request-signature", bare)
    for kept in ("UCP-Agent", "idempotency-key", "request-id", "Content-Type"):
      self.assertIn(kept, bare)


class SigningClientTest(unittest.TestCase):
  """The suite's client signs what it sends."""

  def test_client_signs_requests(self) -> None:
    """A request through the client carries a signature that verifies."""
    platform_key.cache_clear()
    self.addCleanup(platform_key.cache_clear)
    seen = {}

    def handler(request):
      seen["request"] = request
      return base_test.httpx.Response(200, json={})

    transport = base_test.httpx.MockTransport(handler)
    with (
      flagsaver.flagsaver(platform_key_file=None),
      mock.patch.dict(os.environ),
    ):
      os.environ.pop("FLAGS_platform_key_file", None)
      client = base_test.httpx.Client(
        base_url="https://shop.example",
        transport=transport,
        auth=signatures.RequestSigner(platform_key()),
      )
      client.post(
        "/orders/x",
        content=b"{}",
        headers={
          "UCP-Agent": 'profile="https://p.example/ucp"',
          "Content-Type": "application/json",
        },
      )
      client.get("/orders/x", auth=None)
      signed = seen["request"]
    # The second request went unsigned; check the first by replaying it.
    self.assertNotIn("signature", signed.headers)
    client.post(
      "/orders/y", content=b"{}", headers={"Content-Type": "application/json"}
    )
    signed = seen["request"]
    outcome = signatures.verify_request(
      method="POST",
      authority="shop.example",
      path="/orders/y",
      query=None,
      headers=dict(signed.headers),
      body=b"{}",
      keys=[platform_key().jwk],
    )
    self.assertTrue(outcome.ok, outcome.reason)


if __name__ == "__main__":
  unittest.main()
