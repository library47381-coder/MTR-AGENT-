"""
MTR AGENT — Boss + up to 50 sub-agents orchestrator.
UI: Streamlit  •  LLM: OpenAI  •  Tools: MCP servers + n8n webhooks (Render-friendly, 180s timeout)
Modes: Interactive | Plan | Execute
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generator, List, Optional

import requests
import streamlit as st
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mtr")


# =============================================================================
# 1. Settings / Secrets
# =============================================================================

def _sget(obj, key, default=None):
    try:
        return obj[key]
    except (KeyError, FileNotFoundError, TypeError):
        return default


@dataclass
class MCPServerConfig:
    name: str
    kind: str = "mcp"
    url: str = ""
    timeout: int = 180
    headers: Dict[str, str] = field(default_factory=dict)
    sensitive: bool = False
    enabled: bool = True
    tool_path: str = ""

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "MCPServerConfig":
        return MCPServerConfig(
            name=str(d.get("name", "unnamed")),
            kind=str(d.get("kind", d.get("type", "mcp"))).lower(),
            url=str(d.get("url", "")),
            timeout=int(d.get("timeout", 180)),
            headers={str(k): str(v) for k, v in (d.get("headers") or {}).items()},
            sensitive=bool(d.get("sensitive", False)),
            enabled=bool(d.get("enabled", True)),
            tool_path=str(d.get("tool_path", "")),
        )


@dataclass
class Settings:
    openai_api_key: str
    openai_base_url: str = "https://api.openai.com/v1"
    boss_model: str = "gpt-4o"
    agent_model: str = "gpt-4o-mini"
    app_password: str = "mtr"
    max_agents: int = 50
    max_boss_steps: int = 12
    max_agent_steps: int = 8
    mcp_servers: List[MCPServerConfig] = field(default_factory=list)


def load_settings() -> Settings:
    s = st.secrets
    openai_block = _sget(s, "openai", {}) or {}
    app_block = _sget(s, "app", {}) or {}

    raw_servers = _sget(s, "mcp_servers", []) or []
    if isinstance(raw_servers, str):
        try:
            raw_servers = json.loads(raw_servers)
        except Exception as e:
            log.warning("Bad mcp_servers JSON: %s", e)
            raw_servers = []

    servers: List[MCPServerConfig] = []
    for item in raw_servers:
        try:
            servers.append(MCPServerConfig.from_dict(dict(item)))
        except Exception as e:
            log.warning("Bad MCP server entry: %s", e)

    return Settings(
        openai_api_key=str(_sget(openai_block, "api_key", "") or ""),
        openai_base_url=str(_sget(openai_block, "base_url", "https://api.openai.com/v1")),
        boss_model=str(_sget(openai_block, "boss_model", "gpt-4o")),
        agent_model=str(_sget(openai_block, "agent_model", "gpt-4o-mini")),
        app_password=str(_sget(app_block, "password", "mtr")),
        max_agents=int(_sget(app_block, "max_agents", 50)),
        max_boss_steps=int(_sget(app_block, "max_boss_steps", 12)),
        max_agent_steps=int(_sget(app_block, "max_agent_steps", 8)),
        mcp_servers=servers,
    )


# =============================================================================
# 2. MCP / n8n client
# =============================================================================

class MCPError(Exception):
    pass


class MCPClient:
    """JSON-RPC 2.0 MCP client + n8n webhook adapter. Long timeouts for Render."""

    def __init__(self, cfg: MCPServerConfig):
        self.cfg = cfg
        self._id = 0
        self._tools_cache: Optional[List[dict]] = None

    def _headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        h.update(self.cfg.headers)
        return h

    def _post(self, url: str, payload: dict) -> requests.Response:
        try:
            return requests.post(url, json=payload, headers=self._headers(),
                                 timeout=self.cfg.timeout)
        except requests.Timeout:
            raise MCPError(
                f"[{self.cfg.name}] Request timed out after {self.cfg.timeout}s. "
                f"Service may be cold-starting (Render). Try again or raise the timeout."
            )
        except requests.RequestException as e:
            raise MCPError(f"[{self.cfg.name}] Network error: {e}")

    @staticmethod
    def _parse_body(r: requests.Response) -> Any:
        text = (r.text or "").strip()
        if not text:
            return {}
        if text.startswith("event:") or text.startswith("data:"):
            for line in text.splitlines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data and data != "[DONE]":
                        try:
                            return json.loads(data)
                        except Exception:
                            continue
            return {}
        try:
            return r.json()
        except Exception:
            return {"raw": text}

    def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        r = self._post(self.cfg.url, payload)
        if r.status_code >= 400:
            raise MCPError(f"[{self.cfg.name}] HTTP {r.status_code}: {r.text[:300]}")
        data = self._parse_body(r)
        return data if isinstance(data, dict) else {"result": data}

    def list_tools(self) -> List[dict]:
        if self._tools_cache is not None:
            return self._tools_cache

        if self.cfg.kind == "n8n":
            self._tools_cache = [{
                "name": self.cfg.name,
                "description": f"n8n workflow / webhook named '{self.cfg.name}'",
                "inputSchema": {"type": "object", "additionalProperties": True},
            }]
            return self._tools_cache

        try:
            res = self._rpc("tools/list")
            tools = (res.get("result") or {}).get("tools") or []
            self._tools_cache = tools
        except MCPError as e:
            log.warning("list_tools failed for %s: %s", self.cfg.name, e)
            self._tools_cache = []
        return self._tools_cache

    def call_tool(self, tool: str, arguments: dict) -> Any:
        if self.cfg.kind == "n8n":
            url = self.cfg.url
            if self.cfg.tool_path:
                url = url.rstrip("/") + "/" + self.cfg.tool_path.lstrip("/")
            payload = {"tool": tool, "arguments": arguments, "ts": time.time()}
            r = self._post(url, payload)
            if r.status_code >= 400:
                raise MCPError(f"[{self.cfg.name}] HTTP {r.status_code}: {r.text[:300]}")
            return self._parse_body(r)

        res = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        if "error" in res:
            raise MCPError(f"[{self.cfg.name}] {res['error']}")
        return res.get("result", res)


# =============================================================================
# 3. ToolBox
# =============================================================================

class ToolBox:
    """Collects all MCP tools into OpenAI function-calling schemas."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.clients: Dict[str, MCPClient] = {}
        self.schemas: List[dict] = []
        self.meta: Dict[str, dict] = {}
        self._build()

    def _build(self):
        for cfg in self.settings.mcp_servers:
            if not cfg.enabled or not cfg.url:
                continue
            client = MCPClient(cfg)
            self.clients[cfg.name] = client
            for t in client.list_tools():
                tname = t.get("name") or "tool"
                oai_name = f"mcp__{cfg.name}__{tname}"[:64].replace(" ", "_")
                schema = (t.get("inputSchema")
                          or t.get("input_schema")
                          or t.get("parameters")
                          or {"type": "object", "properties": {}})
                self.schemas.append({
                    "type": "function",
                    "function": {
                        "name": oai_name,
                        "description": (t.get("description") or f"{tname} on {cfg.name}")[:1024],
                        "parameters": schema,
                    },
                })
                self.meta[oai_name] = {
                    "server": cfg.name,
                    "tool": tname,
                    "sensitive": cfg.sensitive,
                }

    def is_sensitive(self, oai_name: str) -> bool:
        return bool(self.meta.get(oai_name, {}).get("sensitive"))

    def server_of(self, oai_name: str) -> Optional[str]:
        return self.meta.get(oai_name, {}).get("server")

    def call(self, oai_name: str, arguments: dict) -> Any:
        meta = self.meta.get(oai_name)
        if not meta:
            return {"error": f"Unknown tool: {oai_name}"}
        client = self.clients.get(meta["server"])
        if not client:
            return {"error": f"Server not connected: {meta['server']}"}
        try:
            result = client.call_tool(meta["tool"], arguments or {})
            return _truncate(result, 12000)
        except MCPError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}


