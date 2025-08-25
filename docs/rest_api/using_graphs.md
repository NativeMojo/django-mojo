# Using Graphs in the MOJO REST API

This guide is for REST API consumers—frontend developers, integration partners, script users—who want to understand and control the data shapes (serialization "graphs") returned by Django-MOJO-powered APIs. For backend (Django) developers configuring graphs, see `rest_developer.md`.

---

## What are Graphs?

A "graph" is a named view or representation of a resource for the API. Each graph controls:
- Which fields are returned
- How related/nested objects are represented (e.g., just IDs or full sub-objects)
- The overall shape of your REST responses

MOJO models often define multiple graphs (e.g., `basic`, `default`, `detailed`, or `list`), each tuned for a specific use-case.

---

## How to Select a Graph

When making a REST API request, you may choose the output "graph" by passing the `graph` query parameter.

**Syntax:**
```
GET /api/project/123?graph=detailed
GET /api/projects/?graph=basic
```
- If omitted, the model’s default graph is used.

---

## Graphs and Endpoint Results

### 1. List Endpoint Example

**Request:**
```http
GET /api/projects/?graph=basic
```
**Response:**
```json
[
  {"id": 1, "name": "Alpha"},
  {"id": 2, "name": "Bravo"}
]
```

---

### 2. Detailed Endpoint, Nested Graph

**Request:**
```http
GET /api/project/1?graph=default
```
_Model graph config:_
- `"default"`: returns `id`, `name`, and a related `owner` using that user's `"basic"` graph.

**Response:**
```json
{
  "id": 1,
  "name": "Alpha",
  "owner": {
    "id": 7,
    "username": "alice"
  }
}
```
- If the graph had specified `"owner": "detailed"`, you might see more user info.

---

### 3. Nested Graphs Across Models

APIs can return deeply nested (hierarchical) data by chaining graph names through relationships.

**Example:**
```http
GET /api/project/1?graph=detailed
```
If the "detailed" graph nests `owner` and `team` with their own sub-graphs, your response might look like:

```json
{
  "id": 1,
  "name": "Alpha",
  "owner": {
    "id": 7,
    "username": "alice",
    "email": "alice@example.com"
  },
  "team": {
    "id": 5,
    "name": "Team Rocket"
  }
}
```

---

## When to Use Which Graph?

- **`basic`:** Fastest and smallest; good for dropdowns, autocomplete, or dense lists.
- **`default`:** Generally the main graph for detail pages.
- **`detailed`:** Use when you need all available info—including related objects.
- **Custom graphs:** Some APIs may offer custom shapes tuned for specific frontend views or tasks.

Check your API’s developer documentation or the `/docs`/OpenAPI spec for a list of available graphs per resource.

---

## Tips for API Users

- If you’re not sure what graphs are supported, try requests with `graph=basic`, `graph=default`, `graph=list`, and observe the response shape.
- If you need related object details (not just IDs), pick or request a graph that nests the related resources.
- Too much data? Use a lighter graph.
- Not enough data? Try a more detailed graph—or ask your MOJO backend team to add a richer one!
- The graph parameter works both for listing endpoints (`/resources/`) and detail endpoints (`/resource/<pk>`).

---

## Error Handling

If you request a graph that is not defined, you’ll usually get an error or fall back to a default shape (this depends on server-side config).
- Always check your API responses for consistency.

---

## Example Scenario: On-the-Fly Graph Switching

To optimize a page, your frontend can:
- Fetch a project list with `graph=basic` for a slim table.
- Fetch a single project with `graph=detailed` when viewing or editing its profile.
- Fetch team/user details only when drilling down into those specific sub-resources.

---

## FAQ

**Q: Can I mix graphs for different relationships in a single request?**
- No, you select one graph per resource per request; nested graphs for related fields are controlled by the backend’s configuration.

**Q: Can the backend define which graphs are supported?**
- Yes. MOJO models specify available graphs; unsupported names will be rejected or ignored.

**Q: Is using a more detailed graph slower?**
- Potentially, as more data and more nested objects are retrieved/serialized. Use the minimal graph you need.

---

## Further Reading

- For backend graph customization: [`docs/rest_developer.md`](../rest_developer.md)
- For general API usage and authentication: see other docs under `rest_api/`

---
**Still have questions? Contact your backend team or open a docs issue!**