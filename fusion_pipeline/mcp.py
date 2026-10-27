"""Minimal synchronous MCP client for the configured local Autodesk server."""
import json
import subprocess
import urllib.parse
import urllib.request
import uuid

from .stdio_mcp import CadCallUnknown


def resolved_config(cli):
    result = subprocess.run([str(cli), "debug", "config"], capture_output=True,
                            encoding="utf-8", timeout=60)
    if result.returncode:
        raise RuntimeError("OpenCode resolved-config query failed; credentials are not logged")
    return json.loads(result.stdout)


class FusionMCP:
    def __init__(self, config, audit=None, timeout=120):
        server = config.get("mcp", {}).get("autodeskfusion", {})
        self.url = server.get("url", "")
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("autodeskfusion must resolve to a local HTTP MCP endpoint")
        if server.get("enabled") is False:
            raise ValueError("autodeskfusion is disabled")
        self.headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                        **server.get("headers", {})}
        self.audit = audit
        self.timeout = timeout
        self.unknown = False
        self._closed = False
        self.cleanup_evidence = []
        self.tools = {}
        self.server = self.rpc("initialize", {"protocolVersion": "2025-03-26",
                               "clientInfo": {"name": "fusion-benchmark-runner", "version": "0.1.0"},
                               "capabilities": {}})
        self.rpc("notifications/initialized", notification=True)
        self.tools = {x["name"]: x for x in self.rpc("tools/list")["tools"]}
        if "fusion_mcp_execute" not in self.tools:
            raise ValueError("configured Fusion MCP has no execute tool")

    def rpc(self, method, params=None, *, notification=False):
        if self.unknown:
            raise CadCallUnknown('Fusion transport cannot continue; reconciliation required')
        if self._closed:
            raise RuntimeError('Fusion transport is closed; no request dispatched')
        request_id = None if notification else uuid.uuid4().hex
        body = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            body["id"] = request_id
        if params is not None:
            body["params"] = params
        if self.audit:
            self.audit({"event": "mcp_request_started", "request_id": request_id, "method": method})
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.headers.get("Mcp-Session-Id"):
                    self.headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
                raw = response.read().decode("utf-8")
            if notification:
                return None
            if raw.lstrip().startswith("{"):
                data = json.loads(raw)
            else:
                candidates = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")]
                data = next(item for item in candidates if item.get("id") == request_id)
            if data.get("id") != request_id:
                raise RuntimeError("MCP response request ID mismatch")
            if "error" in data:
                raise RuntimeError("MCP RPC error: " + str(data["error"].get("message", "unknown")))
            if self.audit:
                self.audit({"event": "mcp_request_completed", "request_id": request_id, "method": method})
            return data["result"]
        except BaseException as error:
            self.unknown = True
            if self.audit:
                try:
                    self.audit({"event": "mcp_request_unknown", "request_id": request_id, "method": method})
                except Exception:
                    # Audit storage cannot turn an unconfirmed call into a known
                    # failure or erase the in-memory marker used by lease cleanup.
                    pass
            if not isinstance(error, Exception):
                raise
            # Don't emit URL/headers, which may contain connection credentials.
            raise CadCallUnknown(f"MCP {method} did not return a confirmed result; reconciliation required") from None

    def execute(self, script, *, readonly):
        return self.rpc("tools/call", {"name": "fusion_mcp_execute", "arguments": {
            "featureType": "script", "object": {"script": script, "readOnly": readonly}}})

    def close(self):
        # Local HTTP transport only: never close documents or terminate the CAD
        # server to obtain a clean result. Unknown state survives repeated close.
        self._closed = True
        if self.unknown:
            raise CadCallUnknown('Fusion request completion is unknown; reconciliation required')
        return {'cleanup': self.cleanup_evidence, 'reconciliation_required': False}


def extract_result(result):
    """Handle Fusion's text-wrapped JSON response without trusting unrelated strings."""
    if result.get("isError"):
        raise RuntimeError("Fusion tool returned an error: " + str(result.get("content", []))[:1200])
    candidates = [result.get("structuredContent")]
    candidates.extend(item.get("text") for item in result.get("content", []) if item.get("type") == "text")
    while candidates:
        item = candidates.pop(0)
        if isinstance(item, str):
            try:
                candidates.append(json.loads(item))
            except ValueError:
                continue
        elif isinstance(item, dict):
            if "benchmark_probe" in item or "benchmark_export" in item:
                return item
            candidates.extend(value for value in item.values() if isinstance(value, (dict, list, str)))
        elif isinstance(item, list):
            candidates.extend(item)
    raise ValueError("Fusion response did not contain the expected benchmark result")