def _truncate(obj: Any, limit: int) -> Any:
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    if len(s) <= limit:
        return obj
    return s[:limit] + f"... [truncated {len(s) - limit} chars]"


# =============================================================================
# 4. Sub-agent
# =============================================================================

class SubAgent:
    """A specialist agent. Can use non-sensitive MCP tools."""

    def __init__(self, name: str, role: str, system_prompt: str,
                 model: str, client: OpenAI, toolbox: ToolBox, max_steps: int = 8):
        self.name = name
        self.role = role
        self.system_prompt = system_prompt
        self.model = model
        self.client = client
        self.toolbox = toolbox
        self.max_steps = max_steps

    def _safe_tools(self) -> List[dict]:
        return [s for s in self.toolbox.schemas
                if not self.toolbox.is_sensitive(s["function"]["name"])]

    def run(self, task: str, on_log: Optional[Callable[[str], None]] = None) -> str:
        def log_(m):
            if on_log:
                on_log(m)

        tools = self._safe_tools()
        msgs: List[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]
        log_(f"🤖 **{self.name}** started → {task[:120]}")

        for step in range(self.max_steps):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=msgs,
                    tools=tools or None,
                    tool_choice="auto" if tools else None,
                    temperature=0.3,
                )
            except Exception as e:
                return f"[agent error] {e}"
            m = resp.choices[0].message

            if m.tool_calls:
                msgs.append(_msg_dict(m))
                for tc in m.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        args = {}
                    if self.toolbox.is_sensitive(name):
                        result = {"error": "Sensitive tool blocked for sub-agent. "
                                           "Ask the boss to run it."}
                        log_(f"   ⛔ {self.name} → {name} (blocked: sensitive)")
                    else:
                        log_(f"   🛠️ {self.name} → {name}({_short(args)})")
                        result = self.toolbox.call(name, args)
                    msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, default=str)[:12000],
                    })
                continue

            text = m.content or ""
            log_(f"   ✅ {self.name} finished ({len(text)} chars)")
            return text

        return f"[{self.name}] reached max steps ({self.max_steps}) without finishing."


