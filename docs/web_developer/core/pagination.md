# Pagination — REST API Reference

All list endpoints are paginated. Use `start` and `size` parameters to page through results.

## Parameters

| Param | Alias | Default | Description |
|---|---|---|---|
| `start` | `offset` | `0` | Zero-based index of first record to return |
| `size` | `limit` | `10` | Number of records per page |

```
GET /api/myapp/book?start=0&size=20      # first page, 20 per page
GET /api/myapp/book?start=20&size=20     # second page
GET /api/myapp/book?offset=40&limit=20   # third page (alias params)
```

## Response

```json
{
  "status": true,
  "count": 142,
  "start": 20,
  "size": 20,
  "data": [...]
}
```

| Field | Description |
|---|---|
| `count` | Total number of matching records (before pagination) |
| `start` | The `start` value used for this page |
| `size` | The `size` value used for this page |
| `data` | Array of records for this page |

## Calculating Pages

```
total_pages = ceil(count / size)
next_start  = start + size   # if start + size < count
```

## Example: Fetching All Records

```python
start = 0
size = 100
all_items = []

while True:
    resp = requests.get(url, params={"start": start, "size": size}, headers=auth)
    data = resp.json()
    all_items.extend(data["data"])
    if start + size >= data["count"]:
        break
    start += size
```
