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

"""HTTP Message Signatures (RFC 9421) as UCP uses them, both ways.

signatures.md binds requests and webhook deliveries with RFC 9421: a signer
publishes JWK public keys in its profile, signs a fixed set of components
(``@method``, ``@authority``, ``@path``, the body's ``Content-Digest`` and
headers such as ``UCP-Agent``), and names the key by ``keyid``. ES256 is the
baseline every verifier supports; Ed25519 is optional.

This module gives the suite both roles:

* a **signer** for its own requests, so it is a platform the business can
  authenticate (``sign_request`` and the ``RequestSigner`` httpx auth), with
  an ES256 key whose public JWK the mock agent profile publishes;
* a **verifier** for the business's webhook deliveries (``verify_request``),
  following ``verify_rest_request`` in signatures.md step by step: find the
  key by ``keyid`` among the profile's signature-capable keys, require the
  components the version in force must cover, check ``Content-Digest``
  against the raw body, rebuild the signature base and verify the value.

Keys are read from the profile's ``keys[]`` (the canonical field) or
``signing_keys[]`` (its name up to the 2026-08-25 release).
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
import dataclasses
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric import utils as asymmetric
import httpx

# The release history's view of which headers a webhook signature covers:
# ucp#659 (merged 2026-10-16, after the 2026-08-25 release) added webhook-id
# and webhook-timestamp to the components a signature must cover when the
# headers are present. A server advertising an earlier version isn't held to
# it; a later version, or the draft, is.
WEBHOOK_IDENTITY_COVERED_SINCE = "2026-10-16"

# RFC 7638 §3.2: the members that make a key's thumbprint, per key type.
_THUMBPRINT_MEMBERS = {
  "EC": ("crv", "kty", "x", "y"),
  "OKP": ("crv", "kty", "x"),
  "RSA": ("e", "kty", "n"),
}

_SF_KEY = r"[a-z*][a-z0-9_.*-]*"
_SF_PARAMS = rf'(?:;{_SF_KEY}(?:=(?:"[^"]*"|[^;,\s]+))?)*'
_SIGNATURE_INPUT_MEMBER = re.compile(
  rf"(?P<label>{_SF_KEY})=(?P<raw>\((?P<components>[^)]*)\)(?P<params>{_SF_PARAMS}))"
)
_SIGNATURE_MEMBER = re.compile(
  rf"(?P<label>{_SF_KEY})=:(?P<value>[A-Za-z0-9+/=]*):"
)
_COMPONENT = re.compile(rf'"(?P<name>[^"]+)"(?P<params>{_SF_PARAMS})')
_PARAM = re.compile(rf'(?P<key>{_SF_KEY})(?:=(?P<value>"[^"]*"|[^;,\s]+))?')


class SignatureError(ValueError):
  """A header, key or request the signature code can't work with."""


# ── encoding helpers ───────────────────────────────────────────────────────


def b64url(data: bytes) -> str:
  """Base64url without padding, as JWK and JWS use it."""
  return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
  """Decode base64url with or without padding."""
  padded = text + "=" * (-len(text) % 4)
  return base64.urlsafe_b64decode(padded)


def content_digest(body: bytes) -> str:
  """Return the RFC 9530 ``Content-Digest`` UCP requires (sha-256)."""
  return f"sha-256=:{base64.b64encode(hashlib.sha256(body).digest()).decode()}:"


# ── keys ───────────────────────────────────────────────────────────────────


