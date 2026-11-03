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

"""Transport checks the REST bindings share: strict JSON bodies and TLS 1.3.

Every shopping REST page states the same two Protocol Fundamentals. Content
Types: request and response bodies are `application/json`, and MUST be valid
JSON as RFC 8259 specifies it. Transport Security: all REST endpoints MUST
be served over HTTPS with TLS 1.3 as the minimum version.

`json_body_problems` reads a response the way RFC 8259 asks a receiver to:
UTF-8 without a byte order mark (section 8.1), the JSON grammar with no
extensions (so `NaN` and `Infinity`, which some encoders emit, are not
values; section 6), served under a JSON media type. `probe_tls` asks a host
two questions: does a TLS 1.3 handshake complete, and is a handshake capped
at TLS 1.2 refused? A server meets "minimum TLS version 1.3" only when both
answers are yes.
"""

import codecs
import dataclasses
import json
import socket
import ssl
from typing import Any

JSON_MEDIA_TYPE = "application/json"

# Responses that carry no body by definition (RFC 9110).
_BODILESS_STATUSES = frozenset({204, 205, 304})


def media_type(content_type: str | None) -> str:
  """Return the media type of a Content-Type value, lower-cased."""
  return (content_type or "").split(";", 1)[0].strip().lower()


def is_json_media_type(content_type: str | None) -> bool:
  """Whether a Content-Type names JSON: application/json or a +json type."""
  kind = media_type(content_type)
  return kind == JSON_MEDIA_TYPE or kind.endswith("+json")


def _reject_constant(name: str) -> Any:
  """Refuse the non-finite numbers Python's parser would otherwise accept."""
  raise ValueError(f"{name} is not a JSON value (RFC 8259 section 6)")


def json_text_problems(raw: bytes) -> list[str]:
  """Return why a body is not valid RFC 8259 JSON text; none when it is.

  Args:
    raw: The body's bytes, as received.

  Returns:
    One line per problem found, in reading order: encoding first, then the
    grammar.

  """
  if raw.startswith(codecs.BOM_UTF8):
    return [
      "the body starts with a byte order mark (RFC 8259 section 8.1: "
      "implementations MUST NOT add one)"
    ]
  try:
    text = raw.decode("utf-8")
  except UnicodeDecodeError as error:
    return [f"the body is not UTF-8 (RFC 8259 section 8.1): {error}"]
  try:
    json.loads(text, parse_constant=_reject_constant)
  except ValueError as error:
    return [f"the body is not valid JSON (RFC 8259): {error}"]
  return []


def json_body_problems(
  status_code: int, content_type: str | None, raw: bytes
) -> list[str]:
  """Return why a response is not a valid JSON body served as JSON.

  Args:
    status_code: The response status.
    content_type: The response's Content-Type header, if any.
    raw: The body's bytes, as received.

  Returns:
    One line per problem; none for a well-formed JSON response, or for a
    response that carries no body by definition (204, 205, 304).

  """
  if status_code in _BODILESS_STATUSES and not raw:
    return []
  problems = []
  if not is_json_media_type(content_type):
    problems.append(
      f"Content-Type is {media_type(content_type) or 'missing'!r}, not "
      f"{JSON_MEDIA_TYPE} (Content Types: Response application/json)"
    )
  if not raw:
    problems.append("the body is empty, which is not a JSON text (RFC 8259)")
    return problems
  return problems + json_text_problems(raw)


@dataclasses.dataclass(frozen=True)
class TlsProbe:
  """What two handshakes with a host found.

  Attributes:
    host: The host probed.
    port: The port probed.
    tls13: Whether a handshake that offers only TLS 1.3 completed.
    tls13_detail: The version negotiated, or the error the handshake raised.
    tls12_refused: Whether a handshake that offers at most TLS 1.2 failed,
      as it must when 1.3 is the minimum.
    tls12_detail: The version negotiated when it wasn't refused, or the
      error that refused it.

  """

  host: str
  port: int
  tls13: bool
  tls13_detail: str
  tls12_refused: bool
  tls12_detail: str

  @property
  def minimum_is_1_3(self) -> bool:
    """Whether the host serves TLS 1.3 and nothing older."""
    return self.tls13 and self.tls12_refused


def _handshake(
  host: str,
  port: int,
  minimum: ssl.TLSVersion,
  maximum: ssl.TLSVersion,
  timeout: float,
) -> str:
  """Complete a TLS handshake within a version window; return the version.

  The certificate is not verified here: the question is which protocol
  versions the host speaks, and the suite's HTTP client verifies the
  certificate on every request it makes.
  """
  context = ssl.create_default_context()
  context.check_hostname = False
  context.verify_mode = ssl.CERT_NONE
  context.minimum_version = minimum
  context.maximum_version = maximum
  with (
    socket.create_connection((host, port), timeout=timeout) as sock,
    context.wrap_socket(sock, server_hostname=host) as tls,
  ):
    return tls.version() or "unknown"


def probe_tls(host: str, port: int = 443, timeout: float = 10.0) -> TlsProbe:
  """Probe a host for TLS 1.3 support and for refusing TLS 1.2.

  Args:
    host: The host to connect to.
    port: The port, 443 by default.
    timeout: Seconds to allow each connection.

  Returns:
    The outcome of both handshakes.

  """
  try:
    version = _handshake(
      host, port, ssl.TLSVersion.TLSv1_3, ssl.TLSVersion.TLSv1_3, timeout
    )
    tls13, tls13_detail = version == "TLSv1.3", f"negotiated {version}"
  except (ssl.SSLError, OSError) as error:
    tls13, tls13_detail = False, f"handshake failed: {error}"
  try:
    version = _handshake(
      host, port, ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_2, timeout
    )
    tls12_refused, tls12_detail = False, f"negotiated {version}"
  except ssl.SSLError as error:
    tls12_refused, tls12_detail = True, f"refused: {error}"
  except OSError as error:
    # A host that drops the connection on an old client hello refuses too;
    # the detail says how, in case it was a network failure instead.
    tls12_refused, tls12_detail = True, f"connection failed: {error}"
  return TlsProbe(host, port, tls13, tls13_detail, tls12_refused, tls12_detail)
