"""Safe, explicit registration. No credentials or existing configuration are printed."""
import json
from pathlib import Path
import shutil
import tomllib
import uuid

NAME = "fusion_benchmark"


def definition(provider, command):
    if not command or not all(isinstance(v, str) and v for v in command):
        raise ValueError("non-empty command required")
    if provider == "opencode":
        return {"mcp": {NAME: {"type": "local", "command": command, "enabled": True}}}
    if provider in ("codex", "claude"):
        return {"mcp_servers" if provider == "codex" else "mcpServers": {NAME: {
            "command": command[0], "args": command[1:], **({"type": "stdio"} if provider == "claude" else {})}}}
    raise ValueError("unknown provider")


def register(provider, target, command):
    target = Path(target).resolve()
    incoming = definition(provider, command)
    original = target.read_bytes() if target.exists() else b""
    text = original.decode("utf-8-sig")
    if provider == "codex":
        data = tomllib.loads(text)
        wanted = incoming["mcp_servers"][NAME]
        existing = data.get("mcp_servers", {}).get(NAME)
        if existing is not None:
            if existing != wanted:
                raise ValueError("existing server differs; choose another target or remove it explicitly")
            return {"changed": False, "path": str(target)}
        suffix = "\n[mcp_servers." + NAME + "]\ncommand = " + json.dumps(wanted["command"], ensure_ascii=False)
        suffix += "\nargs = " + json.dumps(wanted["args"], ensure_ascii=False) + "\n"
        replacement = text + suffix
        tomllib.loads(replacement)
    else:
        data = json.loads(text) if text.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("configuration must be a JSON object")
        key = next(iter(incoming))
        servers = data.setdefault(key, {})
        existing = servers.get(NAME)
        wanted = incoming[key][NAME]
        if existing is not None:
            if existing != wanted:
                raise ValueError("existing server differs; choose another target or remove it explicitly")
            return {"changed": False, "path": str(target)}
        servers[NAME] = wanted
        replacement = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if target.exists():
        backup = target.with_name(target.name + ".backup-" + uuid.uuid4().hex)
        backup.write_bytes(original)
    temporary = target.with_name(target.name + ".pending-" + uuid.uuid4().hex)
    temporary.write_text(replacement, encoding="utf-8", newline="\n")
    # Refuse concurrent modifications rather than overwrite another editor.
    if (target.read_bytes() if target.exists() else b"") != original:
        temporary.unlink()
        raise ValueError("configuration changed during registration")
    temporary.replace(target)
    return {"changed": True, "path": str(target), "backup": str(backup) if backup else None}
