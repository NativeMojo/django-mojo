# Django-MOJO Permissions System

This document describes the design, usage, and REST API consequences of permission management in the Django-MOJO framework. It covers how group and user fields on models influence REST API results, the `sys.` prefix convention, permission flow, denial scenarios, and clear developer guidelines.

---

## Overview

Django-MOJO empowers developers with object-level permission control, supporting both system/global and group/scoped security requirements. Its permission logic is designed for:

- **Explicitness:** Easy to reason about, minimal “magic.”
- **Extensibility:** Override or extend at the model, instance, or API level.
- **Security:** Strong audit trail and “fail closed” defaults.

---

## REST Impact: How Permissions Affect API Results

Permissions in Django-MOJO are not just for backend policy—they powerfully shape every REST API response. Understanding this REST impact is critical:

- **Denied Access:** If a user lacks all relevant permissions (system, group, or owner), the REST endpoint returns a 403 Permission Denied error for detail/object requests, and an empty list/result for queries.
- **Owner Shortcut:** If the user is the owner (according to the model’s `user` field and the presence of the `"owner"` key in `RestMeta`), the object will be visible/accessible through detail and list endpoints, regardless of group or broader permissions.
- **Group Context:** When a group id/param is present in the HTTP request, MOJO automatically enforces group-scoping everywhere:
    - All object listings and queries are filtered to that group.
    - Permission checks are run using the user's membership within that group.
    - Users never see or modify objects outside their group unless granted higher, system-level permissions.
- **System Context:** If no group param is provided, and the model has no user-specific shortcut, system/user-level permissions apply.
- **Summary:** API consumers always see exactly the data they're entitled to—no more, no less—based on the intersection of HTTP request context and your permission keys.

---

## Multi-Tenant Handling: Secure Group Isolation

MOJO is designed from the ground up for robust, secure multi-tenancy—meaning you can safely serve multiple groups, organizations, or tenants on the same API backend.

### What This Means

- **Group Isolation:** As soon as an HTTP request includes a group identifier (query param, URL path, etc), MOJO auto-assigns `request.group`. From this point on:
    - All permission checks operate in the context of that group.
    - All queries, listings, and object fetches are scoped so the user only sees data for their group(s), based on their permissions.
    - Attempted access outside of the user’s group is denied—no data is leaked.
- **No Group Param = System Context:** If the API call omits any group context, then only global/system-level user permissions are checked.
- **Owner Guarantee:** Individual users can always see their own data (if `"owner"` permission is configured), regardless of group or system role.
- **Automatic, Not Accidental:** MOJO automatically manages group context for all standard routes/endpoints; developers only need to intervene for unusual or advanced scenarios.
- **REST Impact:** For REST API users and client apps, this means their view of the data is always sandboxed by tenant/group—no accidental cross-tenant visibility or privilege escalation.

---


## Key Permission Types & REST Impact

### 1. **System-level Permissions**

- Checked at the **User** model level (`User.has_permission`).
- Global: apply regardless of group context.
- Use the `sys.` prefix in your permission key to force a system-level lookup (e.g. `"sys.admin"`).
- **REST Impact:** If a user has the needed system permission, they can see/modify/list models (unless further limited by group context).

**Examples:**
```python
member.has_permission("sys.admin")      # Only system/global 'admin' checked on user
member.has_permission("sys.superuser")  # Only user.has_permission('superuser')
```

### 2. **Group/Member-level Permissions**

- Checked on the relevant **GroupMember** instance.
- No prefix; just use the key (e.g. `"manage_group"`).
- Will still fall back to user's system permissions if not found.
- **REST Impact:** If group context (`request.group`) is set, users can only see/modify/list models their group membership allows—even if their system/user perms alone would not allow it.

**Example:**
```python
member.has_permission("manage_group")   # Checks member, then user if needed
```

### 3. **Mixed or "OR" Checks**
Pass a list for “OR” checks (system and/or group).
- **REST Impact:** Any true condition grants access to the resource or action.

```python
member.has_permission(["sys.admin", "manage_group"])
```

---

## Permission Enforcement Flow (and REST/Query Results)

The MOJO REST stack always “fails closed”: if neither group-level nor system/user-level permission is granted, **the resource will not be visible or returnable via REST**.

### Permission Evaluation Order

1. **API endpoint (`rest.py`):** Calls `rest_check_permission(request, keys, instance)`.
2. **RestMeta Lookup:** Gets relevant permission set for the action from model's `RestMeta`.
3. **User Auth:** Unauthenticated users are denied unless `"all"` is explicitly set in the permission keys.
4. **Instance Checks:**
    - Calls `instance.check_edit_permission()` if present (custom per-record logic can override all).
    - **Owner shortcut:** If `"owner"` is in permissions and model has a `user` field, the user matching that field is always allowed.
    - **REST Impact:** Owner will always be able to access their objects (unless logic is overridden).
5. **Group-level Check:**
    - If `request.group` is set and the model has a `group` field, checks group member permission.
    - **REST Impact:** Users see/modify only objects belonging to their groups, if the endpoint sets `request.group`.
    - In list endpoints, query results are filtered to this group.
6. **System-level/User Check:**
    - Fallback: uses user’s global permissions.
    - **REST Impact:** If user has system-level perms, can see all ungrouped (or globally visible) objects.
