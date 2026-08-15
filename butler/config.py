import subprocess
import sys

from pydantic_settings import BaseSettings, SettingsConfigDict


def _tailscale_ip() -> str | None:
    """Return this machine's Tailscale IPv4 address, or None if unavailable."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BUTLER_")

    token: str
    port: int = 8787
    host: str | None = None

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
