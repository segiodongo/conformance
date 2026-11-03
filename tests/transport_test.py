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

"""Unit tests for framework.transport and the base assertions built on it.

The TLS assertion is exercised with the probe patched, so no network is
touched; the strict-JSON checks run on bytes built here.
"""

import codecs
import unittest
from unittest import mock

from framework import transport
from framework.base_test import BaseIntegrationTest


class JsonTextProblemsTest(unittest.TestCase):
  """json_text_problems: RFC 8259 text, read strictly."""

  def test_valid_text_has_no_problems(self) -> None:
    """Objects, arrays and bare values are all JSON texts."""
    for raw in (b'{"a": 1}', b"[1, 2]", b'"text"', b"12", b"null"):
      self.assertEqual(transport.json_text_problems(raw), [], raw)

  def test_grammar_extensions_are_problems(self) -> None:
    """Trailing commas, comments, single quotes and NaN are not JSON."""
    for raw in (
      b'{"a": 1,}',
      b'{"a": 1} // note',
      b"{'a': 1}",
      b'{"a": NaN}',
      b"[Infinity]",
      b"",
    ):
      problems = transport.json_text_problems(raw)
      self.assertTrue(problems, raw)
      self.assertIn("not valid JSON", problems[0])

  def test_nan_names_the_section(self) -> None:
    """The non-finite constants are refused with the RFC reference."""
    [problem] = transport.json_text_problems(b'{"a": NaN}')
    self.assertIn("RFC 8259 section 6", problem)

  def test_encoding_problems(self) -> None:
    """A byte order mark and non-UTF-8 bytes are each reported."""
    [bom] = transport.json_text_problems(codecs.BOM_UTF8 + b"{}")
    self.assertIn("byte order mark", bom)
    [latin] = transport.json_text_problems(b'{"a": "\xe9t\xe9"}')
    self.assertIn("not UTF-8", latin)


class JsonBodyProblemsTest(unittest.TestCase):
  """json_body_problems: the media type and the body together."""

  def test_json_body_served_as_json(self) -> None:
    """application/json and a +json type both pass with a valid body."""
    for content_type in (
      "application/json",
      "application/json; charset=utf-8",
      "application/problem+json",
      "Application/JSON",
    ):
      self.assertEqual(
        transport.json_body_problems(200, content_type, b"{}"),
        [],
        content_type,
      )

  def test_wrong_media_type_is_a_problem(self) -> None:
    """A JSON body under text/plain, or with no Content-Type, is reported."""
    [problem] = transport.json_body_problems(200, "text/plain", b"{}")
    self.assertIn("'text/plain', not application/json", problem)
    [problem] = transport.json_body_problems(200, None, b"{}")
    self.assertIn("'missing'", problem)

  def test_empty_body_is_a_problem_unless_bodiless(self) -> None:
    """An empty 200 body is not a JSON text; a 204 carries none by design."""
    problems = transport.json_body_problems(200, "application/json", b"")
    self.assertEqual(len(problems), 1)
    self.assertIn("empty", problems[0])
    self.assertEqual(transport.json_body_problems(204, None, b""), [])

  def test_problems_accumulate(self) -> None:
    """A bad media type and a bad body are both reported."""
    problems = transport.json_body_problems(500, "text/html", b"<h1>oops")
    self.assertEqual(len(problems), 2)


class Host(unittest.TestCase):
  """A stand-in test for calling the base assertions unbound."""

  def skip_environment(self, why: str) -> None:
    """Mirror BaseIntegrationTest.skip_environment without the prefix."""
    raise unittest.SkipTest(f"environment: {why}")


def probe(tls13: bool, tls12_refused: bool) -> transport.TlsProbe:
  """Build a probe result."""
  return transport.TlsProbe(
    "shop.example", 443, tls13, "detail", tls12_refused, "detail"
  )


class AssertTlsMinimum13Test(unittest.TestCase):
  """assert_tls_minimum_1_3: skip, fail or pass by scheme and probe."""

  def check(self, urls: list[str]) -> None:
    """Run the assertion unbound against a fresh host case."""
    BaseIntegrationTest.assert_tls_minimum_1_3(Host(), urls)

  def test_plaintext_on_a_local_address_skips(self) -> None:
    """A local http:// endpoint is an environment limit."""
    with self.assertRaises(unittest.SkipTest) as caught:
      self.check(["http://localhost:8182/.well-known/ucp"])
    self.assertIn("serve the server over HTTPS", str(caught.exception))

  def test_plaintext_elsewhere_fails(self) -> None:
    """A deployed http:// endpoint fails the rule."""
    with self.assertRaises(AssertionError) as caught:
      self.check(["http://shop.example/.well-known/ucp"])
    self.assertIn("not served over HTTPS", str(caught.exception))

  def test_tls_1_3_only_passes_and_probes_each_host_once(self) -> None:
    """Two URLs on one host mean one probe; both handshakes as required."""
    with mock.patch.object(
      transport, "probe_tls", return_value=probe(True, True)
    ) as probed:
      self.check(
        ["https://shop.example/.well-known/ucp", "https://shop.example/api"]
      )
    probed.assert_called_once_with("shop.example", 443)

  def test_accepting_tls_1_2_fails(self) -> None:
    """A host that still negotiates TLS 1.2 does not meet the minimum."""
    with (
      mock.patch.object(
        transport, "probe_tls", return_value=probe(True, False)
      ),
      self.assertRaises(AssertionError) as caught,
    ):
      self.check(["https://shop.example/api"])
    self.assertIn("still accepts TLS 1.2", str(caught.exception))

  def test_no_tls_1_3_fails(self) -> None:
    """A host without TLS 1.3 fails before the 1.2 question."""
    with (
      mock.patch.object(
        transport, "probe_tls", return_value=probe(False, True)
      ),
      self.assertRaises(AssertionError) as caught,
    ):
      self.check(["https://shop.example:8443/api"])
    self.assertIn(
      "does not complete a TLS 1.3 handshake", str(caught.exception)
    )
    self.assertIn("shop.example:8443", str(caught.exception))


if __name__ == "__main__":
  unittest.main()