7. **Default Denied:**
    - If none of these conditions is met, **REST API returns a permission denied error (single object)** or an empty set (listing/query).

### Failure & Visibility Scenarios

- **If `request.group` is unset and the user lacks user-level permission:**  
  - The result is NO ACCESS: the user cannot see, modify, or list these objects.
- **If `request.group` is set, but user isn’t a member:**  
  - Again, NO ACCESS.
- **If model has `group` field and request has a valid group but permissions are missing:**  
  - NO ACCESS.
- **If model has `user` field, `"owner"` in perms, and requesting user matches:**  
  - ALWAYS has access to their own instance through all REST endpoints unless custom logic disables this.

--- 

## Security & Event Auditing

Django-MOJO automatically logs every permission denial via the **incident/event system**. This is handled through `class_report_incident` and `report_incident` methods on your base models.

- Each denied branch records a structured security/audit log, including user, permissions, route, and denial reason (“unauthenticated”, “group_member_permission_denied”, etc.).
- Granular feedback is **never exposed to the end user**—only generic “permission denied” is shown in API responses. All details are recorded internally.

**Example of an internally logged incident:**
```json
{
  "event_type": "user_permission_denied",
  "model_name": "Project",
  "perms": ["edit_project"],
  "branch": "user.has_permission",
  "request_path": "/api/project/5",
  "user": "...",
  "...": "..."
}
```

You can view and aggregate these events using the [Incidents/Event system](incident.md).

---

## Developer & API Guidelines

### 1. Group Context Is Auto-Set From HTTP Requests

- MOJO automatically populates `request.group` if a group parameter is provided in the incoming HTTP request (as a query param, path part, or similar).
- This means that group-based scoping and permission checks are **enforced by default** whenever the group param is present—developers do not need to manually set `request.group` in typical REST endpoints.

**REST Impact:**
- If a request includes a group reference, users will only see/query objects within that group and only if they have the appropriate group member permissions.
- If no group param is included, access falls back to system/user-level permissions (and all-group filtering is omitted).

**Tip:**  
If you are extending the REST framework or making custom integrations, be aware that group context is robustly handled by default; only override this behavior for advanced scenarios.

### 2. Always Check/Use RestMeta for Permission Keys

- Define `"VIEW_PERMS"`, `"SAVE_PERMS"`, etc. for all models.
- Use `"owner"` in these keys if owners should always access their data (`user` field must be present).
- Be aware: owner shortcut bypasses other permission checks if present.

### 3. Impact on REST API Results

- **No permissions:**  
  - Users see empty result sets in lists, and 403 for object/detail requests.
- **Owner:**  
  - Always sees own objects if `"owner"` in perms. Never sees objects they don’t own unless group/user-level perms granted.
- **Group:**  
  - Users only see group objects if both request.group is set AND membership is valid AND perms allow.
- **System:**  
  - System/global users (with proper perms) can see all objects, even across groups, when group context is not enforced.
  
### 4. Testing/Debugging Guidelines

- Always verify endpoints set `request.group` when intending to scope by group.
- Add tests for all failure scenarios: group not set, no permission, owner only, etc.
- Review API responses: results should match the expected access rights based on model field presence and endpoint logic.

---

## Example REST Flow Scenarios

**Scenario 1: User tries to access all objects with no permissions and no group context.**
- REST returns empty list for queries, 403 for detail.

**Scenario 2: User is group member and `request.group` is set, but lacks permission.**
- REST returns empty list/no access.

**Scenario 3: User is owner of an object, `"owner"` in perms.**
- REST returns object for detail, includes in list.

**Scenario 4: User is system-admin (`sys.ADMIN`) but no group context set.**
- REST returns all objects (if user perms allow), group not enforced.

---


## The `sys.` Prefix

To avoid confusion or escalation risk when permissions may exist at both group and system scope, MOJO reserves the `sys.` prefix for system-level checks.

- `"sys.edit"` checks **only** User permissions, never the group/member.
- `"edit"` or `"manage_group"` checks group member’s context first, then the user.

This simple convention makes it trivial to mix global and scoped access rules in your business logic and endpoint definitions.

---

## Extending/Customizing Permission Logic

- Override `rest_check_permission` on your model for project-specific branching.
- Implement or extend `.check_edit_permission` at the instance level for extremely granular, per-record security.
- Use the incident system for custom alerting, incident bundling, or notification on security events.
- Always log (internally) any decision that blocks a user, for compliance and ops forensics.

---

## Best Practices

- Always define permission keys in each model’s `RestMeta` (`VIEW_PERMS`, `SAVE_PERMS`, etc.).
- Use the `sys.` prefix for system/global permissions. Use plain keys for group/tenant scoping.
- Never leak extra permission failure details in responses; rely on the incident reporting for all debug/security analysis.
- Use the list-syntax `[key1, key2]` for “OR” permission checks.
- Add/override instance or group permission-checking logic only when needed for your domain.

---

## References

- [models/rest.py](../mojo/models/rest.py) — Permission enforcement code
- [mojo/apps/account/models/member.py](../mojo/apps/account/models/member.py) — GroupMember permission handling
- [mojo/apps/incident/](../mojo/apps/incident/) — Event & incident backend

---

_Questions? Contributions? Please improve this doc as the permission model evolves!_
