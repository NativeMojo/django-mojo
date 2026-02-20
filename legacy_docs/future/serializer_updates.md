# Plan to Refactor Serializer Manager

This document outlines a plan to clean up the serializer manager, remove duplicate code, clarify the lazy-loading mechanism, and streamline the overall structure of the `mojo.serializers` package.

## 1. Analysis of Current State

- **Duplicate Code:** `mojo/serializers/core/manager.py` contains duplicate definitions for `set_default_serializer` and `get_performance_stats`.
- **Confusing Structure:** The presence of `mojo/serializers/manager.py` as a backward-compatibility layer for `mojo/serializers/core/manager.py` is confusing. The main entry point for the package is intended to be `mojo/serializers/__init__.py`.
- **Lazy Loading:** Serializers are loaded via `importlib` when registered, which happens on the first call to `get_serializer_manager()`. This is a form of lazy loading, as it avoids importing all serializers at application startup.
- **Usage in Codebase:** `mojo/models/rest.py` uses the deprecated `mojo.serializers.manager` import path.

## 2. Proposed Refactoring Plan

### Step 1: Clean up `mojo.serializers.core.manager.py`

The primary implementation of the serializer manager will be cleaned up.

- **Action:** Remove the duplicate function definitions for `set_default_serializer` and `get_performance_stats` at the end of the file.
- **Rationale:** This eliminates redundant code and makes the file easier to maintain.

### Step 2: Consolidate Public API and Update Imports

The public API for serializers should be consistently accessed through the `mojo.serializers` package. The deprecated `mojo.serializers.manager` module should be removed.

- **Action:**
    1.  Globally search for imports from `mojo.serializers.manager`.
    2.  Update all found imports to use `from mojo.serializers import ...` instead. For example, `from mojo.serializers.manager import get_serializer_manager` in `mojo/models/rest.py` will be changed to `from mojo.serializers import get_serializer_manager`.
    3.  After updating all imports, delete the file `mojo/serializers/manager.py`.
- **Rationale:** This enforces a single, clear public API for the serializers package, removes the deprecated compatibility layer, and resolves the confusion between the two `manager.py` files.

### Step 3: Verify Lazy Loading Mechanism

The current lazy loading approach is sound, but we should document it clearly.

- **Action:** Add a comment to the `SerializerRegistry` class in `mojo/serializers/core/manager.py` explaining the lazy-loading mechanism: serializers are imported via `importlib` upon registration, and default serializers are registered on the first use of the `get_serializer_manager` function.
- **Rationale:** This clarifies for future developers how and when serializers are loaded into memory, addressing the user's query about lazy loading.

## 3. Summary of Changes

1.  **Remove duplicate code** in `mojo/serializers/core/manager.py`.
2.  **Update all internal imports** to point to `mojo.serializers`.
3.  **Delete the backward-compatibility file** `mojo/serializers/manager.py`.
4.  **Add documentation** to clarify the lazy-loading implementation.

This plan will result in a cleaner, more maintainable, and less confusing serializer package.
