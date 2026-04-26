# Assistant Block Rendering Guide

Implementation guide for rendering all assistant block types in the frontend. Blocks arrive in the `blocks` array of assistant responses (REST or WebSocket). The `response` text field contains the narrative with block fences already stripped — render text and blocks together.

## Block Type Reference

| Type | Purpose | Key Fields |
|---|---|---|
| `table` | Query results, record lists | `title`, `columns`, `rows` |
| `chart` | Time-series, trends | `chart_type`, `title`, `labels`, `series`, plus optional render hints (see [Chart Block](#chart--seriesschart--piechart-options) below) |
| `stat` | Dashboard key metrics | `items` (label/value pairs) |
| `action` | Confirmation cards with buttons | `action_id`, `title`, `description`, `actions` |
| `list` | Single-record key/value detail | `title`, `items` (label/value pairs) |
| `alert` | Severity-colored status banners | `level`, `title`, `message` |
| `progress` | Multi-step plan tracker | `plan_id`, `title`, `steps` |

---

## `chart` — SeriesChart / PieChart Options

The base shape (`type`, `chart_type`, `title`, `labels`, `series`) renders a basic chart. The following optional fields let the LLM pick the right rendering for the data.

### Schema

```json
{
    "type": "chart",
    "chart_type": "bar",
    "title": "Events by Severity (7d)",
    "labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "series": [
        {"name": "low", "values": [12, 15, 9, 18, 22, 7, 11], "color": "#22c55e"},
        {"name": "medium", "values": [4, 6, 3, 7, 9, 2, 5]},
        {"name": "high", "values": [1, 2, 0, 1, 3, 0, 1]}
    ],
    "stacked": "auto",
    "colors": ["#22c55e", "#f59e0b", "#ef4444"],
    "show_legend": true,
    "legend_position": "bottom"
}
```

### Field Reference

**Common to all chart types:**

| Field | Type | Default | Description |
|---|---|---|---|
| `colors` | array or null | palette | Chart-level color palette override. `null` falls back to the framework palette. |
| `show_legend` | bool | `true` | Whether to render the legend at all. |
| `legend_position` | string | `"top"` (line/bar/area) / `"right"` (pie) | Legend placement. Line/bar/area: `top`/`bottom`/`left`/`right`. Pie: `right`/`bottom`/`none`. |

**Per-series fields** (on each `series` entry):

| Field | Type | Description |
|---|---|---|
| `color` | string | Per-series color override — wins over chart-level `colors`. |
| `fill` | bool | Line/area only — fill area under the line. |
| `smoothing` | number | Line/area only — curve smoothing factor. |

**Bar charts** (`chart_type: "bar"`):

| Field | Type | Default | Description |
|---|---|---|---|
| `stacked` | `true` / `false` / `"auto"` | `"auto"` (resolves to `true`) | Stack mode. Stacked is the new default. |
| `grouped` | bool | — | Convenience alias for `stacked: false`. |

**Line / area charts** (`chart_type: "line" | "area"`):

| Field | Type | Default | Description |
|---|---|---|---|
| `crosshair_tracking` | bool | `false` | Floating crosshair + per-dataset ghost dot + multi-row tooltip that follows the cursor anywhere over the plot. Best for charts with 2+ series. |

**Pie charts** (`chart_type: "pie"`):

| Field | Type | Default | Description |
|---|---|---|---|
| `cutout` | number `0..1` | `0` | Doughnut depth. `0` is solid pie, `0.55` is doughnut. Server clamps out-of-range values. |
| `show_labels` | bool | `false` | Slice-edge labels. |
| `show_percentages` | bool | `true` | Append `%` next to slice labels. |

### Server-side Validation

The server validates chart blocks before they reach the client (`_validate_block` in `mojo/apps/assistant/services/agent.py`):

- **Drops the block** if `chart_type` is not in `{line, bar, pie, area}`, if `labels` or `series` is empty/non-list, if any series entry lacks `name: str` or `values: list`, or if any `series[i].values` length does not match `len(labels)`.
- **Clamps `cutout`** to `[0, 1]`. Non-numeric values are stripped.
- **Strips `stacked`** if it is not in `{true, false, "auto"}` (chart still renders).
- **Coerces `crosshair_tracking`** to bool.
- **Strips `colors`** if non-list and non-null.
- **Unknown top-level fields pass through** unchanged. Future server schema additions don't require renderer changes; future renderer additions don't require server changes.

The renderer can rely on the shape being well-formed for the validated fields.

---

## `action` — Confirmation Cards

Rendered when the assistant needs user confirmation before a mutating operation (block IP, disable user, cancel job, etc.).

### Schema

```json
{
    "type": "action",
    "action_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "title": "Block IP",
    "description": "Block 1.2.3.4 on all firewall sets for 24 hours",
    "actions": [
        {"label": "Confirm", "value": "confirm"},
        {"label": "Cancel", "value": "cancel"}
    ]
}
```

| Field | Type | Description |
|---|---|---|
| `action_id` | string | UUID assigned by the server. Use to track which action was clicked. |
| `title` | string | What the action is — displayed as the card header. |
| `description` | string or null | Additional detail about what will happen. Optional. |
| `actions` | object[] | 1-4 buttons. Each has `label` (display text) and `value` (sent back to server). |

### Rendering

- Card with a prominent header (`title`) and optional body text (`description`).
- Button row at the bottom. Use primary style for the first action, secondary/outline for the rest.
- The last action is typically "Cancel" — style it as a muted/outline button.

### Interaction Flow

1. User clicks a button.
2. Disable all buttons immediately (prevent double-submit).
3. Visually mark the clicked button (highlight, checkmark, or change to "Confirmed"/"Cancelled").
4. Send the user's choice via WebSocket:

```javascript
ws.send(JSON.stringify({
    type: 'assistant_action',
    conversation_id: conversationId,
    action_id: block.action_id,
    value: clickedAction.value   // e.g. "confirm" or "cancel"
}));
```

5. The server converts the `value` into a user message and triggers the normal assistant flow. The assistant will then execute (or skip) the operation and respond.

### States

| State | Visual |
|---|---|
| **Pending** | Buttons enabled, normal styling |
| **Clicked** | All buttons disabled, chosen button highlighted, others dimmed |
| **Expired** | If viewing from conversation history after action was taken, show which action was chosen (match against the next user message in history) |

### Example Implementation

```javascript
function renderActionBlock(block, conversationId) {
    const card = document.createElement('div');
    card.className = 'assistant-action-card';

    const header = document.createElement('h4');
    header.textContent = block.title;
    card.appendChild(header);

    if (block.description) {
        const desc = document.createElement('p');
        desc.textContent = block.description;
        card.appendChild(desc);
    }

    const btnRow = document.createElement('div');
    btnRow.className = 'action-buttons';

    block.actions.forEach((action, index) => {
        const btn = document.createElement('button');
        btn.textContent = action.label;
        btn.className = index === 0 ? 'btn-primary' : 'btn-outline';
        btn.onclick = () => {
            // Disable all buttons
            btnRow.querySelectorAll('button').forEach(b => {
                b.disabled = true;
                b.classList.add('dimmed');
            });
            btn.classList.remove('dimmed');
            btn.classList.add('chosen');

            // Send choice to server
            ws.send(JSON.stringify({
                type: 'assistant_action',
                conversation_id: conversationId,
                action_id: block.action_id,
                value: action.value,
            }));
        };
        btnRow.appendChild(btn);
    });

    card.appendChild(btnRow);
    return card;
}
```

### CSS Suggestions

```css
.assistant-action-card {
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 16px;
    margin: 8px 0;
    background: #f8fafc;
}
.assistant-action-card h4 {
    margin: 0 0 4px;
    font-size: 15px;
    font-weight: 600;
}
.assistant-action-card p {
    margin: 0 0 12px;
    color: #64748b;
    font-size: 13px;
}
.action-buttons {
    display: flex;
    gap: 8px;
}
.action-buttons button.dimmed {
    opacity: 0.4;
    cursor: not-allowed;
}
.action-buttons button.chosen {
    opacity: 1;
    outline: 2px solid #3b82f6;
}
```

---

## `list` — Key/Value Detail Cards

Rendered for single-record summaries: user profiles, incident details, job info. Replaces the awkward pattern of a 1-row table.

### Schema

```json
{
    "type": "list",
    "title": "Incident #42",
    "items": [
        {"label": "Category", "value": "auth:brute_force"},
        {"label": "Priority", "value": 8},
        {"label": "Status", "value": "investigating"},
        {"label": "Events", "value": 23},
        {"label": "Created", "value": "2026-04-06 14:30 UTC"}
    ]
}
```

| Field | Type | Description |
|---|---|---|
| `title` | string or null | Optional card header. |
| `items` | object[] | 1-20 items, each with `label` (string) and `value` (string or number). |

### Rendering

- Card with optional title header.
- Vertical list of label/value pairs.
- Labels left-aligned and muted. Values right-aligned or on a new line for long values.
- Consider alternating row backgrounds for readability.

### Example Implementation

```javascript
function renderListBlock(block) {
    const card = document.createElement('div');
    card.className = 'assistant-list-card';

    if (block.title) {
        const header = document.createElement('h4');
        header.textContent = block.title;
        card.appendChild(header);
    }

    const dl = document.createElement('dl');
    dl.className = 'list-items';

    block.items.forEach(item => {
        const row = document.createElement('div');
        row.className = 'list-row';

        const dt = document.createElement('dt');
        dt.textContent = item.label;
        row.appendChild(dt);

        const dd = document.createElement('dd');
        dd.textContent = String(item.value);
        row.appendChild(dd);

        dl.appendChild(row);
    });

    card.appendChild(dl);
    return card;
}
```

### CSS Suggestions

```css
.assistant-list-card {
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 16px;
    margin: 8px 0;
    background: #ffffff;
}
.assistant-list-card h4 {
    margin: 0 0 12px;
    font-size: 15px;
    font-weight: 600;
}
.list-items {
    margin: 0;
}
.list-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    padding: 6px 0;
    border-bottom: 1px solid #f1f5f9;
}
.list-row:last-child {
    border-bottom: none;
}
.list-row dt {
    color: #64748b;
    font-size: 13px;
    flex-shrink: 0;
    margin-right: 16px;
}
.list-row dd {
    margin: 0;
    font-size: 14px;
    font-weight: 500;
    text-align: right;
}
```

---

## `alert` — Status Banners

Rendered for warnings, errors, success messages, and informational notices that need visual prominence.

### Schema

```json
{
    "type": "alert",
    "level": "warning",
    "title": "Rate Limited",
    "message": "User exceeded 100 req/min threshold. Current rate: 142 req/min."
}
```

| Field | Type | Description |
|---|---|---|
| `level` | string | `info`, `success`, `warning`, or `error` |
| `title` | string or null | Optional short headline. |
| `message` | string | Detail text. Always present. |

### Level Styles

| Level | Color | Icon | Use Case |
|---|---|---|---|
| `info` | Blue (`#3b82f6`) | Info circle | General notices, tips |
| `success` | Green (`#22c55e`) | Checkmark | Operation completed, action confirmed |
| `warning` | Amber (`#f59e0b`) | Warning triangle | Rate limits, approaching thresholds |
| `error` | Red (`#ef4444`) | X circle | Permission denied, failures, blocked actions |

### Rendering

- Full-width banner with left color border or background tint.
- Icon on the left matching the level.
- Title in bold (if present), message below.
- Should be visually distinct from the narrative text — not just another paragraph.

### Example Implementation

```javascript
const ALERT_STYLES = {
    info:    { bg: '#eff6ff', border: '#3b82f6', icon: 'ℹ' },
    success: { bg: '#f0fdf4', border: '#22c55e', icon: '✓' },
    warning: { bg: '#fffbeb', border: '#f59e0b', icon: '⚠' },
    error:   { bg: '#fef2f2', border: '#ef4444', icon: '✕' },
};

function renderAlertBlock(block) {
    const style = ALERT_STYLES[block.level] || ALERT_STYLES.info;

    const alert = document.createElement('div');
    alert.className = `assistant-alert alert-${block.level}`;
    alert.style.background = style.bg;
    alert.style.borderLeftColor = style.border;

    const icon = document.createElement('span');
    icon.className = 'alert-icon';
    icon.textContent = style.icon;
    alert.appendChild(icon);

    const content = document.createElement('div');
    content.className = 'alert-content';

    if (block.title) {
        const title = document.createElement('strong');
        title.textContent = block.title;
        content.appendChild(title);
    }

    const msg = document.createElement('p');
    msg.textContent = block.message;
    content.appendChild(msg);

    alert.appendChild(content);
    return alert;
}
```

### CSS Suggestions

```css
.assistant-alert {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 12px 16px;
    margin: 8px 0;
    border-radius: 6px;
    border-left: 4px solid;
}
.alert-icon {
    font-size: 16px;
    line-height: 1;
    flex-shrink: 0;
    margin-top: 2px;
}
.alert-content strong {
    display: block;
    margin-bottom: 2px;
    font-size: 14px;
}
.alert-content p {
    margin: 0;
    font-size: 13px;
    color: #334155;
}
```

---

## `progress` — Plan Tracker

Rendered when the assistant creates a multi-step plan. Shows which steps are complete, in progress, or pending. Updates in real time via WebSocket events.

### Schema

```json
{
    "type": "progress",
    "plan_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "title": "Security Audit (24h)",
    "steps": [
        {"id": 1, "description": "Check open incidents", "status": "done", "summary": "3 open, 1 critical"},
        {"id": 2, "description": "Review blocked IPs", "status": "done", "summary": "12 currently blocked"},
        {"id": 3, "description": "Scan failed logins", "status": "in_progress", "summary": null},
        {"id": 4, "description": "Check job failures", "status": "pending", "summary": null},
        {"id": 5, "description": "Summarize findings", "status": "pending", "summary": null}
    ]
}
```

| Field | Type | Description |
|---|---|---|
| `plan_id` | string | UUID for the plan. Use to match against WS update events. |
| `title` | string | Plan title — displayed as the header. |
| `steps` | object[] | Steps with `id`, `description`, `status`, and optional `summary`. |

### Step Statuses

| Status | Icon | Visual |
|---|---|---|
| `pending` | Empty circle `○` | Muted text, waiting |
| `in_progress` | Spinner or pulsing dot | Active styling, animated |
| `done` | Checkmark `✓` | Green, show summary below description |
| `skipped` | Skip icon `⊘` | Muted/strikethrough |

### Rendering

- Card with title header and a progress bar or fraction ("3 of 5 complete").
- Vertical step list. Each step shows status icon, description, and summary (when done).
- The `in_progress` step should have subtle animation (spinner or pulse) to indicate activity.

### Real-Time Updates via WebSocket

The progress block updates without page refresh via two WS events:

#### `assistant_plan` — Full plan created

Fired when the assistant creates a plan. Render the full progress block.

```json
{
    "type": "assistant_plan",
    "conversation_id": 42,
    "plan": {
        "plan_id": "...",
        "title": "Security Audit (24h)",
        "steps": [
            {"id": 1, "description": "Check incidents", "status": "pending", "summary": null},
            {"id": 2, "description": "Check jobs", "status": "pending", "summary": null}
        ]
    }
}
```

#### `assistant_plan_update` — Single step updated

Fired as each step progresses. Update the step in place without re-rendering the whole block.

```json
{
    "type": "assistant_plan_update",
    "conversation_id": 42,
    "plan_id": "...",
    "step_id": 1,
    "status": "done",
    "summary": "3 open incidents, 1 critical"
}
```

### Client Wiring

```javascript
// Store active plans by plan_id for in-place updates
const activePlans = {};

ws.on('assistant_plan', (data) => {
    const plan = data.plan;
    activePlans[plan.plan_id] = plan;
    renderProgressBlock(plan, data.conversation_id);
});

ws.on('assistant_plan_update', (data) => {
    const plan = activePlans[data.plan_id];
    if (!plan) return;

    // Update the step in our local copy
    const step = plan.steps.find(s => s.id === data.step_id);
    if (step) {
        step.status = data.status;
        step.summary = data.summary;
    }

    // Update just the affected step in the DOM
    updateProgressStep(data.plan_id, data.step_id, data.status, data.summary);

    // Update the progress bar/counter
    updateProgressBar(data.plan_id, plan.steps);
});
```

### Example Implementation

```javascript
function renderProgressBlock(plan, conversationId) {
    const card = document.createElement('div');
    card.className = 'assistant-progress-card';
    card.id = `plan-${plan.plan_id}`;

    // Header with title + progress count
    const header = document.createElement('div');
    header.className = 'progress-header';

    const title = document.createElement('h4');
    title.textContent = plan.title;
    header.appendChild(title);

    const counter = document.createElement('span');
    counter.className = 'progress-counter';
    counter.id = `plan-counter-${plan.plan_id}`;
    updateCounterText(counter, plan.steps);
    header.appendChild(counter);

    card.appendChild(header);

    // Progress bar
    const bar = document.createElement('div');
    bar.className = 'progress-bar-track';
    const fill = document.createElement('div');
    fill.className = 'progress-bar-fill';
    fill.id = `plan-bar-${plan.plan_id}`;
    updateBarWidth(fill, plan.steps);
    bar.appendChild(fill);
    card.appendChild(bar);

    // Step list
    const stepList = document.createElement('div');
    stepList.className = 'progress-steps';

    plan.steps.forEach(step => {
        stepList.appendChild(renderProgressStep(plan.plan_id, step));
    });

    card.appendChild(stepList);
    return card;
}

const STATUS_ICONS = {
    pending: '○',
    in_progress: '◉',
    done: '✓',
    skipped: '⊘',
};

function renderProgressStep(planId, step) {
    const row = document.createElement('div');
    row.className = `progress-step step-${step.status}`;
    row.id = `plan-step-${planId}-${step.id}`;

    const icon = document.createElement('span');
    icon.className = 'step-icon';
    icon.textContent = STATUS_ICONS[step.status];
    row.appendChild(icon);

    const content = document.createElement('div');
    content.className = 'step-content';

    const desc = document.createElement('span');
    desc.className = 'step-description';
    desc.textContent = step.description;
    content.appendChild(desc);

    if (step.summary) {
        const summary = document.createElement('span');
        summary.className = 'step-summary';
        summary.textContent = step.summary;
        content.appendChild(summary);
    }

    row.appendChild(content);
    return row;
}

function updateProgressStep(planId, stepId, status, summary) {
    const row = document.getElementById(`plan-step-${planId}-${stepId}`);
    if (!row) return;

    // Update class
    row.className = `progress-step step-${status}`;

    // Update icon
    row.querySelector('.step-icon').textContent = STATUS_ICONS[status];

    // Update or add summary
    let summaryEl = row.querySelector('.step-summary');
    if (summary) {
        if (!summaryEl) {
            summaryEl = document.createElement('span');
            summaryEl.className = 'step-summary';
            row.querySelector('.step-content').appendChild(summaryEl);
        }
        summaryEl.textContent = summary;
    }
}
```

### CSS Suggestions

```css
.assistant-progress-card {
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 16px;
    margin: 8px 0;
    background: #ffffff;
}
.progress-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
}
.progress-header h4 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
}
.progress-counter {
    font-size: 12px;
    color: #64748b;
}

/* Progress bar */
.progress-bar-track {
    height: 4px;
    background: #e2e8f0;
    border-radius: 2px;
    margin-bottom: 12px;
    overflow: hidden;
}
.progress-bar-fill {
    height: 100%;
    background: #3b82f6;
    border-radius: 2px;
    transition: width 0.3s ease;
}

/* Steps */
.progress-steps {
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.progress-step {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 4px 0;
}
.step-icon {
    width: 18px;
    text-align: center;
    flex-shrink: 0;
    font-size: 14px;
    line-height: 20px;
}
.step-content {
    display: flex;
    flex-direction: column;
}
.step-description {
    font-size: 13px;
    line-height: 20px;
}
.step-summary {
    font-size: 12px;
    color: #64748b;
    margin-top: 1px;
}

/* Status styles */
.step-pending .step-icon { color: #94a3b8; }
.step-pending .step-description { color: #94a3b8; }

.step-in_progress .step-icon {
    color: #3b82f6;
    animation: pulse 1.5s ease-in-out infinite;
}
.step-in_progress .step-description { color: #1e293b; font-weight: 500; }

.step-done .step-icon { color: #22c55e; }
.step-done .step-description { color: #334155; }

.step-skipped .step-icon { color: #94a3b8; }
.step-skipped .step-description {
    color: #94a3b8;
    text-decoration: line-through;
}

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}
```

---

## Updated `renderBlocks` Dispatcher

```javascript
function renderBlocks(blocks, conversationId) {
    const container = document.createElement('div');
    container.className = 'assistant-blocks';

    for (const block of blocks) {
        switch (block.type) {
            case 'table':
                container.appendChild(renderTable(block));
                break;
            case 'chart':
                container.appendChild(renderChart(block));
                break;
            case 'stat':
                container.appendChild(renderStatCards(block));
                break;
            case 'action':
                container.appendChild(renderActionBlock(block, conversationId));
                break;
            case 'list':
                container.appendChild(renderListBlock(block));
                break;
            case 'alert':
                container.appendChild(renderAlertBlock(block));
                break;
            case 'progress':
                container.appendChild(renderProgressBlock(block, conversationId));
                break;
        }
    }

    return container;
}
```

---

## Updated WebSocket Event Handling

Add these to your existing WS listener:

```javascript
// Plan events (new)
ws.on('assistant_plan', (data) => {
    const plan = data.plan;
    activePlans[plan.plan_id] = plan;
    renderProgressBlock(plan, data.conversation_id);
});

ws.on('assistant_plan_update', (data) => {
    const plan = activePlans[data.plan_id];
    if (!plan) return;
    const step = plan.steps.find(s => s.id === data.step_id);
    if (step) {
        step.status = data.status;
        step.summary = data.summary;
    }
    updateProgressStep(data.plan_id, data.step_id, data.status, data.summary);
    updateProgressBar(data.plan_id, plan.steps);
});

// Action responses — send via WS when user clicks an action button
function sendActionResponse(conversationId, actionId, value) {
    ws.send(JSON.stringify({
        type: 'assistant_action',
        conversation_id: conversationId,
        action_id: actionId,
        value: value,
    }));
}
```

## Summary of WS Message Types

### Client → Server

| Type | When | Required Fields |
|---|---|---|
| `assistant_message` | User sends a chat message | `message`, optional `conversation_id` |
| `assistant_action` | User clicks an action button | `conversation_id`, `action_id`, `value` |

### Server → Client

| Type | When | Key Fields |
|---|---|---|
| `assistant_thinking` | Processing started | `conversation_id` |
| `assistant_tool_call` | Each tool called | `conversation_id`, `tool`, `input` |
| `assistant_response` | Final response | `conversation_id`, `response`, `blocks`, `tool_calls_made` |
| `assistant_error` | Failure | `conversation_id`, `error` |
| `assistant_plan` | Plan created | `conversation_id`, `plan` (full plan object) |
| `assistant_plan_update` | Step status changed | `conversation_id`, `plan_id`, `step_id`, `status`, `summary` |
