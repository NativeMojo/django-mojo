# Django-MOJO AI Platform

**Two Claude-powered agents. One unified operations layer.**

Django-MOJO ships with a complete AI operations stack built directly into the framework — not a bolt-on, not a SaaS add-on. One agent watches your fleet and reacts autonomously. The other gives your team a natural-language console into the same system. Both run on the same tool-use, memory, and permission primitives, so whatever one learns, the other can act on.

---

## 1. The Autonomous Security Agent

*Your always-on Tier-1 SOC analyst.*

Located at `mojo/apps/incident/handlers/llm_agent.py`, the LLM Security Agent is an autonomous triage engine wired directly into the incident pipeline. When a security event fires, the agent investigates, decides, and acts — no human in the loop unless it needs one.

### What it does

When an incident is raised, the agent runs a tool-use loop (up to 15 turns) with 14 purpose-built tools. It queries recent events, pulls IP history, checks related incidents, spots event-count trends, and takes real action: block IPs fleet-wide, send SMS/email/push alerts, create tickets for human review, or propose new rules to automate the pattern forever.

### What makes it powerful

- **Agentic, not reactive.** It doesn't match a rule and fire a webhook — it investigates. The agent pulls IP geolocation, past incidents, event spikes in the last hour, and related incidents from the same category before deciding whether this is noise, a real threat, or something worth waking a human for.

- **Self-improving rule author.** When it spots a recurring pattern, it writes a new `RuleSet` — disabled, pending human approval — with proper `bundle_by`, `min_count`, and `window_minutes` configuration so future events of this type auto-handle without ever touching the LLM again. A signature-based dedup layer prevents duplicate proposals; repeat sightings bump an `occurrence_count` on the approval ticket so humans see how often the pattern has resurfaced.

- **Persistent memory per rule.** Every `RuleSet` has an `agent_memory` field. The agent writes learnings into it ("I've seen this CIDR before, last time it was a scanner, not a real breach") and reads it back on future invocations. Your incident response gets smarter every week.

- **Human-in-the-loop, done right.** For judgment calls, the agent creates `llm_linked` tickets. When a human replies, `execute_llm_ticket_reply` re-invokes the agent with the full conversation history — so it can execute approved actions, answer follow-up questions, or escalate further.

- **Deep-analysis mode.** `execute_llm_analysis` goes beyond single-incident triage: finds related open incidents, merges them into one, and proposes the RuleSet that would have auto-handled the whole cluster. Turns a pile of 50 near-duplicate incidents into one summarized incident plus a pending automation.

- **Safety rails baked in.** Proposed rules ship disabled by default. Noise patterns get `delete_on_resolution` so they don't clutter your dashboard. Confirmed threats get `do_not_delete` to survive pruning. OSSEC alerts are treated as 99% noise by default — with explicit exceptions for file changes in system directories and SSH logins.

### The pitch

> *It triages incidents like a Tier-1 analyst, learns your environment, and teaches the system to handle repeat patterns without it — so your LLM bill shrinks as your ruleset matures.*

---

## 2. The Admin Assistant

*ChatOps for your entire Django-MOJO deployment.*

Located at `mojo/apps/assistant/`, the Admin Assistant is a conversational Claude agent exposed over WebSocket. Admins ask natural-language questions — *"who logged in from Germany today?"*, *"show me failed jobs the last hour"*, *"block the top offender and open a ticket"* — and the assistant investigates, renders rich results, and executes actions. Every call is gated by the requesting user's permissions.

### What makes it powerful

- **Two-tier tool loading.** Ships with a small core toolset (memory, models, docs, web, logs, files) and dynamically loads domain packs — `security`, `jobs`, `users`, `groups`, `metrics`, `notifications`, `planning`, `skills` — on demand via `load_tools`. The prompt stays lean, the LLM bill stays low, even as the toolkit grows to dozens of capabilities.

- **Every tool is permission-checked.** The user's actual Django permissions gate every call. The LLM can *see* tools it can't use; attempts raise clean permission errors and fire `assistant:permission_denied` incidents — so you get a full audit trail of what was requested vs. what was allowed.

- **Parallel execution + plan mode.** For complex requests ("give me a security audit"), the assistant writes a plan with parallel steps, executes up to four tools concurrently via `ThreadPoolExecutor`, and streams live `plan_update` events to the UI so operators watch work progress step-by-step.

- **Structured `assistant_block` rendering.** The LLM emits typed JSON blocks — `table`, `chart`, `stat`, `list`, `alert`, `action`, `file`, `progress` — that the frontend renders as real components. The `action` block is a confirm/cancel prompt with proper buttons, so mutating operations never fall back to *"type yes to confirm"*.

- **Three-tier persistent memory.** Global (platform facts), group (tenant rules), user (personal preferences). Injected into the system prompt on every turn, so the assistant remembers *"never block 10.0.0.0/8"* or *"this is a healthcare SaaS"* across conversations — and asks the right onboarding questions on a fresh deployment.

- **Reusable skills.** Stored multi-step procedures with trigger phrases. Teach it once (*"nightly user audit = query X, export Y, notify Z"*) and it replays on demand — discoverable via `find_skill`, listable via `list_skills`.

- **Safe by construction.** Uses `request.DATA` input guards. ORM objects serialize through `MojoModel.to_dict()` so RestMeta graphs automatically strip sensitive fields (password hashes, tokens). Mutating calls require explicit confirmation blocks. Every error path reports a structured `incident.report_event` — nothing fails silently.

- **WebSocket-native reliability contract.** The handler guarantees either `assistant_response` or `assistant_error` — no hangs, no silent drops. The UI streams `thinking → tool_call → plan → plan_update → response` for a fluid real-time feel.

### The pitch

> *Your ops team stops clicking through a dozen admin pages — they just ask. The assistant answers with real tables, charts, and confirmations, respects everyone's permissions, remembers your environment's rules, and files an incident every time anyone — human or LLM — tries something they shouldn't.*

---

## Together: A Complete AI Operations Layer

| | Autonomous Security Agent | Admin Assistant |
|---|---|---|
| **Trigger** | Incident or event fires | User sends a chat message |
| **Audience** | Nobody — runs on its own | Your ops / admin team |
| **Goal** | Triage, act, and automate | Answer questions, run operations |
| **Tools** | 14 incident-specific tools | Dozens, loaded on demand per domain |
| **Memory** | Per-`RuleSet` `agent_memory` | Global / group / user tiers |
| **Human loop** | Tickets for judgment calls | Conversational by design |
| **Output** | Actions + history notes | Rich structured blocks + prose |

**The two agents share the framework.** They both use the same:

- Claude-powered tool-use loop (`mojo.helpers.llm`)
- Permission-gated action model (`user.has_permission`)
- Structured incident reporting (`incident.report_event`)
- `MojoModel.to_dict()` serialization with RestMeta graph filtering
- Logit-based structured logging for full audit trails

### The combined story

> *Django-MOJO gives you an AI layer that both watches and talks. One agent autonomously triages every incident your platform raises — blocking attackers, merging noise, and proposing rules that make it unnecessary next time. The other lets your humans run the entire platform by chatting with it — permission-aware, memory-backed, and rendering real UI components instead of walls of text. They share the same tools, the same memory primitives, and the same audit trail. You deploy both by setting a single API key.*

Two agents. One platform. Zero duct tape.
