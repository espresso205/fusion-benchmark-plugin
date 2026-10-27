"""Provider-specific fresh CLI launches; no model is contacted by probe()."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import tomllib
import urllib.parse
import uuid

PROVIDERS = ("opencode", "codex", "claude")


def credential_settings(provider, environment, source=None):
    """Import only provider connection settings; never global MCP, hooks or rules.

    Codex --ignore-user-config still reads its normal authentication store. Named
    provider tokens stay in inherited environment variables, not argv or logs.
    Claude env auth is copied into process memory without loading user settings.
    """
    env = dict(environment)
    extra = []
    metadata = {"credential_source": "existing_process_environment", "credential_values_logged": False}
    if provider == "codex":
        config_path = Path(source) if source else Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        if config_path.is_file():
            settings = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
            selected = settings.get("model_provider")
            providers = settings.get("model_providers", {})
            if selected and selected in providers:
                import re
                if not re.fullmatch(r"[A-Za-z0-9_-]+", selected):
                    raise ValueError("provider name cannot be safely overridden")
                connection = providers[selected]
                allowed = {"name", "base_url", "env_key", "wire_api", "requires_openai_auth", "request_max_retries", "stream_max_retries", "stream_idle_timeout_ms"}
                if set(connection) - allowed:
                    raise ValueError("provider requires unsupported credential settings; configure an env_key provider")
                endpoint = urllib.parse.urlsplit(connection.get("base_url", ""))
                if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                    raise ValueError("provider endpoint may contain credentials; use a clean endpoint and env_key")
                key = connection.get("env_key")
                if key and not env.get(key) and os.name == "nt":
                    # GUI processes can have an old environment snapshot. Read
                    # only the explicitly selected provider's variable by name.
                    import winreg
                    for registry_root, registry_path in ((winreg.HKEY_CURRENT_USER, "Environment"),
                            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
                        try:
                            with winreg.OpenKey(registry_root, registry_path) as handle:
                                value, _ = winreg.QueryValueEx(handle, key)
                            if isinstance(value, str) and value:
                                env[key] = value
                                break
                        except OSError:
                            continue
                extra += ["-c", "model_provider=" + json.dumps(selected)]
                for key, value in connection.items():
                    if type(value) not in (str, bool, int, float):
                        raise ValueError("unsupported provider setting type")
                    extra += ["-c", "model_providers." + selected + "." + key + "=" + json.dumps(value)]
                metadata.update(credential_source="selected_user_provider_and_existing_auth", provider=selected,
                                provider_env_key_present=bool(env.get(connection.get("env_key", ""))))
    elif provider == "claude":
        config_path = Path(source) if source else Path(env.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "settings.json"
        if config_path.is_file():
            settings = json.loads(config_path.read_text(encoding="utf-8-sig"))
            allowed = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                       "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                       "API_TIMEOUT_MS", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_AUTOUPDATER"}
            imported = 0
            for key, value in settings.get("env", {}).items():
                if key in allowed and isinstance(value, (str, int, float)):
                    env[key] = str(value)
                    imported += 1
            metadata.update(credential_source="allowlisted_user_settings_env", imported_variable_count=imported)
            if settings.get("apiKeyHelper") and not env.get("ANTHROPIC_API_KEY") and not env.get("ANTHROPIC_AUTH_TOKEN"):
                raise ValueError("apiKeyHelper is not executed automatically; supply an auth environment variable")
    return env, extra, metadata


def capabilities():
    return {name: {"fresh_process": True, "fresh_session": True, "utf8_stdin": True,
                   "budget_scope": "mcp_tools_scope", "native_tools_disabled": name != "codex",
                   "native_read_tools_possible": name == "codex", "os_sandbox": False,
                   "visual_input_verification": "requires_explicit_visual_check",
                   "model_identity_evidence": "CLI event metadata" if name != "codex" else "requested model; actual catalog identity not attested by exec JSON"}
            for name in PROVIDERS}


def executable(provider, configured=None):
    candidate = Path(configured) if configured else None
    if candidate and candidate.exists() and candidate.suffix.lower() not in (".cmd", ".ps1"):
        return str(candidate)
    found = str(candidate) if candidate and candidate.exists() else shutil.which(provider)
    if not found:
        raise FileNotFoundError(provider)
    folder = Path(found).parent
    # Prefer native binaries: cmd.exe re-parses special characters in arguments.
    choices = []
    if provider == "opencode":
        choices = [folder / "node_modules/opencode-ai/bin/opencode.exe"]
    elif provider == "codex":
        choices = list((folder / "node_modules/@openai/codex/node_modules/@openai").glob("codex-*/vendor/*/bin/codex.exe"))
    for path in choices:
        if path.is_file():
            return str(path)
    if Path(found).suffix.lower() == ".ps1":
        sibling = Path(found).with_suffix(".cmd")
        if sibling.is_file():
            return str(sibling)
    return found


def probe(provider, configured=None):
    if provider not in PROVIDERS:
        raise ValueError("unsupported provider")
    result = {"provider": provider, "available": False, **capabilities()[provider]}
    try:
        binary = executable(provider, configured)
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        version = subprocess.run([binary, "--version"], capture_output=True, timeout=15, **options)
        args = [binary, "run", "--help"] if provider == "opencode" else [binary, "exec", "--help"] if provider == "codex" else [binary, "--help"]
        help_result = subprocess.run(args, capture_output=True, timeout=15, **options)
        text = (help_result.stdout + help_result.stderr).decode("utf-8", errors="replace")
        required = {"opencode": ["--format", "--model"], "codex": ["--ignore-user-config", "--ephemeral", "--json"],
                    "claude": ["--strict-mcp-config", "--session-id", "--tools"]}[provider]
        result.update(available=version.returncode == help_result.returncode == 0 and all(flag in text for flag in required),
                      executable=binary, version=version.stdout.decode("utf-8", errors="replace").strip()[:120],
                      required_flags_present=all(flag in text for flag in required), auth="not_checked", live_model="not_called")
    except (OSError, subprocess.SubprocessError):
        result["failure"] = "executable_or_help_unavailable"
    return result


def build_invocation(spec):
    provider = spec["provider"]
    if provider not in PROVIDERS:
        raise ValueError("unsupported provider")
    if spec.get("strict_tool_isolation") and not capabilities()[provider]["native_tools_disabled"]:
        raise ValueError("provider cannot attest strict native tool exclusion")
    model = spec["model"]
    if not isinstance(model, str) or not model:
        raise ValueError("explicit model required")
    root, audit = Path(spec["workspace"]), Path(spec["audit_dir"])
    env, provider_options, auth_metadata = credential_settings(provider, spec.get("env", os.environ), spec.get("credential_settings"))
    command = spec["worker_command"]
    args = [spec["executable"]]
    tool_timeout = spec.get('tool_timeout_s', 180)
    if type(tool_timeout) is not int or not 1 <= tool_timeout <= 1800:
        raise ValueError('invalid CAD tool timeout')
    session = str(uuid.uuid4())
    if provider == "opencode":
        # Project discovery and all native tools are disabled. Global provider auth
        # may remain, while the explicit overlay disables every discovered MCP.
        mcp = {name: {"enabled": False} for name in spec.get("existing_mcp_names", [])}
        mcp["benchmark"] = {"type": "local", "command": command, "enabled": True, "timeout": tool_timeout * 1000}
        overlay = {"model": model, "small_model": model, "plugin": [], "mcp": mcp,
                   "permission": {"*": "deny", "benchmark_*": "allow"}, "snapshot": False,
                   "agent": {"benchmark": {"mode": "primary", "model": model, "prompt": "Follow only this case contract. Use benchmark MCP tools."}}}
        env.update(OPENCODE_CONFIG_CONTENT=json.dumps(overlay), OPENCODE_DISABLE_PROJECT_CONFIG="1",
                   OPENCODE_DISABLE_CLAUDE_CODE="1", OPENCODE_DISABLE_DEFAULT_PLUGINS="1")
        args += ["run", "--pure", "--format", "json", "--model", model, "--agent", "benchmark", "--dir", str(root), "--title", spec.get('cad_backend', 'fusion') + " " + session]
    elif provider == "codex":
        config = {"mcp_servers": {"benchmark": {"command": command[0], "args": command[1:], "tool_timeout_sec": tool_timeout}}}
        args += ["--no-daemon", "-a", "never", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                 "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--model", model, "--cd", str(root)]
        args += provider_options
        for feature in ("shell_tool", "apps", "browser_use", "computer_use", "image_generation", "multi_agent", "plugins", "hooks", "memories"):
            args += ["-c", "features." + feature + "=false"]
        args += ["-c", "features.skip_host_skill_discovery=true", "-c", "web_search=\"disabled\"", "-c", "project_doc_max_bytes=0",
                 "-c", "mcp_servers.benchmark.command=" + json.dumps(command[0]),
                 "-c", "mcp_servers.benchmark.args=" + json.dumps(command[1:]),
                  "-c", "mcp_servers.benchmark.tool_timeout_sec=" + str(tool_timeout), "-"]
    else:
        env['MCP_TOOL_TIMEOUT'] = str(tool_timeout * 1000)
        config_file = audit / "claude-mcp.json"
        config_file.write_text(json.dumps({"mcpServers": {"benchmark": {"type": "stdio", "command": command[0], "args": command[1:]}}}), encoding="utf-8")
        settings_file = audit / "claude-settings.json"
        settings_file.write_text(json.dumps({"disableAllHooks": True, "permissions": {"allow": ["mcp__benchmark__*"]}}), encoding="utf-8")
        args += ["--print", "--verbose", "--output-format", "stream-json", "--input-format", "text", "--model", model,
                 "--session-id", session, "--no-session-persistence", "--strict-mcp-config", "--mcp-config", str(config_file),
                 "--settings", str(settings_file), "--setting-sources", "", "--tools", "", "--disable-slash-commands",
                 "--permission-mode", "dontAsk", "--no-chrome"]
    expected_actual_model = spec.get("expected_actual_model", model)
    if provider == "claude" and model in ("sonnet", "opus", "haiku"):
        expected_actual_model = env.get("ANTHROPIC_DEFAULT_" + model.upper() + "_MODEL")
    return {"args": args, "env": env, "input": spec["prompt"].encode("utf-8"), "session_id_requested": session,
            "budget_scope": "mcp_tools_scope", "provider": provider, "auth_metadata": auth_metadata,
            "expected_actual_model": expected_actual_model}


def inspect_events(provider, events, model, expected_actual_model=None):
    errors, sessions, actual_models = [], set(), set()
    terminal = False
    for event in events:
        if not isinstance(event, dict):
            errors.append("invalid_event")
            continue
        kind = event.get("type")
        session = event.get("sessionID") or event.get("session_id") or event.get("thread_id")
        if isinstance(session, str):
            sessions.add(session)
        if kind in ("error", "turn.failed") or event.get("is_error") is True:
            errors.append("provider_error")
        if provider == "codex" and kind == "turn.completed":
            terminal = True
        if provider == "claude":
            if kind == "system" and event.get("subtype") == "init" and event.get("model"):
                actual_models.add(event["model"])
            if kind == "result":
                terminal = event.get("subtype") == "success" and event.get("is_error") is not True
        if provider == "opencode" and kind == "step_finish":
            terminal = event.get("part", {}).get("reason") == "stop"
    if len(sessions) != 1:
        errors.append("session_identity_unconfirmed")
    if not terminal:
        errors.append("terminal_unconfirmed")
    if actual_models and len(actual_models) != 1:
        errors.append("model_identity_changed")
    expected = expected_actual_model or model
    model_verified = bool(actual_models) and actual_models == {expected}
    if provider == "claude" and actual_models and actual_models != {expected}:
        errors.append("model_identity_mismatch")
    if provider == "claude" and not actual_models:
        errors.append("model_identity_missing")
    return {"verified": not errors, "errors": sorted(set(errors)), "session_id": next(iter(sessions), None),
            "requested_model": model, "actual_model": next(iter(actual_models), None),
            "expected_actual_model": expected, "model_verified": model_verified,
            "model_identity_source": "provider_metadata" if actual_models else "requested_only"}


class _WindowsProcessTree:
    """An unnamed job captures descendants before the suspended root can run."""
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': ([c.c_void_p, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
            'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
            'QueryInformationJobObject': ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
            'CloseHandle': ([w.HANDLE], w.BOOL),
            'CreateToolhelp32Snapshot': ([w.DWORD, w.DWORD], w.HANDLE),
            'Thread32First': ([w.HANDLE, c.c_void_p], w.BOOL),
            'Thread32Next': ([w.HANDLE, c.c_void_p], w.BOOL),
            'OpenThread': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            'ResumeThread': ([w.HANDLE], w.DWORD),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        class Basic(c.Structure):
            _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                        ('minimum_working_set', c.c_size_t), ('maximum_working_set', c.c_size_t),
                        ('active_limit', w.DWORD), ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Limits(c.Structure):
            _fields_ = [('basic', Basic), ('io_counters', c.c_uint64 * 6),
                        ('process_memory', c.c_size_t), ('job_memory', c.c_size_t),
                        ('peak_process_memory', c.c_size_t), ('peak_job_memory', c.c_size_t)]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        self.verified_stopped, self.remaining_processes = False, None
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not self.kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            error = c.WinError(c.get_last_error())
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise error

    def attach(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, self.w.HANDLE(int(process._handle))):
            raise self.c.WinError(self.c.get_last_error())

    def resume(self, process):
        c, w = self.c, self.w
        class ThreadEntry(c.Structure):
            _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('thread_id', w.DWORD), ('process_id', w.DWORD),
                        ('base_priority', w.LONG), ('delta_priority', w.LONG), ('flags', w.DWORD)]
        snapshot = self.kernel.CreateToolhelp32Snapshot(0x4, 0)
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        threads = []
        try:
            entry = ThreadEntry()
            entry.size = c.sizeof(entry)
            present = self.kernel.Thread32First(snapshot, c.byref(entry))
            while present:
                if entry.process_id == process.pid:
                    threads.append(entry.thread_id)
                present = self.kernel.Thread32Next(snapshot, c.byref(entry))
        finally:
            self.kernel.CloseHandle(snapshot)
        if len(threads) != 1:
            raise RuntimeError('suspended candidate primary thread identity unconfirmed')
        thread = self.kernel.OpenThread(0x2, False, threads[0])
        if not thread:
            raise c.WinError(c.get_last_error())
        try:
            if self.kernel.ResumeThread(thread) != 1:
                raise RuntimeError('candidate primary thread suspension state changed')
        finally:
            self.kernel.CloseHandle(thread)

    def stop(self):
        if self.verified_stopped:
            return
        c, w = self.c, self.w
        class Accounting(c.Structure):
            _fields_ = [('user_time', c.c_int64), ('kernel_time', c.c_int64),
                        ('period_user', c.c_int64), ('period_kernel', c.c_int64),
                        ('faults', w.DWORD), ('total', w.DWORD), ('active', w.DWORD), ('terminated', w.DWORD)]
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise c.WinError(c.get_last_error())
        deadline = time.monotonic() + 15
        while True:
            accounting = Accounting()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, c.byref(accounting), c.sizeof(accounting), None):
                raise c.WinError(c.get_last_error())
            self.remaining_processes = accounting.active
            if not accounting.active:
                if not self.kernel.CloseHandle(self.handle):
                    raise c.WinError(c.get_last_error())
                self.handle, self.verified_stopped = None, True
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('candidate job still contains active processes; reconciliation required')
            time.sleep(0.02)


class _PosixProcessTree:
    def __init__(self, process):
        self.process = process
        self.group = process.pid  # Popen starts a new session before exec.
        self.verified_stopped, self.remaining_processes = False, None

    def stop(self):
        import signal
        if self.verified_stopped:
            return
        try:
            os.killpg(self.group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Reap our root before probing its group: a killed, unreaped root can
        # otherwise keep the group observable even though it cannot execute.
        self.process.wait(timeout=15)
        deadline = time.monotonic() + 15
        while True:
            try:
                os.killpg(self.group, 0)
            except ProcessLookupError:
                self.remaining_processes, self.verified_stopped = 0, True
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('candidate process group exit cannot be confirmed')
            time.sleep(0.02)


def spawn_owned_process(arguments, **options):
    """Start only a candidate-owned process; never attach an existing CAD process."""
    if os.name != 'nt':
        options['start_new_session'] = True
        process = subprocess.Popen(arguments, **options)
        process._benchmark_owned_tree = _PosixProcessTree(process)
        return process
    tree, process = _WindowsProcessTree(), None
    try:
        options['creationflags'] = options.get('creationflags', 0) | 0x4  # CREATE_SUSPENDED
        process = subprocess.Popen(arguments, **options)
        process._benchmark_owned_tree = tree
        tree.attach(process)
        tree.resume(process)
        return process
    except BaseException:
        # If assignment failed, the never-resumed root is not in the job yet.
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=15)
        tree.stop()
        raise


def launch_fresh(spec, processes):
    from .runner import atomic, stop_tree
    invocation = build_invocation(spec)
    audit = Path(spec["audit_dir"])
    atomic(audit / "credential-source.json", invocation["auth_metadata"])
    (audit / "prompt.txt").write_bytes(invocation["input"])
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    started = time.monotonic()
    timed_out = cancelled = False
    with (audit / "session.jsonl").open("xb") as log, (audit / "stderr.log").open("xb") as error:
        process = spawn_owned_process(invocation["args"], cwd=spec["workspace"], env=invocation["env"], stdin=subprocess.PIPE,
                                   stdout=log, stderr=error, **options)
        processes.append(process)
        data = invocation["input"]
        try:
            while True:
                remaining = spec["timeout"] - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    stop_tree(process)
                    break
                if spec.get("cancel_file") and Path(spec["cancel_file"]).exists():
                    cancelled = True
                    stop_tree(process)
                    break
                try:
                    process.communicate(input=data, timeout=min(1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    data = None
        except BaseException:
            stop_tree(process)
            raise
        # communicate() proves only the CLI root exited. The job/group also owns
        # Gateway, stdio bridge and any detached descendants before submission.
        stop_tree(process)
        process_cleanup = {'schema': 'candidate-process-tree/1',
            'root_pid': process.pid, 'method': 'windows_job' if os.name == 'nt' else 'posix_session',
            'verified_stopped': process._benchmark_owned_tree.verified_stopped,
            'remaining_processes': process._benchmark_owned_tree.remaining_processes}
        atomic(audit / 'process-tree-cleanup.json', process_cleanup)
    events = []
    malformed = False
    for line in (audit / "session.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            malformed = True
    verification = inspect_events(spec["provider"], events, spec["model"], invocation["expected_actual_model"])
    if spec["provider"] == "opencode" and verification.get("session_id") and not timed_out and not cancelled:
        # Unlike the attached client stream, stored messages are authoritative.
        # Export reads only this newly created session and does not invoke a model.
        from .runner import verify_session_messages
        exported = subprocess.run([spec["executable"], "export", verification["session_id"]], cwd=spec["workspace"],
                                  env=invocation["env"], capture_output=True, timeout=30, **options)
        try:
            text = exported.stdout.decode("utf-8")
            stored = json.loads(text[text.index("{"):])
            if exported.returncode:
                raise ValueError("export failed")
            verification = verify_session_messages(stored["messages"], spec["prompt"], verification["session_id"],
                                                   smoke=spec.get("smoke", False), expected_model=spec["model"])
            verification["model_identity_source"] = "stored_session"
            verification["model_verified"] = verification["verified"]
        except (ValueError, KeyError, TypeError):
            verification.update(verified=False, errors=verification.get("errors", []) + ["stored_session_unavailable"])
    if spec.get("smoke") and spec["provider"] in ("codex", "claude"):
        final_text = ""
        for event in events:
            if spec["provider"] == "claude" and event.get("type") == "result":
                final_text = event.get("result", "")
            if spec["provider"] == "codex" and event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                final_text = event["item"].get("text", "")
        verification["smoke_tail_acknowledged"] = isinstance(final_text, str) and final_text.strip() == "SMOKE_OK"
        if not verification["smoke_tail_acknowledged"]:
            verification.update(verified=False, errors=verification.get("errors", []) + ["smoke_tail_ack_missing"])
    if malformed:
        verification.update(verified=False, errors=verification["errors"] + ["malformed_event_stream"])
    atomic(audit / "session-verification.json", verification)
    return {"returncode": process.returncode, "timed_out": timed_out, "cancelled": cancelled,
            "elapsed_s": time.monotonic() - started, "session_verification": verification,
            'process_cleanup': process_cleanup}