def profile_keys(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
  """Return the signing keys a profile publishes.

  Args:
    profile: The profile document, as served at /.well-known/ucp.

  Returns:
    The JWKs from ``keys[]`` (canonical) or else ``signing_keys[]`` (the
    field's name through the 2026-08-25 release), as dicts.

  """
  for field in ("keys", "signing_keys"):
    value = profile.get(field)
    if isinstance(value, list):
      return [key for key in value if isinstance(key, dict)]
  return []


def sig_capable(jwk: Mapping[str, Any]) -> bool:
  """Whether a JWK may verify signatures (RFC 7517 §4.2 and §4.3)."""
  if jwk.get("use") == "enc":
    return False
  key_ops = jwk.get("key_ops")
  return key_ops is None or "verify" in key_ops


def thumbprint(jwk: Mapping[str, Any]) -> str:
  """Return the key's RFC 7638 thumbprint, UCP's recommended ``kid``.

  Args:
    jwk: A JWK with the members its key type requires.

  Returns:
    base64url(SHA-256(the required members, as compact JSON in lexicographic
    order)).

  Raises:
    SignatureError: If the key type is unknown or a member is missing.

  """
  kty = jwk.get("kty")
  members = _THUMBPRINT_MEMBERS.get(str(kty))
  if members is None:
    raise SignatureError(f"no thumbprint rule for key type {kty!r}")
  try:
    subset = {name: jwk[name] for name in members}
  except KeyError as error:
    raise SignatureError(f"JWK lacks {error} for its thumbprint") from None
  canonical = json.dumps(subset, separators=(",", ":"), sort_keys=True)
  return b64url(hashlib.sha256(canonical.encode("utf-8")).digest())


def algorithm_for(jwk: Mapping[str, Any]) -> str:
  """Return the JWS algorithm a key signs with, from its type and curve.

  signatures.md derives the algorithm from ``kty``/``crv``; ``alg`` in
  ``Signature-Input`` is not used.

  Raises:
    SignatureError: For a key type or curve this verifier doesn't support.

  """
  kty, crv = jwk.get("kty"), jwk.get("crv")
  if kty == "EC" and crv == "P-256":
    return "ES256"
  if kty == "EC" and crv == "P-384":
    return "ES384"
  if kty == "OKP" and crv == "Ed25519":
    return "EdDSA"
  raise SignatureError(f"unsupported key: kty={kty!r} crv={crv!r}")


def public_key_from_jwk(jwk: Mapping[str, Any]) -> Any:
  """Build a verifying key from a public JWK.

  Raises:
    SignatureError: For an unsupported or malformed key.

  """
  algorithm = algorithm_for(jwk)
  try:
    if algorithm == "EdDSA":
      return ed25519.Ed25519PublicKey.from_public_bytes(b64url_decode(jwk["x"]))
    curve = ec.SECP256R1() if algorithm == "ES256" else ec.SECP384R1()
    numbers = ec.EllipticCurvePublicNumbers(
      int.from_bytes(b64url_decode(jwk["x"]), "big"),
      int.from_bytes(b64url_decode(jwk["y"]), "big"),
      curve,
    )
    return numbers.public_key()
  except (KeyError, ValueError) as error:
    raise SignatureError(f"malformed JWK: {error}") from error


def _coordinate_size(algorithm: str) -> int:
  """Bytes per ECDSA coordinate: 32 for P-256, 48 for P-384."""
  return 32 if algorithm == "ES256" else 48


def _hash_for(algorithm: str) -> hashes.HashAlgorithm:
  return hashes.SHA256() if algorithm == "ES256" else hashes.SHA384()


@dataclasses.dataclass(frozen=True)
class SigningKey:
  """A private key with the public JWK that names it.

  Attributes:
    private_key: The key object used to sign.
    jwk: The public JWK to publish, with ``kid``, ``alg`` and ``use``.
    algorithm: ``ES256``, ``ES384`` or ``EdDSA``.

  """

  private_key: Any
  jwk: dict[str, Any]
  algorithm: str

  @property
  def kid(self) -> str:
    """The key's identifier, sent as ``keyid``."""
    return str(self.jwk["kid"])


def _public_jwk(private_key: Any, kid: str | None = None) -> dict[str, Any]:
  """Describe a private key's public half as a JWK, kid = thumbprint."""
  public = private_key.public_key()
  if isinstance(private_key, ed25519.Ed25519PrivateKey):
    raw = public.public_bytes(
      serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    jwk: dict[str, Any] = {"kty": "OKP", "crv": "Ed25519", "x": b64url(raw)}
    jwk["alg"] = "EdDSA"
  elif isinstance(private_key, ec.EllipticCurvePrivateKey):
    numbers = public.public_numbers()
    size = (private_key.curve.key_size + 7) // 8
    crv = {"secp256r1": "P-256", "secp384r1": "P-384"}.get(
      private_key.curve.name
    )
    if crv is None:
      raise SignatureError(f"unsupported curve {private_key.curve.name}")
    jwk = {
      "kty": "EC",
      "crv": crv,
      "x": b64url(numbers.x.to_bytes(size, "big")),
      "y": b64url(numbers.y.to_bytes(size, "big")),
    }
    jwk["alg"] = "ES256" if crv == "P-256" else "ES384"
  else:
    raise SignatureError(f"unsupported private key {type(private_key)}")
  jwk["use"] = "sig"
  jwk["kid"] = kid or thumbprint(jwk)
  return jwk


def generate_signing_key() -> SigningKey:
  """Generate a fresh ES256 key, the algorithm every UCP verifier supports."""
  private_key = ec.generate_private_key(ec.SECP256R1())
  jwk = _public_jwk(private_key)
  return SigningKey(private_key, jwk, "ES256")


def load_signing_key(path: str | Path) -> SigningKey:
  """Load a private key from a PEM file or a private JWK (JSON) file.

  Args:
    path: The file. PEM holds an EC P-256/P-384 or Ed25519 private key; JSON
      holds a JWK with ``d`` (and ``kid`` to keep, else the thumbprint).

  Returns:
    The key, with its public JWK.

  Raises:
    SignatureError: If the file holds something else.

  """
  data = Path(path).read_bytes()
  if data.lstrip().startswith(b"{"):
    jwk = json.loads(data)
    algorithm = algorithm_for(jwk)
    try:
      if algorithm == "EdDSA":
        private_key: Any = ed25519.Ed25519PrivateKey.from_private_bytes(
          b64url_decode(jwk["d"])
        )
      else:
        private_key = ec.derive_private_key(
          int.from_bytes(b64url_decode(jwk["d"]), "big"),
          ec.SECP256R1() if algorithm == "ES256" else ec.SECP384R1(),
        )
    except (KeyError, ValueError) as error:
      raise SignatureError(f"malformed private JWK: {error}") from error
    return SigningKey(
      private_key, _public_jwk(private_key, jwk.get("kid")), algorithm
    )
  try:
    private_key = serialization.load_pem_private_key(data, password=None)
  except (ValueError, TypeError) as error:
    raise SignatureError(f"not a PEM private key: {error}") from error
  jwk = _public_jwk(private_key)
  return SigningKey(private_key, jwk, str(jwk["alg"]))


# ── Signature-Input and Signature ──────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class SignatureInput:
  """One member of a ``Signature-Input`` dictionary.

  Attributes:
    label: The member's key, such as ``sig1``.
    components: The covered component identifiers as written, with their
      parameters, such as ``"@method"`` or ``"signature-agent";key="sig1"``.
    names: The bare component names, lower-cased, in the same order.
    params: The signature parameters: ``keyid``, ``created``, ``expires``,
      ``nonce``, ``tag``, ``alg`` when present, unquoted.
    raw: The member value as written: the inner list and its parameters,
      which is what the ``@signature-params`` line of the base repeats.

  """

  label: str
  components: tuple[str, ...]
  names: tuple[str, ...]
  params: dict[str, str]
  raw: str

  @property
  def keyid(self) -> str | None:
    """The ``keyid`` parameter, if any."""
    return self.params.get("keyid")


def _unquote(value: str | None) -> str | None:
  if value is None:
    return None
  if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
    return value[1:-1]
  return value


def parse_signature_input(value: str) -> dict[str, SignatureInput]:
  """Parse a ``Signature-Input`` header into its members by label.

  Args:
    value: The header value, an RFC 8941 dictionary of inner lists.

  Returns:
    The members, keyed by label, in order. Empty when nothing parses.

  """
  members: dict[str, SignatureInput] = {}
  for match in _SIGNATURE_INPUT_MEMBER.finditer(value):
    components = tuple(
      component.group(0)
      for component in _COMPONENT.finditer(match.group("components"))
    )
    names = tuple(
      component.group("name").lower()
      for component in _COMPONENT.finditer(match.group("components"))
    )
    params = {
      param.group("key"): _unquote(param.group("value")) or ""
      for param in _PARAM.finditer(match.group("params").lstrip(";"))
      if param.group("key")
    }
    members[match.group("label")] = SignatureInput(
      match.group("label"), components, names, params, match.group("raw")
    )
  return members


def parse_signature(value: str) -> dict[str, bytes]:
  """Parse a ``Signature`` header into signature bytes by label."""
  members: dict[str, bytes] = {}
  for match in _SIGNATURE_MEMBER.finditer(value):
    try:
      members[match.group("label")] = base64.b64decode(
        match.group("value"), validate=True
      )
    except ValueError:
      continue
  return members


# ── the signature base (RFC 9421 §2.5) ─────────────────────────────────────


def _header(headers: Mapping[str, str], name: str) -> str | None:
  """Return a header's value by case-insensitive name, whitespace trimmed."""
  for key, value in headers.items():
    if key.lower() == name:
      return " ".join(str(value).split()) if value is not None else None
  return None


def signature_base(
  sig_input: SignatureInput,
  *,
  method: str | None = None,
  authority: str | None = None,
  path: str | None = None,
  query: str | None = None,
  status: int | None = None,
  headers: Mapping[str, str],
) -> bytes:
  """Build the RFC 9421 signature base for a message.

  Args:
    sig_input: The parsed ``Signature-Input`` member being signed or
      verified; its ``raw`` value becomes the ``@signature-params`` line.
    method: The request method, for ``@method``.
    authority: The target host (and port when not the default), for
      ``@authority``.
    path: The target path, for ``@path``.
    query: The query string without ``?``, for ``@query``; None when the
      target has none.
    status: The response status, for ``@status``.
    headers: The message's header fields.

  Returns:
    The base, as the bytes the signature is computed over.

  Raises:
    SignatureError: For a component this implementation can't derive, a
      component parameter it doesn't support, or a header the message lacks.

  """
  lines = []
  for identifier, name in zip(
    sig_input.components, sig_input.names, strict=True
  ):
    if ";" in identifier:
      raise SignatureError(
        f"component parameters are not supported: {identifier}"
      )
    if name.startswith("@"):
      derived = {
        "@method": (method or "").upper() or None,
        "@authority": (authority or "").lower() or None,
        "@path": path,
        "@query": None if query is None else f"?{query}",
        "@status": None if status is None else str(status),
      }
      if name not in derived:
        raise SignatureError(f"derived component {name} is not supported")
      value = derived[name]
      if value is None:
        raise SignatureError(f"the message has no value for {name}")
    else:
      value = _header(headers, name)
      if value is None:
        raise SignatureError(f"the message has no {name} header to cover")
    lines.append(f'"{name}": {value}')
  lines.append(f'"@signature-params": {sig_input.raw}')
  return "\n".join(lines).encode("utf-8")


# ── verification (signatures.md, verify_rest_request) ──────────────────────


def covers_webhook_identity(version: str | None) -> bool:
  """Whether the version a server implements must sign webhook-id/-timestamp.

  Args:
    version: The version the server advertises for the capability, a date
      string such as ``2026-04-08``; None or a non-date reads as the draft.

  """
  if version is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", version):
    return True
  return version >= WEBHOOK_IDENTITY_COVERED_SINCE


def required_components(
  *,
  has_query: bool,
  has_body: bool,
  headers: Mapping[str, str],
  version: str | None = None,
) -> list[str]:
  """Return the components a request signature must cover (step 2b).

  Args:
    has_query: Whether the target has a query string.
    has_body: Whether the request carries a body.
    headers: The request headers; the conditional components depend on
      which of them are present.
    version: The version the signer implements, for the webhook identity
      headers (see ``covers_webhook_identity``).

  """
  required = ["@method", "@authority", "@path"]
  if has_query:
    required.append("@query")
  if has_body:
    required += ["content-digest", "content-type"]
  for header in ("idempotency-key", "ucp-agent", "signature-agent"):
    if _header(headers, header) is not None:
      required.append(header)
  if covers_webhook_identity(version):
    for header in ("webhook-id", "webhook-timestamp"):
      if _header(headers, header) is not None:
        required.append(header)
  return required


@dataclasses.dataclass(frozen=True)
class Verification:
  """The outcome of verifying one request.

  Attributes:
    ok: Whether a signature authenticated the request.
    reason: ``verified``, or why not: ``unsigned``, ``malformed``,
      ``key_not_found``, ``algorithm_unsupported``, ``coverage_insufficient``,
      ``digest_mismatch`` or ``signature_invalid``, with detail after a
      colon where there is any.
    label: The signature label the outcome is about, if one was parsed.
    keyid: The ``keyid`` that signature named, if any.
    covered: The component names that signature covers.

  """

  ok: bool
  reason: str
  label: str | None = None
  keyid: str | None = None
  covered: tuple[str, ...] = ()


def verify_request(
  *,
  method: str,
  authority: str,
  path: str,
  query: str | None,
  headers: Mapping[str, str],
  body: bytes,
  keys: Iterable[Mapping[str, Any]],
  version: str | None = None,
) -> Verification:
  """Verify a request's signature against a signer's published keys.

  Follows ``verify_rest_request`` in signatures.md. With several signatures,
  the first that verifies authenticates the request; otherwise the outcome
  of the last one tried is returned (RFC 9421 §4.3).

  Args:
    method: The request method.
    authority: The host the request was sent to, as the signer saw it.
    path: The request path.
    query: The query string without ``?``, or None.
    headers: The request headers as received.
    body: The raw body bytes, empty when there is none.
    keys: The signer's published JWKs (``profile_keys``).
    version: The version the signer implements, for the coverage rule.

  Returns:
    The verification outcome.

  """
  signature_input = _header(headers, "signature-input")
  signature = _header(headers, "signature")
  if not signature_input and not signature:
    return Verification(False, "unsigned")
  inputs = parse_signature_input(signature_input or "")
  values = parse_signature(signature or "")
  if not inputs:
    return Verification(
      False, "malformed: Signature-Input is not a dictionary of inner lists"
    )
  usable = [key for key in keys if sig_capable(key)]
  outcome = Verification(False, "malformed")
  for label, sig_input in inputs.items():
    outcome = _verify_one(
      sig_input,
      values.get(label),
      method=method,
      authority=authority,
      path=path,
      query=query,
      headers=headers,
      body=body,
      keys=usable,
      version=version,
    )
    if outcome.ok:
      return outcome
  return outcome


def _verify_one(
  sig_input: SignatureInput,
  signature: bytes | None,
  *,
  method: str,
  authority: str,
  path: str,
  query: str | None,
  headers: Mapping[str, str],
  body: bytes,
  keys: list[Mapping[str, Any]],
  version: str | None,
) -> Verification:
  """Verify a single candidate signature; see ``verify_request``."""
  label, keyid, covered = sig_input.label, sig_input.keyid, sig_input.names

  def fail(reason: str) -> Verification:
    return Verification(False, reason, label, keyid, covered)

  if signature is None:
    return fail(f"malformed: Signature has no member {label!r}")
  if not keyid:
    return fail("malformed: Signature-Input carries no keyid")
  jwk = next((key for key in keys if key.get("kid") == keyid), None)
  if jwk is None:
    return fail(f"key_not_found: no signature-capable key with kid {keyid!r}")
  try:
    algorithm = algorithm_for(jwk)
    public_key = public_key_from_jwk(jwk)
  except SignatureError as error:
    return fail(f"algorithm_unsupported: {error}")
  missing = [
    name
    for name in required_components(
      has_query=query is not None,
      has_body=bool(body),
      headers=headers,
      version=version,
    )
    if name not in covered
  ]
  if missing:
    return fail(f"coverage_insufficient: not covered: {', '.join(missing)}")
  if "content-digest" in covered and _header(
    headers, "content-digest"
  ) != content_digest(body):
    return fail(
      "digest_mismatch: Content-Digest is not the sha-256 of the body"
    )
  try:
    base = signature_base(
      sig_input,
      method=method,
      authority=authority,
      path=path,
      query=query,
      headers=headers,
    )
  except SignatureError as error:
    return fail(f"malformed: {error}")
  try:
    if algorithm == "EdDSA":
      public_key.verify(signature, base)
    else:
      size = _coordinate_size(algorithm)
      if len(signature) != 2 * size:
        return fail(
          f"signature_invalid: {algorithm} signatures are r||s, "
          f"{2 * size} bytes, got {len(signature)}"
        )
      der = asymmetric.encode_dss_signature(
        int.from_bytes(signature[:size], "big"),
        int.from_bytes(signature[size:], "big"),
      )
      public_key.verify(der, base, ec.ECDSA(_hash_for(algorithm)))
  except InvalidSignature:
    return fail("signature_invalid: the value does not verify over the base")
  return Verification(True, "verified", label, keyid, covered)


# ── signing (signatures.md, sign_rest_request) ─────────────────────────────


def sign_request(
  key: SigningKey,
  *,
  method: str,
  authority: str,
  path: str,
  query: str | None,
  headers: Mapping[str, str],
  body: bytes | None,
  created: int | None = None,
  label: str = "sig1",
) -> dict[str, str]:
  """Sign a request the way signatures.md has a platform sign it.

  Args:
    key: The platform's signing key.
    method: The request method.
    authority: The target host (and port when not the default).
    path: The target path.
    query: The query string without ``?``, or None.
    headers: The request's headers; ``UCP-Agent`` and ``Idempotency-Key``
      are covered when present, ``Content-Type`` when there is a body.
    body: The raw body, or None.
    created: The ``created`` parameter; now by default.
    label: The signature label.

  Returns:
    The headers to add: ``Content-Digest`` when there is a body,
    ``Signature-Input`` and ``Signature``.

  """
  added: dict[str, str] = {}
  all_headers = dict(headers)
  components = ["@method", "@authority", "@path"]
  if query is not None:
    components.append("@query")
  if _header(headers, "ucp-agent") is not None:
    components.append("ucp-agent")
  for header in ("idempotency-key", "webhook-id", "webhook-timestamp"):
    if _header(headers, header) is not None:
      components.append(header)
  if body:
    added["Content-Digest"] = content_digest(body)
    all_headers["Content-Digest"] = added["Content-Digest"]
    if _header(headers, "content-type") is None:
      all_headers["Content-Type"] = "application/json"
    components += ["content-digest", "content-type"]
  inner = " ".join(f'"{name}"' for name in components)
  raw = f'({inner});created={created or int(time.time())};keyid="{key.kid}"'
  sig_input = SignatureInput(
    label, tuple(f'"{name}"' for name in components), tuple(components), {}, raw
  )
  base = signature_base(
    sig_input,
    method=method,
    authority=authority,
    path=path,
    query=query,
    headers=all_headers,
  )
  if key.algorithm == "EdDSA":
    value = key.private_key.sign(base)
  else:
    der = key.private_key.sign(base, ec.ECDSA(_hash_for(key.algorithm)))
    r, s = asymmetric.decode_dss_signature(der)
    size = _coordinate_size(key.algorithm)
    value = r.to_bytes(size, "big") + s.to_bytes(size, "big")
  added["Signature-Input"] = f"{label}={raw}"
  added["Signature"] = f"{label}=:{base64.b64encode(value).decode()}:"
  return added


class RequestSigner(httpx.Auth):
  """An httpx auth that signs every request with the platform's key.

  Set on the suite's client so each request the suite makes carries the
  RFC 9421 headers a business can verify against the key the mock agent
  profile publishes. A request sent with ``auth=None`` goes unsigned.
  """

  requires_request_body = True

  def __init__(self, key: SigningKey):
    """Keep the key to sign with."""
    self.key = key

  def auth_flow(self, request: httpx.Request):
    """Add Content-Digest, Signature-Input and Signature; send once."""
    url = request.url
    query = url.query.decode("ascii") if url.query else None
    added = sign_request(
      self.key,
      method=request.method,
      authority=url.netloc.decode("ascii"),
      path=url.raw_path.split(b"?", 1)[0].decode("ascii"),
      query=query,
      headers=dict(request.headers),
      body=request.content or None,
    )
    for name, value in added.items():
      request.headers[name] = value
    yield request
