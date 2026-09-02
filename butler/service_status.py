"""Read-only launchd and log status collection for the Butler dashboard."""

from __future__ import annotations

import plistlib
import subprocess
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHD_DIR = REPO_ROOT / "launchd"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def _launchctl_jobs() -> dict[str, tuple[str, str]]:
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, check=False
    )
    jobs: dict[str, tuple[str, str]] = {}
    for line in result.stdout.splitlines()[1:]:
        fields = line.split(None, 2)
        if len(fields) == 3:
            jobs[fields[2]] = (fields[0], fields[1])
    return jobs


def _schedule(data: dict) -> str:
    if "StartCalendarInterval" in data:
        values = data["StartCalendarInterval"]
        if not isinstance(values, list):
            values = [values]
        formatted = []
        for item in values:
            formatted.append(f"{item.get('Hour', '*')}:{int(item.get('Minute', 0)):02d}")
        return "Daily " + ", ".join(formatted)
    if "StartInterval" in data:
        return f"Every {int(data['StartInterval']) // 3600}h"
    if data.get("RunAtLoad"):
        return "At login"
    return "Persistent" if data.get("KeepAlive") else "On demand"


def _recent_log(path: Path) -> dict:
    if not path.exists():
        return {"timestamp": None, "message": "No log yet", "health": "unknown"}
    try:
        lines = [
            line.strip()
            for line in path.read_text(errors="replace").splitlines()
            if line.strip()
        ]
        modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError as exc:
        return {"timestamp": None, "message": f"Log unavailable: {exc}", "health": "unknown"}
    recent = lines[-40:]
    health = "error" if any("Traceback" in line or " ERROR " in line for line in recent) else "ok"
    return {
        "timestamp": modified.isoformat(timespec="minutes"),
        "message": lines[-1][-240:] if lines else "Empty log",
        "health": health,
    }


def collect_status() -> dict:
    loaded = _launchctl_jobs()
    services = []
    for source in sorted(LAUNCHD_DIR.glob("*.plist")):
        with source.open("rb") as handle:
            data = plistlib.load(handle)
        label = data["Label"]
        installed = LAUNCH_AGENTS_DIR / source.name
        pid, _exit_code = loaded.get(label, ("-", "-"))
        if pid != "-":
            status = "running"
        elif not installed.exists():
            status = "not-installed"
        elif label not in loaded:
            status = "not-loaded"
        else:
            status = "idle"
        browser = (
            "Persistent Chrome/CDP"
            if "smartfind" in label
            else "Headless Chrome per scan"
            if any(name in label for name in ("marketplace", "homes"))
            else "None"
        )
        log_path = Path(data.get("StandardErrorPath", REPO_ROOT / "logs" / f"{label}.err.log"))
        services.append(
            {
                "label": label,
                "status": status,
                "pid": None if pid == "-" else int(pid),
                "installed": installed.exists(),
                "schedule": _schedule(data),
                "command": " ".join(data.get("ProgramArguments", [])),
                "browser": browser,
                "log": _recent_log(log_path),
            }
        )
    return {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "services": services}
