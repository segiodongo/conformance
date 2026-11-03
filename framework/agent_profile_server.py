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

"""Background mock agent server serving agent profile configurations."""

import json
from pathlib import Path
import threading
import time
from typing import Any
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from framework.constants import DEFAULT_UCP_VERSION
import httpx
import uvicorn

# The agent profile served to the merchant under test. It is shipped with the
# package (see `only-include` in pyproject.toml) and declares the release and
# webhook endpoint through the `{ucp_version}` / `{webhook_port}` placeholders.
DEFAULT_PROFILE_TEMPLATE = (
  Path(__file__).resolve().parent.parent / "shopping-agent-test.json"
)


class AgentProfileServer:
  """Serves a test agent profile with dynamic port resolution."""

  PROFILE_PATH = "/profiles/agent.json"
  LEGACY_PROFILE_PATH = "/profiles/shopping-agent.json"

  def __init__(
    self,
    *,
    port: int,
    webhook_port: int,
    ucp_version: str = DEFAULT_UCP_VERSION,
    profile_template_path: str | None = None,
    profile_dict: dict[str, Any] | None = None,
    webhook_base_url: str | None = None,
    keys: list[dict[str, Any]] | None = None,
  ):
    """Initialize AgentProfileServer with port and profile configuration.

    Args:
      port: The HTTP port to bind to.
      webhook_port: The mock webhook port to interpolate into endpoints.
      ucp_version: The UCP release the profile should declare, driven by the
        merchant's conformance input so the suite advertises the release the
        server under test targets.
      profile_template_path: Optional path to a JSON profile template.
      profile_dict: Optional dictionary to serialize as the profile.
      webhook_base_url: A public address that reaches the mock webhook
        server, such as a tunnel, for a server under test that cannot reach
        this machine's localhost. It replaces
        http://localhost:{webhook_port} in the profile.
      keys: The platform's public signing keys (JWKs) to publish, so the
        business under test can verify the suite's request signatures. They
        are served as `keys[]`, the canonical profile field, and as
        `signing_keys[]`, its name through the 2026-08-25 release.

    """
    self.port = port
    self.webhook_port = webhook_port
    self.ucp_version = ucp_version
    self.webhook_base_url = webhook_base_url
    self.keys = list(keys or [])
    self.app = FastAPI()

    if profile_dict is not None:
      self._profile_template = json.dumps(profile_dict)
    else:
      template = (
        Path(profile_template_path)
        if profile_template_path
        else DEFAULT_PROFILE_TEMPLATE
      )
      if not template.is_file():
        raise FileNotFoundError(f"Agent profile template not found: {template}")
      self._profile_template = template.read_text(encoding="utf-8")

    self._setup_routes()
    self._server: uvicorn.Server | None = None
    self._thread: threading.Thread | None = None

  def render_profile(self) -> dict[str, Any]:
    """Return the profile, with its placeholders filled in."""
    content = self._profile_template
    if self.webhook_base_url:
      content = content.replace(
        "http://localhost:{webhook_port}", self.webhook_base_url.rstrip("/")
      )
    content = content.replace("{webhook_port}", str(self.webhook_port))
    content = content.replace("{ucp_version}", self.ucp_version)
    profile = json.loads(content)
    if self.keys:
      profile["keys"] = self.keys
      profile["signing_keys"] = self.keys
    return profile

  def _setup_routes(self) -> None:
    async def _respond_profile() -> JSONResponse:
      return JSONResponse(content=self.render_profile())

    self.app.get(self.PROFILE_PATH, response_model=None)(_respond_profile)
    self.app.get(self.LEGACY_PROFILE_PATH, response_model=None)(
      _respond_profile
    )

    @self.app.get("/healthz")
    async def health_check() -> dict[str, str]:
      return {"status": "ok"}

  def start(self) -> None:
    """Start the mock agent server in a background thread."""
    config = uvicorn.Config(
      self.app, host="0.0.0.0", port=self.port, log_level="error"
    )
    self._server = uvicorn.Server(config)
    self._thread = threading.Thread(target=self._server.run, daemon=True)
    self._thread.start()

    for _ in range(50):
      try:
        with httpx.Client() as client:
          resp = client.get(f"http://localhost:{self.port}/healthz")
          if resp.status_code == 200:
            return
      except httpx.ConnectError:
        time.sleep(0.05)
    raise RuntimeError(
      f"AgentProfileServer failed to start on port {self.port}"
    )

  def stop(self) -> None:
    """Stop the background mock agent server."""
    if self._server is not None:
      self._server.should_exit = True
      if self._thread is not None:
        self._thread.join(timeout=3)
