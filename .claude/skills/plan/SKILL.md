---
name: plan
description: Design implementation approach for an issue or request file, adds a Plan section
user-invocable: true
argument-hint: <path to issue or request file>
---

Takes an issue or request file and designs the implementation approach. Adds a ## Plan section to the file.

## Arguments

$ARGUMENTS — Path to an issue or request file (e.g., `planning/issues/login-mfa-bypass.md`). If empty, list open files in `planning/issues/` and `planning/requests/` and ask which one to plan.

## Workflow

### 1. Read the File
Read the issue/request file at $ARGUMENTS. Understand the description, acceptance criteria, and investigation findings.

### 2. Deep Exploration
- Read every file listed in the investigation section
- Read additional files you discover are relevant
- Read `docs/django_developer/README.md` — do not reinvent existing features
- Check `mojo/helpers/` for utilities before planning new ones

### 3. Design the Plan
Produce a concrete, file-level implementation plan:
- **Objective** — one sentence
- **Steps** — ordered, file-level breakdown of what changes
- **Design decisions** — key choices with brief rationale
- **Edge cases** — risks and guards
- **Testing** — test scenarios mapped to test files
- **Docs** — which doc tracks need updating

### 4. Confirm with User
Present the plan clearly. Wait for explicit confirmation or feedback. Iterate if the user redirects.

### 5. Write the Plan
Once confirmed, append `## Plan` to the issue/request file:

```markdown
## Plan

**Status**: planned
**Planned**: <YYYY-MM-DD>

### Objective
<one sentence>

### Steps
1. `<file>` — <what changes and why>
2. `<file>` — <what changes and why>

### Design Decisions
- <decision>: <rationale>

### Edge Cases
- <risk>: <how it's handled>

### Testing
- <scenario> -> `tests/<file>.py`

### Docs
- `<doc file>` — <what changes>
```

Update the file's Status to `planned`.

### 6. Stop and Hand Off
Print:
- The updated file path
- `To implement, start a new session and run: /build <file-path>`

## Rules

- Do NOT write implementation code. Planning and documentation only.
- Do NOT move files between planning folders.
- Resolve open questions with the user before writing the plan.
- Reference specific files and line ranges.
- Keep plans token-efficient: decisions and structure, not narrative.