def _short(d: Any, n: int = 80) -> str:
    s = json.dumps(d, default=str)
    return s if len(s) <= n else s[:n] + "…"


def _msg_dict(m) -> dict:
    d = {"role": m.role}
    if m.content:
        d["content"] = m.content
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in tcs
        ]
    return d


# =============================================================================
# 5. Engine
# =============================================================================

BOSS_SYSTEM = """You are **MTR BOSS**, the orchestrator of a multi-agent system.

Your job:
1. Understand the user's task.
2. Decide the best strategy.
3. Spawn specialised sub-agents (max {max_agents}) using `spawn_agent`.
4. Delegate concrete tasks to them using `delegate_task`.
5. Call external tools yourself (MCP / n8n) via `mcp__*` functions.
6. When everything is done, reply to the user with the final consolidated answer.

Rules:
- Do NOT invent results. Only report what tools / agents actually returned.
- Prefer parallel decomposition: spawn 2–8 agents for non-trivial tasks.
- Keep agent names short and unique (e.g. researcher, writer, reviewer).
- When you have the final answer, respond in plain text (no tool call).
- Be concise and factual.
"""

PLAN_SYSTEM = """You are **MTR BOSS**. Produce a clear, numbered EXECUTION PLAN for the
user's task. Do NOT execute anything. Do NOT call tools.

Return the plan in this exact markdown format:

### Goal
<one line>

### Agents to spawn
- name — role (what it will do)

### Steps
1. ...
2. ...

### External tools / MCP calls
- <server>.<tool> — purpose (write "none" if not needed)

### Risks & approval points
- <what might need user approval>
"""


