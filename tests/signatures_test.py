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

"""Unit tests for framework.signatures: keys, the base, signing, verifying."""

import json
import unittest

from cryptography.hazmat.primitives.asymmetric import ed25519
from framework import signatures
import httpx

# RFC 7638 section 3.1: the example key and its thumbprint.
RFC_7638_KEY = {
  "kty": "RSA",
  "n": (
    "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1R"
    "K7aPFFxuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9"
    "yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZg"
    "nYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZ"
    "u0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
  ),
  "e": "AQAB",
}
RFC_7638_THUMBPRINT = "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"

WEBHOOK_INPUT = (
  'sig1=("@method" "@authority" "@path" "ucp-agent" "content-digest" '
  '"content-type" "webhook-id" "webhook-timestamp");keyid="merchant-2026"'
)


def request(
  key: signatures.SigningKey,
  *,
  method: str = "POST",
  path: str = "/webhooks/orders",
  body: bytes = b'{"id": "ord_1"}',
  headers: dict[str, str] | None = None,
) -> tuple[dict[str, str], bytes]:
  """Sign a request; return its full headers and body."""
  base_headers = {
    "UCP-Agent": 'profile="https://shop.example/.well-known/ucp"',
    "Content-Type": "application/json",
  }
  if headers is not None:
    base_headers = headers
  added = signatures.sign_request(
    key,
    method=method,
    authority="platform.example",
    path=path,
    query=None,
    headers=base_headers,
    body=body or None,
    created=1737000001,
  )
  return {**base_headers, **added}, body


def verify(
  headers: dict[str, str],
  body: bytes,
  keys: list[dict],
  *,
  method: str = "POST",
  path: str = "/webhooks/orders",
  authority: str = "platform.example",
  version: str | None = None,
) -> signatures.Verification:
  """Verify a request built by `request`."""
  return signatures.verify_request(
    method=method,
    authority=authority,
    path=path,
    query=None,
    headers=headers,
    body=body,
    keys=keys,
    version=version,
  )


class KeysTest(unittest.TestCase):
  """JWKs: the profile fields, thumbprints, capability."""

  def test_thumbprint_matches_rfc_7638(self) -> None:
    """The RFC's own example key gives the RFC's thumbprint."""
    self.assertEqual(signatures.thumbprint(RFC_7638_KEY), RFC_7638_THUMBPRINT)

  def test_generated_key_is_es256_named_by_its_thumbprint(self) -> None:
    """A fresh key publishes an EC P-256 JWK with kid = thumbprint."""
    key = signatures.generate_signing_key()
    jwk = key.jwk
    self.assertEqual(
      (jwk["kty"], jwk["crv"], jwk["alg"], jwk["use"]),
      ("EC", "P-256", "ES256", "sig"),
    )
    self.assertEqual(jwk["kid"], signatures.thumbprint(jwk))
    self.assertEqual(key.algorithm, "ES256")

  def test_profile_keys_reads_keys_then_signing_keys(self) -> None:
    """keys[] is canonical; signing_keys[] is the older name."""
    jwk = {"kid": "k", "kty": "EC"}
    self.assertEqual(signatures.profile_keys({"keys": [jwk]}), [jwk])
    self.assertEqual(signatures.profile_keys({"signing_keys": [jwk]}), [jwk])
    self.assertEqual(
      signatures.profile_keys(
        {"keys": [jwk], "signing_keys": [{"kid": "old"}]}
      ),
      [jwk],
    )
    self.assertEqual(signatures.profile_keys({"ucp": {}}), [])

  def test_sig_capable(self) -> None:
    """Encryption keys and keys without verify in key_ops are skipped."""
    self.assertTrue(signatures.sig_capable({"kty": "EC"}))
    self.assertTrue(
      signatures.sig_capable({"use": "sig", "key_ops": ["verify"]})
    )
    self.assertFalse(signatures.sig_capable({"use": "enc"}))
    self.assertFalse(signatures.sig_capable({"key_ops": ["sign"]}))

  def test_algorithm_from_type_and_curve(self) -> None:
    """ES256, ES384 and EdDSA follow from kty/crv; others are refused."""
    self.assertEqual(
      signatures.algorithm_for({"kty": "EC", "crv": "P-256"}), "ES256"
    )
    self.assertEqual(
      signatures.algorithm_for({"kty": "EC", "crv": "P-384"}), "ES384"
    )
    self.assertEqual(
      signatures.algorithm_for({"kty": "OKP", "crv": "Ed25519"}), "EdDSA"
    )
    with self.assertRaises(signatures.SignatureError):
      signatures.algorithm_for({"kty": "RSA"})


