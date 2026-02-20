# MOJO REST API Examples

Ready-to-run examples for the most common workflows when using a MOJO-powered Django REST API.
Use these with curl, HTTPie, Postman, code, etc.—just plug in your domain, tokens, and data!

---

## 1. Login and Get a Token

```sh
curl -X POST https://api.example.com/api/login \
  -H "Content-Type: application/json" \
  -d '{"username": "youruser", "password": "yourpass"}'
```
_Response:_
```json
{
  "status": true,
  "data": {
    "access_token": "...",
    "refresh_token": "...",
    "user": { "id": 1, "username": "youruser" }
  }
}
```

---

## 2. List Projects (Paginated)

```sh
curl -X GET "https://api.example.com/api/projects/?size=5" \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

---

## 3. Fetch a Single Project With Details

```sh
curl -X GET "https://api.example.com/api/project/42?graph=default" \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

---

## 4. Create a New Project

```sh
curl -X POST https://api.example.com/api/project/ \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
        "name": "Apollo",
        "description": "Moon mission",
        "is_active": true
      }'
```

---

## 5. Update a Project

```sh
curl -X POST https://api.example.com/api/project/42/ \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"name": "Apollo 2", "is_active": false}'
```

---

## 6. Delete a Project

```sh
curl -X DELETE https://api.example.com/api/project/42/ \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

---

## 7. Filter Projects (Active, Name Contains)

```sh
curl -X GET "https://api.example.com/api/projects/?is_active=true&name__icontains=apollo" \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

---

## 8. Sort Users by Date Created (Newest First)

```sh
curl -X GET "https://api.example.com/api/users/?sort=-created" \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

---

## 9. Paginate Users

```sh
curl -X GET "https://api.example.com/api/users/?start=10&size=10" \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

---

## 10. Get User Profile ("Me")

```sh
curl -X GET https://api.example.com/api/user/me \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

---

## 11. Refresh Your Token

```sh
curl -X POST https://api.example.com/api/refresh_token \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "<REFRESH_TOKEN>"}'
```

---

## 12. Error Example: Forbidden (No Permission)

```sh
curl -X GET https://api.example.com/api/users/5 \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```
_Example Response:_
```json
{
  "status": false,
  "error": "Permission denied: User",
  "code": 403,
  "is_authenticated": true
}
```

---

## 13. Advanced: Use a Nested Graph for Related Objects

```sh
curl -X GET "https://api.example.com/api/project/42?graph=detailed" \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```
_Response (if graph includes nested 'owner' or 'team'):_
```json
{
  "id": 42,
  "name": "Apollo",
  "owner": {
     "id": 7,
     "username": "sally"
  },
  "team": {
     "id": 4,
     "name": "Flight Ops"
  }
}
```

---

## Pro Tips

- Replace `<ACCESS_TOKEN>` and `<REFRESH_TOKEN>` with your real tokens from login.
- Use the `graph` parameter for richer or sparser results.
- Use `size` and `start` for efficient client-side pagination.
- Combine filters and sorting for precise queries.
- See API docs for more resource-specific details.

---

**That’s it! Modify and use these examples for fast, reliable MOJO REST API development.**