# Listing & Pagination in the MOJO REST API

Learn how to fetch data in bulk, handle large result sets, and work efficiently with paginated responses from Django-MOJO REST APIs.

---

## Fetching Lists of Resources

To get a list of objects (such as projects, groups, users, etc.), make a GET request to the plural endpoint. For example:

```
GET /api/projects/
Authorization: Bearer <ACCESS_TOKEN>
```

**Standard Response:**
```json
{
  "count": 42,
  "start": 0,
  "size": 10,
  "results": [
    { "id": 1, "name": "Alpha", ... },
    { "id": 2, "name": "Beta", ... },
    ...
  ]
}
```

- `count`: Total number of items matching your query.
- `start`: Index of the first item in this batch.
- `size`: How many items are returned in this response.
- `results`: Your list of objects.

---

## Pagination: Navigating Large Result Sets

By default, most list endpoints return the first 10 items. To work with larger or specific subsets, use the `size` and `start` query parameters.

### Parameters

- **`size`**: Number of items you want per page (default: 10)
- **`start`**: Starting index (zero-based) for your page

**Examples:**
- Get the first 20 projects:
  ```
  GET /api/projects/?size=20
  ```
- Get projects 21–40:
  ```
  GET /api/projects/?start=20&size=20
  ```

---

## Typical Pagination Workflow

1. **First Page (default):**
   ```
   GET /api/projects/
   ```
   - Returns items 0–9, plus the total `count`.

2. **Next Page:**
   - Use `start = previous_start + previous_size`.
   - Example:
     ```
     GET /api/projects/?start=10&size=10
     ```
   - Continue until `start + size >= count`.

---

## Quick Tips

- Some resources may allow larger `size` (up to 100 or more), but extremely huge pages may be limited by the server.
- Always check the `count` field to know when you’ve reached the end of the list.
- Use filters and sorting to reduce result volume—see [filters_and_sorting.md](filters_and_sorting.md).
- Paginate in your frontend or automation using `start` and `size` to avoid missing or duplicating items.

---

## Example: Paginating Through Projects

Fetch all projects 10 at a time:

```python
import requests

url = "https://api.example.com/api/projects/"
token = "<ACCESS_TOKEN>"
start = 0
size = 10

while True:
    resp = requests.get(
        url, 
        headers={"Authorization": f"Bearer {token}"},
        params={"start": start, "size": size}
    )
    data = resp.json()
    for project in data["results"]:
        print(project)
    start += size
    if start >= data["count"]:
        break
```

---

## Bonus: Selecting Graphs for Listing

You can also use the `graph` parameter to control which fields you receive, optimizing for both speed and minimal data transfer.

**Example:**
```
GET /api/projects/?graph=basic&size=25
```

---

**Happy paginating!**