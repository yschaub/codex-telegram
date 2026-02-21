"""Runtime health helpers for Codex CLI integration."""

import asyncio
import time
from typing import Any, Dict

from .session_keys import get_integration

_CACHE_KEY = "_codex_runtime_health_cache"
_CACHE_TTL_SECONDS = 30.0
_AUTH_STATUS_TIMEOUT_SECONDS = 5.0


async def get_codex_runtime_health(bot_data: Dict[str, Any]) -> Dict[str, str]:
    """Return cached Codex runtime health with lightweight auth probing."""
    now = time.monotonic()
    cached = bot_data.get(_CACHE_KEY)
    if isinstance(cached, dict) and (now - float(cached.get("timestamp", 0.0))) < _CACHE_TTL_SECONDS:
        return cached["value"]

    health: Dict[str, str] = {
        "cli": "missing",
        "cli_path": "",
        "auth": "unknown",
        "auth_detail": "Unavailable",
    }

    integration = get_integration(bot_data)
    sdk_manager = getattr(integration, "sdk_manager", None) if integration else None
    codex_path = getattr(sdk_manager, "codex_path", None)

    if not codex_path:
        health["auth_detail"] = "Codex CLI not found"
        bot_data[_CACHE_KEY] = {"timestamp": now, "value": health}
        return health

    health["cli"] = "available"
    health["cli_path"] = str(codex_path)

    try:
        process = await asyncio.create_subprocess_exec(
            codex_path,
            "login",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_AUTH_STATUS_TIMEOUT_SECONDS
        )
        output = (
            (stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode("utf-8", errors="replace"))
            .strip()
        )
        output_lower = output.lower()

        if "logged in" in output_lower and process.returncode == 0:
            health["auth"] = "logged_in"
            health["auth_detail"] = output.splitlines()[0] if output else "Logged in"
        elif "not logged in" in output_lower:
            health["auth"] = "not_logged_in"
            health["auth_detail"] = output.splitlines()[0] if output else "Not logged in"
        else:
            health["auth"] = "unknown"
            health["auth_detail"] = output.splitlines()[0] if output else f"Exit {process.returncode}"

    except asyncio.TimeoutError:
        health["auth"] = "timeout"
        health["auth_detail"] = "Timed out checking auth"
    except Exception as exc:
        health["auth"] = "unknown"
        health["auth_detail"] = str(exc)

    bot_data[_CACHE_KEY] = {"timestamp": now, "value": health}
    return health
