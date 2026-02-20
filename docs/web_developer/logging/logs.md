# Log API — REST API Reference

## Permissions Required

- `view_logs` or `manage_users`

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/logit/log` | List log entries |
| GET | `/api/logit/log/<id>` | Get single log entry |

## List Logs

**GET** `/api/logit/log`

```
GET /api/logit/log?level=error&sort=-created&size=50
```

**Response:**

```json
{
  "status": true,
  "count": 142,
  "start": 0,
  "size": 50,
  "data": [
    {
      "id": 9001,
      "created": "2024-01-15T10:30:00Z",
      "level": "error",
      "kind": "order:payment:failed",
      "log": "Payment declined for order 42",
      "uid": 5,
      "username": "alice@example.com",
      "model_name": "orders.Order",
      "model_id": 42,
      "path": "/api/orders/order/42",
      "method": "POST",
      "ip": "1.2.3.4"
    }
  ]
}
```

## Filtering

| Filter | Example | Description |
|---|---|---|
| `level` | `?level=error` | Filter by log level |
| `kind` | `?kind=order:payment:failed` | Exact kind match |
| `kind__startswith` | Filter by kind prefix | Prefix match |
| `uid` | `?uid=42` | Filter by user ID |
| `model_name` | `?model_name=orders.Order` | Filter by model |
| `model_id` | `?model_id=42` | Filter by record ID |
| `search` | `?search=payment` | Full text in log message |

## Date Range

```
GET /api/logit/log?dr_start=2024-01-01&dr_end=2024-01-31&level=error
```

## Available Graphs

| Graph | Fields |
|---|---|
| `basic` | id, created, level, kind, log, uid, username |
| `default` | basic + model_name, model_id, path, method, ip |