class Engine:
    def __init__(self, settings: Settings, toolbox: ToolBox, mode: str, state: dict):
        self.s = settings
        self.toolbox = toolbox
        self.mode = mode
        self.state = state
        self.client = OpenAI(api_key=settings.openai_api_key,
                             base_url=settings.openai_base_url)
        self.messages: List[dict] = state.setdefault("messages", [])
        self.agents: Dict[str, dict] = state.setdefault("agents", {})

    def _boss_tools(self) -> List[dict]:
        schemas = list(self.toolbox.schemas)

        schemas.append({
            "type": "function",
            "function": {
                "name": "spawn_agent",
                "description": "Create a new specialised sub-agent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "short unique id, e.g. researcher"},
                        "role": {"type": "string", "description": "one-line role"},
                        "system_prompt": {"type": "string", "description": "detailed instructions"},
                    },
                    "required": ["name", "role", "system_prompt"],
                },
            },
        })
        schemas.append({
            "type": "function",
            "function": {
                "name": "delegate_task",
                "description": "Send a task to a previously spawned sub-agent and get its result.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_name": {"type": "string"},
                        "task": {"type": "string"},
                    },
                    "required": ["agent_name", "task"],
                },
            },
        })
        schemas.append({
            "type": "function",
            "function": {
                "name": "list_agents",
                "description": "List all spawned sub-agents.",
                "parameters": {"type": "object", "properties": {}},
            },
        })
        schemas.append({
            "type": "function",
            "function": {
                "name": "list_external_tools",
                "description": "List all available MCP / n8n tools.",
                "parameters": {"type": "object", "properties": {}},
            },
        })
        return schemas

    def _h_spawn(self, name: str, role: str, system_prompt: str) -> dict:
        if len(self.agents) >= self.s.max_agents:
            return {"error": f"max agents ({self.s.max_agents}) reached"}
        if name in self.agents:
            return {"ok": True, "note": "agent already exists", "name": name}
        self.agents[name] = {"name": name, "role": role,
                             "system_prompt": system_prompt, "created": time.time()}
        return {"ok": True, "name": name, "total": len(self.agents)}

    def _h_delegate(self, agent_name: str, task: str, on_log) -> dict:
        meta = self.agents.get(agent_name)
        if not meta:
            return {"error": f"unknown agent '{agent_name}'. Spawn it first."}
        agent = SubAgent(
            name=meta["name"], role=meta["role"],
            system_prompt=meta["system_prompt"],
            model=self.s.agent_model, client=self.client,
            toolbox=self.toolbox, max_steps=self.s.max_agent_steps,
        )
        result = agent.run(task, on_log=on_log)
        return {"agent": agent_name, "result": result}

    def _h_list_agents(self) -> dict:
        return {"count": len(self.agents), "agents": list(self.agents.keys())}

    def _h_list_tools(self) -> dict:
        return {
            "servers": list(self.toolbox.clients.keys()),
            "tools": [s["function"]["name"] for s in self.toolbox.schemas],
        }

    def _call_tool(self, name: str, args: dict, on_log) -> Any:
        if name == "spawn_agent":
            r = self._h_spawn(**{k: args.get(k, "") for k in
                                 ("name", "role", "system_prompt")})
            on_log(f"🧬 spawn_agent → {args.get('name')}")
            return r
        if name == "delegate_task":
            return self._h_delegate(args.get("agent_name", ""),
                                    args.get("task", ""), on_log)
        if name == "list_agents":
            return self._h_list_agents()
        if name == "list_external_tools":
            return self._h_list_tools()
        if name.startswith("mcp__"):
            on_log(f"🔌 {name}({_short(args)})")
            return self.toolbox.call(name, args)
        return {"error": f"unknown function {name}"}

    def _needs_approval(self, name: str) -> bool:
        if self.mode == "interactive":
            return False
        if self.mode == "plan":
            return True
        return self.toolbox.is_sensitive(name)

    def stream(self, on_log: Callable[[str], None]) -> Generator[dict, None, None]:
        tools = self._boss_tools()
        for _ in range(self.s.max_boss_steps):
            try:
                resp = self.client.chat.completions.create(
                    model=self.s.boss_model,
                    messages=self.messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0.3,
                )
            except Exception as e:
                yield {"type": "error", "content": f"OpenAI error: {e}"}
                return

            m = resp.choices[0].message

            if m.tool_calls:
                self.messages.append(_msg_dict(m))
                for idx, tc in enumerate(m.tool_calls):
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        args = {}

                    if self._needs_approval(name):
                        self.state["pending"] = {
                            "message_index": len(self.messages) - 1,
                            "tc_index": idx,
                            "tool_call_id": tc.id,
                            "name": name,
                            "args": args,
                        }
                        yield {"type": "approval", "name": name, "args": args}
                        return

                    result = self._call_tool(name, args, on_log)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, default=str)[:12000],
                    })
                continue

            text = m.content or ""
            self.messages.append({"role": "assistant", "content": text})
            yield {"type": "final", "content": text}
            return

        yield {"type": "final", "content": "⚠️ Boss reached max reasoning steps."}

    def resume(self, approved: bool, on_log: Callable[[str], None]
               ) -> Generator[dict, None, None]:
        p = self.state.pop("pending", None)
        if not p:
            yield {"type": "error", "content": "Nothing pending to approve."}
            return

        if approved:
            on_log(f"✅ approved → {p['name']}")
            result = self._call_tool(p["name"], p["args"], on_log)
        else:
            on_log(f"⛔ denied → {p['name']}")
            result = {"denied": True, "reason": "User denied this action."}

        self.messages.append({
            "role": "tool",
            "tool_call_id": p["tool_call_id"],
            "content": json.dumps(result, default=str)[:12000],
        })

        msg = self.messages[p["message_index"]]
        tcs = msg.get("tool_calls", [])
        for tc in tcs[p["tc_index"] + 1:]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except Exception:
                args = {}
            if self._needs_approval(name):
                self.state["pending"] = {
                    "message_index": p["message_index"],
                    "tc_index": tcs.index(tc),
                    "tool_call_id": tc["id"],
                    "name": name,
                    "args": args,
                }
                yield {"type": "approval", "name": name, "args": args}
                return
            r = self._call_tool(name, args, on_log)
            self.messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(r, default=str)[:12000],
            })

        yield from self.stream(on_log)


