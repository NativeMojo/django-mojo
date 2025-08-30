# Incident Management System (Developer Guide)

This document provides a detailed guide for Django developers working with the internal mechanics of the MOJO Incident Management System.

---

## Core Concepts & Data Models

The system is built around four primary models: `Event`, `Rule`, `RuleSet`, and `Incident`. The flow is designed to be robust and extensible, allowing for complex event processing and automated incident creation.

### 1. Event

- **Model:** `mojo.apps.incident.models.Event`
- **Purpose:** An `Event` is the fundamental unit of the system. It represents a single, discrete occurrence that needs to be recorded and potentially acted upon. This could be anything from a failed login attempt, a security alert from an external service like OSSEC, a 404 error, or a custom business logic event.
- **Key Fields:**
    - `level`: An integer (0-15) indicating the severity of the event. By default, events with a level of 7 or higher can automatically be escalated to an `Incident`.
    - `category`: A string used to group similar events (e.g., "auth", "ossec", "api_error"). This is the primary key for matching events to a `RuleSet`.
    - `metadata`: A flexible JSON field that stores all the detailed attributes of the event. The rule engine operates on the key-value pairs within this field.

### 2. RuleSet & Rule

- **Models:** `mojo.apps.incident.models.RuleSet`, `mojo.apps.incident.models.Rule`
- **Purpose:** This is the heart of the decision-making engine. A `RuleSet` is a container for one or more `Rule` objects and defines how they should be evaluated.

#### RuleSet Logic
A `RuleSet` is the top-level object that the system checks against an incoming `Event`.

- **Matching:** A `RuleSet` is matched to an `Event` based on the `category` field. All `RuleSet`s in a given category are checked in order of their `priority`.
- **`match_by` Behavior:**
    - `0` (All): All `Rule`s within the `RuleSet` must evaluate to `True` for the `RuleSet` to be considered a match.
    - `1` (Any): Any single `Rule` within the `RuleSet` can evaluate to `True` for the `RuleSet` to match.
- **Bundling:** The `bundle_minutes` and `bundle_by` fields control how events are grouped into a single `Incident` to reduce noise. For example, you can configure a `RuleSet` to bundle all events from the same `hostname` within a 10-minute window into one `Incident`.
- **Handlers:** The `handler` field defines what action to take when a `RuleSet` matches and creates a *new* `Incident`. See the "Handlers" section below for more details.

#### Rule Logic
A `Rule` is a single, specific condition to be checked against an `Event`'s `metadata`.

- **`field_name`**: The key within the `Event.metadata` dictionary to check (e.g., "source_ip", "level", "http_status").
- **`comparator`**: The logical operation to perform. Supported comparators include:
    - `==`, `eq`: Equal to
    - `>`, `>=`, `<`, `<=`: Greater/less than
    - `contains`: The value in the event's metadata contains the rule's `value`.
    - `regex`: The value in the event's metadata matches the regular expression in the rule's `value`.
- **`value` & `value_type`**: The value to compare against, and the type (`int`, `float`, `str`) to cast both the event's data and the rule's value to before comparison.

### 3. Incident

- **Model:** `mojo.apps.incident.models.Incident`
- **Purpose:** An `Incident` is a high-level record that is created when an `Event` (or a group of events) is deemed significant enough to require attention. An `Incident` can be linked to multiple `Event`s.

### 4. IncidentHistory

- **Model:** `mojo.apps.incident.models.IncidentHistory`
- **Purpose:** This model tracks all changes and notes related to an `Incident`, providing a complete audit trail.

---

## Event Processing Flow

1.  **Ingestion**: An event enters the system. This can happen in several ways:
    - **Via REST API**: The endpoints at `/api/ossec/alert` are specifically designed to parse and report events from OSSEC.
    - **Via `report_event` helper**: The function `mojo.apps.incident.reporter.report_event` can be called from anywhere in the codebase to submit a new event.
    - **Via `MojoModel.report_incident`**: A convenience method on `MojoModel` that wraps the `report_event` helper.

2.  **Creation & Metadata Sync**: The `report_event` function creates an `Event` instance. The `event.sync_metadata()` method is called to ensure all key fields (`level`, `category`, `source_ip`, etc.) are copied into the `metadata` JSON blob, making them accessible to the rule engine.

3.  **Publication**: The `event.publish()` method is called. This is the trigger for the core logic.

4.  **Rule Evaluation**:
    - Inside `publish()`, the system calls `RuleSet.check_by_category(event.category, event)`.
    - This retrieves all `RuleSet`s matching the event's category, ordered by priority.
    - It iterates through each `RuleSet` and calls `ruleset.check_rules(event)`.
    - `check_rules` applies the `match_by` logic (ALL or ANY) and evaluates each `Rule` within the set against the `event.metadata`.
    - The first `RuleSet` that matches is returned.

5.  **Incident Creation/Bundling**:
    - If a `RuleSet` was matched OR the `event.level` meets the `INCIDENT_LEVEL_THRESHOLD`, the system proceeds to `event.get_or_create_incident(rule_set)`.
    - This method uses the `bundle_by` and `bundle_minutes` criteria from the matched `RuleSet` to see if a similar, recent `Incident` already exists.
    - If a suitable `Incident` is found, the event is linked to it. If not, a new `Incident` is created.

6.  **Handler Execution**:
    - If a **new** `Incident` was created as a result of a `RuleSet` match, the `ruleset.run_handler(event, incident)` method is called.

---

## Handlers

Handlers are actions that are automatically executed when a `RuleSet` triggers the creation of a new `Incident`. They are defined in the `handler` field of a `RuleSet` using a URL-like syntax.

- **`task://task_name?param1=value1`**: Executes a background task. (Requires task runner integration).
- **`email://recipient@example.com`**: Sends an email notification. (Requires email backend integration).
- **`notify://recipient`**: Sends a notification via a configured system (e.g., push, SMS).

The handler logic is implemented in `mojo/apps/incident/handlers/`. These are currently placeholders and must be integrated with your project's specific task, email, and notification systems.