class ParsingTest(unittest.TestCase):
  """Signature-Input and Signature parsing."""

  def test_parses_the_webhook_example(self) -> None:
    """order.md's example yields its components, keyid and raw value."""
    [member] = signatures.parse_signature_input(WEBHOOK_INPUT).values()
    self.assertEqual(member.label, "sig1")
    self.assertEqual(member.names[:3], ("@method", "@authority", "@path"))
    self.assertIn("webhook-timestamp", member.names)
    self.assertEqual(member.keyid, "merchant-2026")
    self.assertTrue(member.raw.startswith('("@method"'))
    self.assertTrue(member.raw.endswith('keyid="merchant-2026"'))

  def test_parses_parameters_and_signature_values(self) -> None:
    """created, expires and tag are read; Signature decodes base64."""
    [member] = signatures.parse_signature_input(
      'sig1=("@method");created=1;expires=2;tag="web-bot-auth";keyid="k"'
    ).values()
    self.assertEqual(
      member.params,
      {"created": "1", "expires": "2", "tag": "web-bot-auth", "keyid": "k"},
    )
    self.assertEqual(
      signatures.parse_signature("sig1=:QUJD:"), {"sig1": b"ABC"}
    )
    self.assertEqual(signatures.parse_signature("garbage"), {})


class SignAndVerifyTest(unittest.TestCase):
  """A signed request verifies; each tampering is caught with its reason."""

  @classmethod
  def setUpClass(cls) -> None:
    """Generate one key for the class."""
    cls.key = signatures.generate_signing_key()
    cls.keys = [cls.key.jwk]

  def test_round_trip_es256(self) -> None:
    """A request signed with the key verifies against its JWK."""
    headers, body = request(self.key)
    self.assertIn("Content-Digest", headers)
    outcome = verify(headers, body, self.keys)
    self.assertTrue(outcome.ok, outcome.reason)
    self.assertEqual(outcome.keyid, self.key.kid)
    self.assertEqual(
      outcome.covered,
      (
        "@method",
        "@authority",
        "@path",
        "ucp-agent",
        "content-digest",
        "content-type",
      ),
    )

  def test_webhook_headers_are_covered_when_present(self) -> None:
    """A delivery's webhook-id and webhook-timestamp are signed too."""
    headers, body = request(
      self.key,
      headers={
        "UCP-Agent": 'profile="https://shop.example/.well-known/ucp"',
        "Content-Type": "application/json",
        "Webhook-Id": "evt_1",
        "Webhook-Timestamp": "1737000000",
      },
    )
    outcome = verify(headers, body, self.keys, version=None)
    self.assertTrue(outcome.ok, outcome.reason)
    self.assertIn("webhook-id", outcome.covered)
    self.assertIn("webhook-timestamp", outcome.covered)

  def test_round_trip_ed25519(self) -> None:
    """An Ed25519 key signs and verifies the same way."""
    private = ed25519.Ed25519PrivateKey.generate()
    key = signatures.SigningKey(
      private, signatures._public_jwk(private), "EdDSA"
    )
    self.assertEqual(key.jwk["crv"], "Ed25519")
    headers, body = request(key)
    self.assertTrue(verify(headers, body, [key.jwk]).ok)

  def test_get_without_body_covers_the_target_only(self) -> None:
    """A GET with no body signs @method, @authority, @path (and ucp-agent)."""
    headers, body = request(
      self.key,
      method="GET",
      path="/orders/ord_1",
      body=b"",
      headers={"UCP-Agent": 'profile="https://p.example/ucp"'},
    )
    self.assertNotIn("Content-Digest", headers)
    outcome = verify(
      headers, b"", self.keys, method="GET", path="/orders/ord_1"
    )
    self.assertTrue(outcome.ok, outcome.reason)
    self.assertEqual(
      outcome.covered, ("@method", "@authority", "@path", "ucp-agent")
    )

  def test_tampered_body_is_a_digest_mismatch(self) -> None:
    """Changing the body after signing fails on Content-Digest."""
    headers, _ = request(self.key)
    outcome = verify(headers, b'{"id": "ord_2"}', self.keys)
    self.assertFalse(outcome.ok)
    self.assertTrue(
      outcome.reason.startswith("digest_mismatch"), outcome.reason
    )

  def test_tampered_target_is_signature_invalid(self) -> None:
    """A different path or host breaks the signature over the base."""
    headers, body = request(self.key)
    for kwargs in ({"path": "/elsewhere"}, {"authority": "other.example"}):
      outcome = verify(headers, body, self.keys, **kwargs)
      self.assertFalse(outcome.ok)
      self.assertTrue(
        outcome.reason.startswith("signature_invalid"), outcome.reason
      )

  def test_unknown_key_and_unsigned(self) -> None:
    """A keyid the profile lacks, and no signature at all, are named."""
    headers, body = request(self.key)
    other = signatures.generate_signing_key()
    self.assertTrue(
      verify(headers, body, [other.jwk]).reason.startswith("key_not_found")
    )
    unsigned = {
      k: v
      for k, v in headers.items()
      if k not in ("Signature", "Signature-Input")
    }
    self.assertEqual(verify(unsigned, body, self.keys).reason, "unsigned")

  def test_coverage_depends_on_the_version(self) -> None:
    """webhook-id/-timestamp are required from 2026-10-16 on, not before."""
    # A business on an older release: it sends the webhook headers but its
    # signature predates the rule that covers them.
    headers, body = request(self.key)
    headers["Webhook-Id"] = "evt_1"
    headers["Webhook-Timestamp"] = "1737000000"
    self.assertTrue(verify(headers, body, self.keys, version="2026-04-08").ok)
    self.assertTrue(verify(headers, body, self.keys, version="2026-08-25").ok)
    outcome = verify(headers, body, self.keys, version=None)
    self.assertFalse(outcome.ok)
    self.assertIn("coverage_insufficient", outcome.reason)
    self.assertIn("webhook-id", outcome.reason)
    self.assertTrue(signatures.covers_webhook_identity("2026-12-01"))
    self.assertFalse(signatures.covers_webhook_identity("2026-04-08"))

  def test_required_components(self) -> None:
    """The conditional components follow the request's shape and headers."""
    required = signatures.required_components(
      has_query=True,
      has_body=True,
      headers={"Idempotency-Key": "k", "UCP-Agent": "p", "Webhook-Id": "w"},
      version="2026-04-08",
    )
    self.assertEqual(
      required,
      [
        "@method",
        "@authority",
        "@path",
        "@query",
        "content-digest",
        "content-type",
        "idempotency-key",
        "ucp-agent",
      ],
    )

  def test_ecdsa_value_is_raw_r_s(self) -> None:
    """The ES256 signature value is 64 bytes, not DER."""
    headers, _ = request(self.key)
    [value] = signatures.parse_signature(headers["Signature"]).values()
    self.assertEqual(len(value), 64)

  def test_der_encoded_value_is_refused(self) -> None:
    """A DER signature (the common library default) fails with a reason."""
    headers, body = request(self.key)
    headers["Signature"] = "sig1=:" + "MEUCIQ" + "A" * 90 + ":"
    outcome = verify(headers, body, self.keys)
    self.assertIn("r||s", outcome.reason)


class RequestSignerTest(unittest.TestCase):
  """The httpx auth signs what the client sends."""

  def test_signs_an_httpx_request(self) -> None:
    """The auth adds the three headers, computed over the actual request."""
    key = signatures.generate_signing_key()
    body = json.dumps({"line_items": []}).encode()
    req = httpx.Request(
      "POST",
      "https://shop.example:8443/checkout-sessions?x=1",
      content=body,
      headers={
        "UCP-Agent": 'profile="https://p.example/ucp"',
        "Idempotency-Key": "idem-1",
        "Content-Type": "application/json",
      },
    )
    flow = signatures.RequestSigner(key).auth_flow(req)
    signed = next(flow)
    outcome = signatures.verify_request(
      method="POST",
      authority="shop.example:8443",
      path="/checkout-sessions",
      query="x=1",
      headers=dict(signed.headers),
      body=body,
      keys=[key.jwk],
    )
    self.assertTrue(outcome.ok, outcome.reason)
    self.assertEqual(
      outcome.covered,
      (
        "@method",
        "@authority",
        "@path",
        "@query",
        "ucp-agent",
        "idempotency-key",
        "content-digest",
        "content-type",
      ),
    )


if __name__ == "__main__":
  unittest.main()