# =============================================================================
# 6. Streamlit UI
# =============================================================================

st.set_page_config(page_title="MTR AGENT", page_icon="🧠", layout="wide")


def login_gate(settings: Settings):
    if st.session_state.get("authed"):
        return True
    st.title("🧠 MTR AGENT")
    st.caption("Multi-Agent Orchestrator · OpenAI · MCP / n8n")
    pwd = st.text_input("Password", type="password", key="pwd_input")
    if st.button("Unlock", type="primary"):
        if pwd and pwd == settings.app_password:
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


@st.cache_resource(show_spinner="Connecting to MCP servers…")
def get_settings_and_toolbox():
    s = load_settings()
    tb = ToolBox(s)
    return s, tb


def init_state():
    ss = st.session_state
    ss.setdefault("messages", [])
    ss.setdefault("agents", {})
    ss.setdefault("pending", None)
    ss.setdefault("plan_text", None)
    ss.setdefault("plan_task", None)
    ss.setdefault("running", False)
    ss.setdefault("last_final", None)


def sidebar(settings: Settings, toolbox: ToolBox):
    with st.sidebar:
        st.markdown("## ⚙️ MTR AGENT")
        mode = st.radio(
            "Mode",
            ["interactive", "plan", "execute"],
            format_func=lambda x: {
                "interactive": "💬 Interactive (chat only)",
                "plan": "🗂️ Plan (approve before run)",
                "execute": "⚡ Execute (auto, sensitive needs OK)",
            }[x],
            key="mode_radio",
        )
        st.divider()
        st.markdown("### 🤖 Agents")
        st.write(f"**{len(st.session_state.agents)} / {settings.max_agents}**")
        for name, meta in st.session_state.agents.items():
            st.markdown(f"- `{name}` — {meta['role']}")
        st.divider()
        st.markdown("### 🔌 MCP servers")
        if not toolbox.clients:
            st.caption("None connected.")
        for name, cl in toolbox.clients.items():
            n = len(cl.list_tools())
            badge = "🔒 sensitive" if cl.cfg.sensitive else "🟢"
            st.markdown(f"- **{name}** · `{cl.cfg.kind}` · {n} tool(s) · {badge} "
                        f"· timeout {cl.cfg.timeout}s")
        st.divider()
        if st.button("🗑️ Reset conversation", use_container_width=True):
            for k in ("messages", "agents", "pending", "plan_text",
                      "plan_task", "last_final"):
                st.session_state.pop(k, None)
            init_state()
            st.rerun()
        st.caption("MTR AGENT · powered by OpenAI + MCP")
    return mode


def render_history():
    for m in st.session_state.messages:
        role = m.get("role")
        if role == "user":
            with st.chat_message("user"):
                st.markdown(m.get("content", ""))
        elif role == "assistant" and m.get("content"):
            with st.chat_message("assistant", avatar="🧠"):
                st.markdown(m["content"])
        elif role == "tool":
            pass


def run_engine(engine: Engine, log_box):
    logs: List[str] = []

    def on_log(line: str):
        logs.append(line)
        log_box.markdown("\n\n".join(logs[-12:]))

    for ev in engine.stream(on_log):
        if ev["type"] == "final":
            st.session_state.last_final = ev["content"]
            st.session_state.running = False
            st.rerun()
        elif ev["type"] == "approval":
            st.session_state.pending = st.session_state.pending or {
                "name": ev["name"], "args": ev["args"],
            }
            st.session_state.running = False
            st.rerun()
        elif ev["type"] == "error":
            st.error(ev["content"])
            st.session_state.running = False


