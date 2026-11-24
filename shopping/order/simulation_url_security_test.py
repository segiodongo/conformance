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

"""Tests for the access control on the request that updates an order.

The specification defines no operation that updates an order, so a server
that lets the suite ask for one declares the request in its fixtures
(`order_update_request`; see `shopping/fixtures/README.md`). The sample
server's is its `/testing/simulate-shipping/{order_id}` hook, which takes
the secret given with --simulation_secret in a `Simulation-Secret` header.
Such a request is a test harness of the implementation under test, not part
of the UCP specification, which is why these tests live with the shopping
order suite rather than in `core/`.
"""

from absl.testing import absltest
from framework.decorators import requires_capability, scenario
import httpx
from shopping.base import (
  SIMULATION_SECRET_PLACEHOLDER,
  ShoppingIntegrationTestBase,
  describe_order_update_request,
  uses_simulation_secret,
)


@requires_capability("dev.ucp.shopping.order")
class SecurityTest(ShoppingIntegrationTestBase):
  """Tests for security controls.

  The request is the server's own, and these tests are about the secret it
  takes. A business that declares no `order_update_request`, or one that
  carries no `{simulation_secret}`, has nothing to show here, so each test
  skips as "no trigger". So does a server that answers the declared request
  with 404 or 405, which doesn't have it; one that refuses the configured
  secret skips as an environment problem. None of that counts three
  failures against a store that never had the hook.
  """

  def _order_to_update(self) -> str:
    """Return a completed order, where the declared request takes a secret."""
    declared = self.require_trigger("order_update_request")
    if not uses_simulation_secret(declared):
      self.skip_no_trigger(
        f"an order_update_request that carries {SIMULATION_SECRET_PLACEHOLDER}"
        ", the secret these tests leave out or get wrong"
      )
    return self.create_completed_order()

  def _assert_refused(self, order_id: str, response: httpx.Response) -> None:
    """Assert the server refused a request without the right secret."""
    if response.status_code in (404, 405):
      # Either the server has no such endpoint, or one that reveals
      # nothing to a caller without the secret. The request as declared
      # tells which: it skips the test unless the server answers it.
      self.request_order_update(order_id)
      return
    sent = describe_order_update_request(
      self.require_trigger("order_update_request")
    )
    self.assertIn(
      response.status_code,
      (401, 403),
      msg=(
        f"{sent} was not refused without the right secret: the server "
        f"answered {response.status_code}, not 401 or 403. Response: "
        f"{response.text}"
      ),
    )

  @scenario(
    "the store's own request for updating an order (order_update_request "
    "in the fixtures) and the secret it takes, a test harness of the "
    "implementation under test that no specification page describes"
  )
  def test_simulation_endpoint_missing_header(self):
    """Test access without the secret is refused (401 or 403)."""
    order_id = self._order_to_update()
    response = self.send_order_update_request(order_id, None)
    self._assert_refused(order_id, response)

  @scenario(
    "the store's own request for updating an order (order_update_request "
    "in the fixtures) and the secret it takes, a test harness of the "
    "implementation under test that no specification page describes"
  )
  def test_simulation_endpoint_incorrect_secret(self):
    """Test access with an incorrect secret is refused (401 or 403)."""
    order_id = self._order_to_update()
    response = self.send_order_update_request(
      order_id, "for-sure-incorrect-secret"
    )
    self._assert_refused(order_id, response)

  @scenario(
    "the store's own request for updating an order (order_update_request "
    "in the fixtures) and the secret it takes, a test harness of the "
    "implementation under test that no specification page describes"
  )
  def test_simulation_endpoint_correct_secret(self):
    """Test access with the correct secret is granted (2xx)."""
    order_id = self._order_to_update()
    self.request_order_update(order_id)


if __name__ == "__main__":
  absltest.main()
