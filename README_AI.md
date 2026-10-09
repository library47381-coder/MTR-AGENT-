# 🧠 MTR AGENT

**Boss + up to 50 sub-agents** orchestrator built on **Streamlit + OpenAI + MCP**.
Designed to work with **n8n workflows hosted on Render** (long cold-start friendly).

---

## ✨ Features

| Feature | Description |
|---|---|
| 🧠 Boss agent | Reads the user's task, plans, spawns agents, delegates, calls external tools |
| 🤖 Up to 50 sub-agents | Dynamically created by the boss (`spawn_agent`) |
| 💬 Interactive mode | Plain chat — boss answers directly (tools allowed, no approval) |
| 🗂️ Plan mode | Boss first produces a plan → you approve → then it executes |
| ⚡ Execute mode | Boss runs autonomously; only **sensitive** tools ask for approval |
| 🔌 MCP support | Standard MCP servers (JSON-RPC 2.0 over HTTP) |
| 🔗 n8n support | n8n webhooks via HTTP POST (with 180 s timeout for Render cold starts) |
| 🔐 Secrets | OpenAI key + MCP headers live in Streamlit secrets |
| 🚫 Sensitive tools | Any server marked `sensitive = true` requires user approval |
| ⏱️ Render-friendly | Default 180 s timeout so n8n can wake up (~60 s) without cutting the call |

---

## 📦 Installation

```bash
git clone <your-repo>
cd mtr-agent
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 🔑 Secrets (`.streamlit/secrets.toml`)

```toml
[app]
password    = "change-me-please"
max_agents  = 50

[openai]
api_key      = "sk-xxxxxxxxxxxxxxxxxxxxxxxx"
base_url     = "https://api.openai.com/v1"
boss_model   = "gpt-4o"
agent_model  = "gpt-4o-mini"

# ----------------------------------------------------------------
# MCP / n8n servers
# ----------------------------------------------------------------
[[mcp_servers]]
name     = "n8n"
kind     = "n8n"
url      = "https://your-n8n.onrender.com/webhook/agent"
timeout  = 180
sensitive = true

[[mcp_servers]]
name     = "filesystem"
kind     = "mcp"
url      = "https://mcp.example.com/mcp"
timeout  = 60
sensitive = false
```

> ⚠️ On Streamlit Cloud, paste the same content into **App → Settings → Secrets**.

### Secret keys explained

| Key | Meaning |
|---|---|
| `app.password` | UI login password |
| `app.max_agents` | Hard cap on sub-agents (default 50) |
| `openai.api_key` | Your OpenAI key |
| `openai.base_url` | Custom base URL (Azure / OpenRouter / proxy) |
| `openai.boss_model` | Model used by the boss (use the strongest) |
| `openai.agent_model` | Model used by sub-agents (use a cheaper one) |
| `mcp_servers[].kind` | `"mcp"` for JSON-RPC, `"n8n"` for webhooks |
| `mcp_servers[].timeout` | Per-request timeout in seconds (default **180**) |
| `mcp_servers[].sensitive` | If `true`, every call from that server asks approval |
| `mcp_servers[].headers` | Extra HTTP headers (API keys, bearer tokens…) |

---

## ▶️ Run

```bash
streamlit run mtr_agent.py
```

Open http://localhost:8501 → enter the password → pick a mode.

---

## 🎛️ Modes

### 1. 💬 Interactive
- Normal chat with the boss.
- Boss may still call MCP / n8n tools on its own.
- **No approvals** — fastest, best for brainstorming & light tasks.

### 2. 🗂️ Plan
- Boss first returns a numbered **execution plan** (goal, agents, steps, risks).
- You review the plan.
- Click **Approve & Execute** → boss runs it. Every tool call is **approved one-by-one**.
- Click **Discard plan** → throw it away.

### 3. ⚡ Execute
- Boss runs end-to-end on its own.
- **Only sensitive tools** (servers marked `sensitive = true`) pause for approval.
- Best for long pipelines where you trust most tools.

---

## 🧩 How the boss works

```
User task
   │
   ▼
┌──────────────────────────┐
│  MTR BOSS (LLM loop)     │
│  tools:                  │
│   • spawn_agent          │
│   • delegate_task        │
│   • list_agents          │
│   • list_external_tools  │
│   • mcp__<server>__<tool>│
└──────────┬───────────────┘
           │
   ┌───────┴────────┐
   ▼                ▼
Sub-agents     External tools
(≤50)          MCP / n8n
```

Sub-agents **cannot** call sensitive tools directly — if they need one, they
report it back and the boss triggers the approval flow.

---

## 🔌 n8n on Render — why 180 s?

Render free/cheap tiers **spin down** idle services. The first request after
spin-down can take **~60 seconds** before the workflow starts. A 30 s timeout
would kill the agent loop.

`MTR AGENT` sets `timeout = 180` by default for every MCP/n8n server, so the
boss waits patiently for the container to wake up.

If your workflow is heavy, raise it:

```toml
[[mcp_servers]]
name    = "n8n"
kind    = "n8n"
url     = "https://your-n8n.onrender.com/webhook/agent"
timeout = 300
```

---

## 📁 Project layout

```
mtr-agent/
├── mtr_agent.py
├── requirements.txt
├── README.md
└── .streamlit/
    └── secrets.toml      # NOT committed to git
```

Add `.streamlit/secrets.toml` to `.gitignore`.

---

## 🐞 Troubleshooting

| Symptom | Fix |
|---|---|
| `OpenAI API key missing` | Add `[openai] api_key` to secrets |
| `Request timed out after 180s` | n8n still cold — retry, or raise `timeout` |
| `HTTP 401` on MCP call | Wrong / missing header in `mcp_servers[].headers` |
| Boss loops without finishing | Lower `app.max_boss_steps` or use a stronger `boss_model` |
| Agent stuck | Lower `app.max_agent_steps`, or make the agent's `system_prompt` tighter |
| Sub-agent blocked on sensitive tool | That's by design — the boss must run it |

---

## 🔒 Security notes

- Never commit `secrets.toml`.
- Use a **separate OpenAI key** for this app; rotate it periodically.
- Mark any server that can mutate state (email, DB writes, deployments,
  payments) as `sensitive = true`.
- The login gate is a **UI lock only** — put the app behind Streamlit Cloud's
  own auth or an SSO proxy for production.

---

## 📜 License

MIT — do whatever you want, no warranty.
