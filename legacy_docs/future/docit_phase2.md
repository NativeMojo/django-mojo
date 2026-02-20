# DocIt Phase 2: Markdown Processing and Rendering

## Overview

Phase 2 builds upon the foundational models and APIs from Phase 1 by introducing a robust Markdown processing and rendering pipeline. The primary goal is to transform the raw Markdown content stored in `Page` models into secure, feature-rich HTML. This will be achieved by integrating the `mistune` library and developing a flexible plugin system to extend Markdown capabilities.

## Goals for Phase 2

1.  **Design and Implement a Markdown Service**: Create a centralized service for converting Markdown to HTML.
2.  **Integrate `mistune` Library**: Use `mistune` as the core rendering engine for performance and flexibility.
3.  **Develop a Plugin System**: Build a system to automatically discover and apply custom Markdown plugins (e.g., for table of contents, syntax highlighting).
4.  **Update the `Page` Model**: Add a dynamic `html` property to the `Page` model that renders its Markdown `content` on-demand.
5.  **Extend Unit Tests**: Add tests for the new functionality to the existing test suite.

## File Structure Changes

```
mojo/apps/docit/
├── models/
│   └── page.py          # Modified
├── services/
│   └── markdown.py      # New
└── markdown_plugins/
    ├── __init__.py      # New
    ├── toc.py           # New (Example Plugin)
    └── syntax_highlight.py # New (Example Plugin)
```

## Implementation Details

### 1. Designing the Markdown Service (`services/markdown.py`)

A new `MarkdownRenderer` service will be created to handle all parsing and rendering logic. This service will be responsible for:
- Initializing `mistune` with a standard set of security features (e.g., HTML sanitization).
- Dynamically discovering and loading plugins from the `mojo/apps/docit/markdown_plugins/` directory.
- Providing a simple `render(markdown_text)` method that returns processed HTML.

### 2. Integrating `mistune` and Building the Plugin System

**Dependency**: The `mistune` library will be added to the project's dependencies in `pyproject.toml`. For syntax highlighting, `pygments` will also be added.

**Plugin Architecture**:
- Plugins will be defined as simple Python files within the `markdown_plugins` directory.
- Each plugin file will contain a function, for example `plugin(mistune_instance)`, which modifies the `mistune` renderer to add new rules or features.
- The `MarkdownRenderer` service will iterate through files in this directory, import them, and execute the `plugin` function, effectively registering all available plugins.

**Example Plugins**:
-   **Table of Contents (`toc.py`)**: A plugin to automatically generate a `[TOC]` tag and replace it with a nested list of document headings.
-   **Syntax Highlighting (`syntax_highlight.py`)**: A plugin to parse code blocks, apply syntax highlighting using `pygments`, and wrap them in the appropriate HTML and CSS classes.

### 3. Updating the Page Model (`models/page.py`)

The `Page` model will be updated to include a new property that leverages the `MarkdownRenderer` service. This makes accessing the rendered HTML seamless and intuitive.

```python
# mojo/apps/docit/models/page.py

from django.db import models
from mojo.models.meta import MojoModel
from mojo.apps.docit.services.markdown import MarkdownRenderer # New import

# ... existing Page model definition ...

class Page(models.Model, MojoModel):
    # ... existing fields ...

    @property
    def html(self):
        """
        Renders the Markdown content of the page to HTML.
        """
        renderer = MarkdownRenderer()
        rendered_html = renderer.render(self.content)
        return rendered_html

    # ... existing methods and RestMeta ...
```

This implementation ensures that:
- The `html` property is always up-to-date with the latest `content`.
- The rendering logic is cleanly separated into the `MarkdownRenderer` service.

## Testing Approach

All new tests will be added to the existing test suite at `tests/test_docit/docit_core.py`.

1.  **Markdown Service Tests**:
    -   Add a test case to verify that the `MarkdownRenderer` service correctly converts basic Markdown to HTML.
    -   Test that the service discovers and applies plugins from the `markdown_plugins` directory.
2.  **Plugin Tests**:
    -   Within the service tests, validate the output of each specific plugin (e.g., ensure the Table of Contents and Syntax Highlighting plugins work as expected).
3.  **Page Model `html` Property Test**:
    -   Add a test to an existing or new test case that creates a `Page` instance with Markdown content.
    -   Assert that the `page.html` property returns the correctly rendered HTML.

## Success Criteria

Phase 2 is complete when:

1.  ✅ The `mistune` and `pygments` libraries are added as project dependencies.
2.  ✅ A `MarkdownRenderer` service exists in `mojo/apps/docit/services/markdown.py`.
3.  ✅ The service can dynamically load plugins from the `mojo/apps/docit/markdown_plugins/` directory.
4.  ✅ At least two functional example plugins (TOC and syntax highlighting) are created.
5.  ✅ The `Page` model has a working `@property` named `html` that renders Markdown content to HTML.
6.  ✅ New unit tests for the markdown rendering are added to `tests/test_docit/docit_core.py`.

## Next Steps (Phase 3 Preview)

-   **Content Search**: Implement full-text search for Page content.
-   **Asset Integration**: Develop Markdown syntax for easily embedding `Asset` model images and files.
-   **Advanced Page Operations**: Build REST endpoints for moving, copying, and reordering pages in the hierarchy.