# Django-MOJO Docs: Developer Guide & Reference Index

Welcome to the documentation for Django-MOJO, a lightweight yet powerful REST and authentication framework for Django projects. This set of docs is focused on Django/MOJO backend developers—those building, customizing, or extending applications with MOJO.

---

## 📚 Developer Core Docs

- **[Developer Quickstart & Coding Guide](developer_guide.md)**
- **[Permissions & Security Model](permissions.md)**
- **[REST Developer Deep Dive (RestMeta, Graphs)](rest_developer.md)**
- **[Decorators & Helpers](decorators.md), [Helpers (utility modules)](helpers.md)**
- **[Auth System Under the Hood](auth.md)**
- **[Task Runner & Cron Scheduling](tasks.md), [Cron Patterns](cron.md)**
- **[Metrics & Monitoring](metrics.md)**
- **[Testing Framework](testit.md)**
- **[Future Docs, Patches, and Ideas](future/patches.md)**

---

## 🔗 REST API User Docs

Are you integrating with a MOJO-powered REST API? All documentation for REST consumers (frontend devs, mobile, 3rd-party, QA, etc.) is housed in [docs/rest_api/](rest_api/):

- **[API Overview](rest_api/overview.md)**
- **[Authentication (JWT, login, tokens)](rest_api/authentication.md)**
- **[Listing & Pagination](rest_api/listing_and_pagination.md)**
- **[Filtering & Sorting Results](rest_api/filters_and_sorting.md)**
- **[Using Graphs—Select Your Response Shape](rest_api/using_graphs.md)**
- **[API Error Reference](rest_api/errors.md)**
- **[End-to-End Usage Examples](rest_api/examples.md)**

---

## 🧭 Navigating This Documentation

- **Developer?**&nbsp; Start with the Quickstart—then jump to Permissions, RestMeta/Graphs, Helpers, and Patterns as you build and maintain your application.
- **API User/Integrator?**&nbsp; See the [rest_api/](rest_api/) directory for usage instructions, guides, and examples.

The docs are divided for clarity:  
- Everything in this root `docs/` folder is for backend, model, and framework authors.  
- Everything in `docs/rest_api/` is for consumers of the API.

---

## 📝 Contributing & Improving Docs

- Find a gap or unclear section? Please suggest edits, add a guide, or submit a PR.
- We welcome additional examples, best practices, and clarifications for both devs and API users.

---

**Django-MOJO:**<br>
Simple. Secure. Scalable.  
_Fast and clear Django APIs for any use case._