# =============================================================================
# 7. Main app
# =============================================================================

def main():
    settings, toolbox = get_settings_and_toolbox()
    if not login_gate(settings):
        return

    init_state()
    mode = sidebar(settings, toolbox)

    st.title("🧠 MTR AGENT")
    st.caption(f"Mode: **{mode}** · Boss: `{settings.boss_model}` · "
               f"Agents: `{settings.agent_model}`")

    if not settings.openai_api_key:
        st.error("OpenAI API key missing. Add `[openai] api_key = \"sk-...\"` to secrets.")
        return

    render_history()

    pending = st.session_state.pending
    if pending:
        st.warning(f"⚠️ Approval required: **{pending['name']}**")
        st.code(json.dumps(pending["args"], indent=2, ensure_ascii=False), language="json")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Approve", type="primary", use_container_width=True):
                engine = Engine(settings, toolbox, mode, st.session_state)
                log_box = st.empty()
                try:
                    logs: List[str] = []

                    def on_log(line):
                        logs.append(line)
                        log_box.markdown("\n\n".join(logs[-12:]))

                    for ev in engine.resume(True, on_log):
                        if ev["type"] == "final":
                            st.session_state.last_final = ev["content"]
                        elif ev["type"] == "approval":
                            st.session_state.pending = {"name": ev["name"], "args": ev["args"]}
                            st.rerun()
                    st.session_state.pending = None
                    st.rerun()
                except Exception as e:
                    st.error(f"Resume failed: {e}")
        with c2:
            if st.button("⛔ Deny", use_container_width=True):
                engine = Engine(settings, toolbox, mode, st.session_state)
                log_box = st.empty()
                try:
                    for ev in engine.resume(False, lambda x: None):
                        if ev["type"] == "final":
                            st.session_state.last_final = ev["content"]
                        elif ev["type"] == "approval":
                            st.session_state.pending = {"name": ev["name"], "args": ev["args"]}
                            st.rerun()
                    st.session_state.pending = None
                    st.rerun()
                except Exception as e:
                    st.error(f"Resume failed: {e}")

    if mode == "plan" and st.session_state.plan_text and not pending:
        st.info("📋 Plan ready for review")
        st.markdown(st.session_state.plan_text)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Approve & Execute", type="primary", use_container_width=True):
                st.session_state.messages.append({
                    "role": "user",
                    "content": f"PLAN APPROVED. Execute it now step by step:\n\n{st.session_state.plan_text}",
                })
                st.session_state.plan_text = None
                engine = Engine(settings, toolbox, mode, st.session_state)
                log_box = st.empty()
                run_engine(engine, log_box)
        with c2:
            if st.button("✏️ Discard plan", use_container_width=True):
                st.session_state.plan_text = None
                st.rerun()

    if st.session_state.last_final:
        with st.chat_message("assistant", avatar="🧠"):
            st.markdown(st.session_state.last_final)
        st.session_state.last_final = None

    placeholder = {
        "interactive": "Chat with the boss…",
        "plan": "Describe the task — I'll produce a plan first…",
        "execute": "Describe the task — I'll execute it (sensitive steps need approval)…",
    }[mode]

    user_input = st.chat_input(placeholder)
    if not user_input:
        return

    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    engine = Engine(settings, toolbox, mode, st.session_state)

    if mode == "plan" and not st.session_state.plan_text:
        with st.chat_message("assistant", avatar="🧠"):
            box = st.empty()
            plan_msgs = [
                {"role": "system", "content": PLAN_SYSTEM},
                {"role": "user", "content": user_input},
            ]
            try:
                resp = engine.client.chat.completions.create(
                    model=settings.boss_model, messages=plan_msgs, temperature=0.4,
                )
                plan = resp.choices[0].message.content or ""
            except Exception as e:
                st.error(f"Plan generation failed: {e}")
                return
            box.markdown(plan)
        st.session_state.plan_text = plan
        st.session_state.messages.append({"role": "assistant", "content": plan})
        st.rerun()
        return

    with st.chat_message("assistant", avatar="🧠"):
        log_box = st.empty()
        engine.state["pending"] = None
        run_engine(engine, log_box)


if __name__ == "__main__":
    main()
