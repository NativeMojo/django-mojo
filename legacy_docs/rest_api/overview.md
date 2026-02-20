# MOJO REST API Overview

Welcome to the Django-MOJO REST API! This quick guide gives you the know-how and links needed to start consuming MOJO-powered APIs in any client (frontend, mobile, script, integration).

---

## What is MOJO REST?

MOJO transforms Django models into secure, flexible API endpoints—so you can interact with your application’s data using standard HTTP(S) requests.

You can:
- List, retrieve, create, update, and delete almost any resource (CRUD).
- Filter, sort, and paginate large data sets.
- Authenticate securely (typically with JWT tokens).
- Fetch related/nested data using “graphs” to control response shapes.

---

## Main Concepts

- **Authentication:** Most endpoints require users to log in with username/password and use a JWT token.
- **Graphs:** Control how much and what shape of data you get back via a `graph` parameter.
- **Filtering & Sorting:** Query with flexible parameters to get exactly the data you want.
- **Pagination:** Handle massive lists easily, page by page.

---

## Navigating the API Docs

- [Authentication](authentication.md)
- [Listing & Pagination](listing_and_pagination.md)
- [Filtering & Sorting](filters_and_sorting.md)
- [Using Graphs](using_graphs.md)
- [Error Responses](errors.md)
- [Examples](examples.md)

---

## Making Your First Call

1. **Log in and get a token** via the API (see [Authentication](authentication.md)).
2. **Make requests** with your token in the `Authorization` header:
    ```
    Authorization: Bearer <your_jwt_token>
    ```
3. **Pass query params** (like `graph`, `size`, `start`, filters) to get precisely the data needed.

**Example:**
```
GET /api/project/123?graph=default
Authorization: Bearer eyJ0eXAiOiJKV...
```

---

## Quick Tips

- **Use a REST client** (HTTPie, Postman, curl, JS fetch, etc.)—it’s all standard HTTP.
- **All standard CRUD actions** are available on core resources.
- **Read [using_graphs.md](using_graphs.md)** if you want nested/related data!
- **All input/output is JSON.**
- **Questions?** Start with the examples, then check with your backend/developer team.

---

Happy building with MOJO REST APIs!