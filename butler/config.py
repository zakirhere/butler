import subprocess
import sys

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# The Mac App Store build of Tailscale doesn't put its CLI on PATH.
_TAILSCALE_CANDIDATES = (
    "tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
)


def _tailscale_ip() -> str | None:
    """Return this machine's Tailscale IPv4 address, or None if unavailable."""
    for binary in _TAILSCALE_CANDIDATES:
        try:
            result = subprocess.run(
                [binary, "ip", "-4"],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        ip = result.stdout.strip()
        if ip:
            return ip
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BUTLER_")

    token: str
    port: int = 8787
    host: str | None = None
    slack_bot_token: str | None = None
    slack_webhook_url: str | None = None
    slack_channel_id: str | None = None
    marketplace_enabled: bool = False
    marketplace_state_path: str = "data/marketplace-seen.json"
    marketplace_browser_profile: str = "data/facebook-profile"
    marketplace_llm_provider: str = "openai"
    marketplace_llm_model: str = "gpt-5.6-luna"
    marketplace_locations: str = "sanfrancisco,losangeles,portland,lasvegas,saltlakecity"
    marketplace_query: str = "2026 tesla model y"
    marketplace_min_price: int = 35000
    marketplace_max_price: int = 41000
    marketplace_max_detail_pages: int = 12
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")

    def resolved_host(self) -> str:
        if self.host:
            return self.host
        ip = _tailscale_ip()
        if ip is None:
            print(
                "warning: could not determine Tailscale IP "
                "(is `tailscale` installed and logged in?); "
                "falling back to 127.0.0.1, which is only reachable locally.",
                file=sys.stderr,
            )
            return "127.0.0.1"
        return ip


settings = Settings()
