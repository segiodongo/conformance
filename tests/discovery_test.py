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

"""Unit tests for framework.discovery: the profile's capabilities."""

import unittest

from framework.discovery import UcpProfile

DICT_PROFILE = {
  "ucp": {
    "version": "2026-04-08",
    "capabilities": {
      "dev.ucp.shopping.checkout": [{"version": "2026-04-08"}],
      "dev.ucp.shopping.cart": [{"version": "2026-04-08"}],
      "dev.ucp.shopping.discount": [
        {
          "version": "2026-04-08",
          "extends": ["dev.ucp.shopping.checkout", "dev.ucp.shopping.cart"],
        }
      ],
      "dev.ucp.shopping.fulfillment": [
        {"version": "2026-04-08", "extends": "dev.ucp.shopping.checkout"}
      ],
    },
  }
}

LIST_PROFILE = {
  "ucp": {
    "version": "2026-04-08",
    "capabilities": [
      {"name": "dev.ucp.shopping.checkout", "version": "2026-04-08"},
      {
        "name": "dev.ucp.shopping.discount",
        "version": "2026-04-08",
        "extends": ["dev.ucp.shopping.checkout"],
      },
    ],
  }
}


class ExtendsTest(unittest.TestCase):
  """What a capability entry's `extends` says, in both profile shapes."""

  def test_dictionary_profile_lists_each_extended_capability(self) -> None:
    """A list of names is kept in order; a bare string counts as one."""
    profile = UcpProfile(DICT_PROFILE)
    self.assertEqual(
      profile.capabilities["dev.ucp.shopping.discount"].extends,
      ("dev.ucp.shopping.checkout", "dev.ucp.shopping.cart"),
    )
    self.assertEqual(
      profile.capabilities["dev.ucp.shopping.fulfillment"].extends,
      ("dev.ucp.shopping.checkout",),
    )
    self.assertEqual(profile.capabilities["dev.ucp.shopping.cart"].extends, ())

  def test_list_profile_is_read_the_same_way(self) -> None:
    """The older list shape yields the same tuples."""
    profile = UcpProfile(LIST_PROFILE)
    self.assertEqual(
      profile.capabilities["dev.ucp.shopping.discount"].extends,
      ("dev.ucp.shopping.checkout",),
    )

  def test_extension_extends(self) -> None:
    """extension_extends is true only for an advertised, matching pair."""
    profile = UcpProfile(LIST_PROFILE)
    discount = "dev.ucp.shopping.discount"
    self.assertTrue(
      profile.extension_extends(discount, "dev.ucp.shopping.checkout")
    )
    self.assertFalse(
      profile.extension_extends(discount, "dev.ucp.shopping.cart")
    )
    self.assertFalse(
      profile.extension_extends("dev.ucp.shopping.nothing", "dev.ucp.x")
    )


if __name__ == "__main__":
  unittest.main()
