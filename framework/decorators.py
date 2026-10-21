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

"""Decorators for capability filtering and specification traceability."""

from collections.abc import Callable
import dataclasses
import inspect
from typing import TypeVar
import unittest

from framework.requirements import ID_RE

T = TypeVar("T")

# Where a marker is kept on the test method it labels.
MARKER_ATTRIBUTE = "_spec_marker"


def requires_capability(
  capability_name: str, min_version: str | None = None
) -> Callable[[T], T]:
  """Mark a test class or test method as requiring a specific UCP capability.

  If the server under test does not advertise the capability, skip the test.
  Requirements accumulate down the class hierarchy: a decorated subclass
  needs its own capability and everything its base classes declared.

  Args:
    capability_name: The name of the capability.
    min_version: Optional minimum version string.

  Returns:
    The decorated class or method.

  """

  def decorator(obj: T) -> T:
    # `getattr` sees requirements inherited from base classes; copy them so
    # the base class's own set is never mutated through a subclass.
    inherited = getattr(obj, "_required_capabilities", set())
    obj._required_capabilities = set(inherited) | {
      (capability_name, min_version)
    }
    return obj

  return decorator


@dataclasses.dataclass(frozen=True)
class SpecMarker:
  """What a test says about the specification rules it checks.

  Attributes:
    kind: "spec_assert", "no_rule" or "scenario".
    rule_ids: For "spec_assert", the IDs of the rules the test checks.
    reason: For "no_rule" and "scenario", why the test cites no rule.

  """

  kind: str
  rule_ids: tuple[str, ...] = ()
  reason: str = ""


def spec_assert(*rule_ids: str) -> Callable[[T], T]:
  """Mark a test as checking one or more specification rules.

  Rules are cited by their stable ID in the requirements catalog, such as
  `REQ-SHOPPING-CHECKOUT-0133`. Cite the rules the test's assertions check,
  not every rule the test passes through on the way. Like the other markers,
  this only labels the test: it runs exactly as before. A test takes one
  marker.

  Args:
    *rule_ids: The IDs of the rules the test checks.

  Returns:
    A decorator for a test method.

  Raises:
    ValueError: If no ID is given, or an ID is malformed or given twice.

  """
  if not rule_ids:
    raise ValueError("spec_assert needs at least one rule ID")
  malformed = [
    rule_id
    for rule_id in rule_ids
    if not isinstance(rule_id, str) or not ID_RE.match(rule_id)
  ]
  if malformed:
    raise ValueError(f"spec_assert: malformed rule IDs {malformed}")
  repeated = sorted(
    {rule_id for rule_id in rule_ids if rule_ids.count(rule_id) > 1}
  )
  if repeated:
    raise ValueError(f"spec_assert: rule IDs given twice {repeated}")
  return _label(SpecMarker("spec_assert", rule_ids=tuple(rule_ids)))


def no_rule(reason: str) -> Callable[[T], T]:
  """Mark a test whose behaviour the spec describes without a keyword.

  For a test of behaviour the specification describes, but not with an
  RFC 2119 keyword, so the catalog has no rule for it.

  Args:
    reason: Where and how the specification describes the behaviour.

  Returns:
    A decorator for a test method.

  Raises:
    ValueError: If the reason is empty.

  """
  return _label(SpecMarker("no_rule", reason=_reason("no_rule", reason)))


def scenario(reason: str) -> Callable[[T], T]:
  """Mark a test that checks a store's own data rather than a spec rule.

  For a test built on one store's products or settings, such as a
  free-shipping threshold, whose expected outcome no rule decides.

  Args:
    reason: What the test checks instead of a rule.

  Returns:
    A decorator for a test method.

  Raises:
    ValueError: If the reason is empty.

  """
  return _label(SpecMarker("scenario", reason=_reason("scenario", reason)))


def spec_marker(
  test: Callable[..., object] | unittest.TestCase,
) -> SpecMarker | None:
  """Return the marker a test carries, or None if it has none.

  Args:
    test: A test method, or a test case as unittest loads it.

  Returns:
    The test's marker, or None.

  """
  if isinstance(test, unittest.TestCase):
    test = getattr(test, test._testMethodName)
  return getattr(test, MARKER_ATTRIBUTE, None)


def _reason(kind: str, reason: str) -> str:
  """Return a marker's reason, which must not be empty."""
  if not isinstance(reason, str) or not reason.strip():
    raise ValueError(f"{kind} needs a reason")
  return reason.strip()


def _label(marker: SpecMarker) -> Callable[[T], T]:
  """Return a decorator that attaches a marker to a test method."""

  def decorator(func: T) -> T:
    if not inspect.isfunction(func):
      raise TypeError(f"{marker.kind} labels a test method, not {func!r}")
    earlier = getattr(func, MARKER_ATTRIBUTE, None)
    if earlier is not None:
      raise ValueError(
        f"{func.__qualname__} already has a {earlier.kind} marker; a test "
        "takes one marker"
      )
    setattr(func, MARKER_ATTRIBUTE, marker)
    return func

  return decorator
