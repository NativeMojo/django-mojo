# MOJO REST API Docs

Welcome to the user documentation for Django-MOJO’s REST API! This directory contains quick-access guides, examples, and references for anyone interacting with a MOJO-powered API—frontend devs, integrators, testers, mobile, scripts, and automation.

---

## 🏁 Getting Started

1. **[Overview: How the API Works](overview.md)**
2. **[How to Authenticate (Login, JWT Tokens)](authentication.md)**
3. **[Listing Data & Handling Pagination](listing_and_pagination.md)**
4. **[Filtering, Sorting, and Searching](filters_and_sorting.md)**
5. **[Controlling Response Format with Graphs](using_graphs.md)**
6. **[Error Messages: Meanings and Handling](errors.md)**
7. **[Copy-Paste Ready Examples](examples.md)**

---

## 🧑‍💻 What’s in These Docs?

- **Practical, hands-on examples:** Use curl, HTTPie, JS fetch, etc. to try out the API immediately.
- **Short and clear guides:** Every doc is focused, with tips for best results.
- **Designed for API users:** No Django knowledge required to get started!

---

## 🔄 For Django/MOJO Developers

If you’re building models, customizing endpoints, or working on the backend, see the in-depth developer docs in [../](../README.md):

- Developer Quickstart
- Permissions
- RestMeta/Graph docs
- Helpers/utilities, etc.

---

## 💡 Tips

- Most calls require authentication—**see [authentication.md](authentication.md)** for how to get/refresh your token!
- Use the `graph` parameter to control which fields and relationships show up in responses.
- Play with `size`, `start`, filters, and sorts to get exactly what you need.

---

## 📝 Contribute or Improve Docs

- Is something missing, unclear, or not working as described?
- PRs, issues, and example additions are always welcome!

---

**MOJO REST API:**  
_Secure by default. Flexible by design. Happy building!_