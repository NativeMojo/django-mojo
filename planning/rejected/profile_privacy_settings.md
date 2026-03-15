# Rejected: Profile Visibility / Privacy Settings

## Request

`POST /api/user/me/privacy` with settings like `{ show_email: false, show_phone: false, profile_visible: "members_only" }` to control what other users can see on a profile.

## Decision: Rejected

## Reason

This is entirely product-defined behaviour. The framework has no concept of "what other users can see" because it has no knowledge of the downstream app's data model, access patterns, or user relationships.

Specifically:

- "Members only" requires the framework to know what a "member" is — that definition varies per project.
- Which fields are visible to which roles is a product decision, not a framework primitive.
- Enforcing visibility at the REST graph level would require per-field, per-caller logic that the framework intentionally delegates to downstream `RestMeta` graph definitions.
- Any generic implementation would either be too opaque (projects wouldn't know how to override it) or so thin it adds no real value over what projects can already do with custom graphs and permission checks.

## Correct Approach

Downstream projects should implement this themselves:

- Define a `privacy` or `profile_settings` field on their extended User model (or in `user.metadata`).
- Control field exposure via `RestMeta` graphs and `VIEW_PERMS` in their project-level user serialisation.
- Use `OWNER_FIELD` and permission decorators to enforce who can see what.

The framework already provides all the primitives needed. There is nothing generic to build here.

## Reconsidered If

A sufficiently common, well-defined subset of privacy controls emerged across multiple projects — for example, a boolean `is_profile_public` flag with clear framework-level semantics. That would be a narrow, explicit feature rather than a general privacy system.