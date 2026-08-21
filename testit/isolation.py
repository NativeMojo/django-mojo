"""Default-tier isolation policy for repository test packages.

Deterministic, side-effect-free AST audit of test source files. Detects the
enumerated global-mutation grammar that has corrupted parallel default-tier
runs: patches of shared production objects, mutation of the shared settings
singleton, Django settings / environment / sys.modules writes, and writes to
protected configuration keys through the Setting ORM, the settings service
writers, or literal REST calls.

This is a deterministic guard for the enumerated grammar below — it proves
the mutation classes it can parse and resolve, and fails closed on
provenance it cannot resolve inside a default-core package. It is NOT a
proof that arbitrary dynamic Python cannot mutate shared state.

Nothing in this module imports or executes the code it scans.
"""
import ast
import os

from objict import objict


# Namespaces whose module attributes are production state shared by every
# parallel test thread in the process.
PRODUCTION_PREFIXES = ("mojo.", "django.", "testit.")

# The one shared settings singleton. `from mojo.helpers.settings import
# settings` (or any alias of it) resolves here.
SETTINGS_SINGLETON = "mojo.helpers.settings.settings"

# Django's own settings object.
DJANGO_SETTINGS = "django.conf.settings"

# Keys a test may freely write: reserved for test fixtures, read by nothing
# in production.
ALLOWED_KEY_PREFIX = "TESTIT_"

# Production configuration keys whose database rows are visible to every
# parallel module the moment they are written.
PROTECTED_KEYS = frozenset({
    "AUTH_CONFIG",
    "BASE_URL",
    "SECRET_KEY",
    "APIKEY_PERMS_PROTECTION",
    "MEMBER_PERMS_PROTECTION",
    "LOGIT_RETURN_REAL_ERROR",
    "ALLOWED_REDIRECT_URLS",
    "WEBAPP_BASE_URL",
    "GITHUB_WEBHOOK_SECRET",
    "WEBHOOK_SIGNATURE_HEADER",
    "MOJO_INSTALLATION_SLUG",
    "MOJO_INSTALLATION_UUID",
    "SECURE_POSTURE",
    "JOBS_CHANNELS",
})

# Whole families, matched by prefix.
PROTECTED_PREFIXES = (
    "EDGE_",
    "GEOFENCE_",
    "JOBS_WEBHOOK_",
    "ADMIN_FLEET_",
    "ADMIN_PROVIDER_",
    "GEOIP_",
    "WEBHOOK_",
    "SIGNING_",
)

# Terminal queryset/manager methods that write.
_ORM_WRITE_METHODS = frozenset({
    "create", "update", "delete", "bulk_create", "bulk_update",
    "get_or_create", "update_or_create",
})

# Settings service writers: (imported object name) -> index of the key arg.
_SERVICE_WRITERS = {
    "set_override": 1,
    "clear_override": 1,
    "set_auth_safe_fields": None,  # writes AUTH_CONFIG by definition
}

_REST_WRITE_METHODS = frozenset({"post", "put", "patch", "delete"})
_REST_PROTECTED_PATH = "/api/settings"

_ENVIRON_WRITE_METHODS = frozenset({"update", "setdefault", "pop", "clear", "popitem"})
_SYS_MODULES_WRITE_METHODS = frozenset({"update", "setdefault", "pop", "clear", "popitem"})


def violation(code, file, line, detail):
    return objict(code=code, file=file, line=line, detail=detail)


def is_protected_key(key):
    if not isinstance(key, str):
        return False
    if key.startswith(ALLOWED_KEY_PREFIX):
        return False
    if key in PROTECTED_KEYS:
        return True
    return key.startswith(PROTECTED_PREFIXES)


def _is_production_path(dotted):
    return isinstance(dotted, str) and dotted.startswith(PRODUCTION_PREFIXES)


class _Provenance:
    """What a local name is known to be."""
    IMPORTED_MODULE = "imported_module"      # import mojo.x / from mojo import x (module)
    IMPORTED_OBJECT = "imported_object"      # from mojo.x import y
    CONSTRUCTED = "constructed"              # name = CapWordsCall(...)
    RETURNED = "returned"                    # name = helper_call(...)
    CONSTANT = "constant"                    # name = "literal" / (literals,)
    UNKNOWN = "unknown"


class _FileScanner(ast.NodeVisitor):
    def __init__(self, filename):
        self.filename = filename
        self.violations = []
        # name -> objict(kind, dotted, value)
        self.names = {}

    # -- recording helpers ---------------------------------------------------

    def _flag(self, code, node, detail):
        self.violations.append(
            violation(code, self.filename, getattr(node, "lineno", 0), detail))

    def _bind(self, name, kind, dotted=None, value=None):
        self.names[name] = objict(kind=kind, dotted=dotted, value=value)

    def _lookup(self, name):
        return self.names.get(name)

    # -- name resolution -----------------------------------------------------

    def _dotted_of(self, node):
        """Resolve an expression to a dotted path when provable, else None.

        Name -> its recorded import path; Attribute chains extend the base.
        """
        if isinstance(node, ast.Name):
            info = self._lookup(node.id)
            if info and info.dotted:
                return info.dotted
            return None
        if isinstance(node, ast.Attribute):
            base = self._dotted_of(node.value)
            if base:
                return f"{base}.{node.attr}"
            return None
        return None

    def _resolve_string(self, node):
        """A literal string, or a module constant holding one. None otherwise."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            info = self._lookup(node.id)
            if info and info.kind == _Provenance.CONSTANT and isinstance(info.value, str):
                return info.value
        return None

    def _resolve_string_collection(self, node):
        """A literal tuple/list/set of strings (possibly via a module constant),
        or None when any element is unresolvable."""
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            out = []
            for element in node.elts:
                value = self._resolve_string(element)
                if value is None:
                    return None
                out.append(value)
            return out
        if isinstance(node, ast.Name):
            info = self._lookup(node.id)
            if info and info.kind == _Provenance.CONSTANT and isinstance(info.value, (list, tuple)):
                if all(isinstance(v, str) for v in info.value):
                    return list(info.value)
        single = self._resolve_string(node)
        if single is not None:
            return [single]
        return None

    # -- imports and assignments build the name table ------------------------

    def visit_Import(self, node):
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            dotted = alias.name if alias.asname else alias.name.split(".")[0]
            self._bind(bound, _Provenance.IMPORTED_MODULE, dotted=dotted)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            dotted = f"{module}.{alias.name}" if module else alias.name
            self._bind(bound, _Provenance.IMPORTED_OBJECT, dotted=dotted)
        self.generic_visit(node)

    def _record_assignment(self, target, value):
        if not isinstance(target, ast.Name):
            return
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            self._bind(target.id, _Provenance.CONSTANT, value=value.value)
            return
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            elements = []
            for element in value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    elements.append(element.value)
                else:
                    elements = None
                    break
            if elements is not None:
                self._bind(target.id, _Provenance.CONSTANT, value=elements)
                return
        if isinstance(value, ast.Name):
            info = self._lookup(value.id)
            if info:
                self.names[target.id] = objict(info)
                return
            self._bind(target.id, _Provenance.UNKNOWN)
            return
        if isinstance(value, ast.Call):
            terminal = None
            func = value.func
            if isinstance(func, ast.Name):
                terminal = func.id
            elif isinstance(func, ast.Attribute):
                terminal = func.attr
            if terminal and terminal[:1].isupper():
                # CapWords call — a constructor building a test-owned instance.
                self._bind(target.id, _Provenance.CONSTRUCTED)
            else:
                self._bind(target.id, _Provenance.RETURNED)
            return
        if isinstance(value, ast.Attribute):
            dotted = self._dotted_of(value)
            if dotted:
                self._bind(target.id, _Provenance.IMPORTED_OBJECT, dotted=dotted)
                return
        self._bind(target.id, _Provenance.UNKNOWN)

    # -- mutation checks -----------------------------------------------------

    def _attribute_mutation(self, node, verb):
        """node is an ast.Attribute being assigned to or deleted."""
        dotted = self._dotted_of(node.value)
        base_info = None
        if isinstance(node.value, ast.Name):
            base_info = self._lookup(node.value.id)

        if dotted == SETTINGS_SINGLETON or (
                dotted and dotted.startswith(SETTINGS_SINGLETON + ".")):
            self._flag(
                "settings_singleton_mutation", node,
                f"{verb} attribute '{node.attr}' of the shared settings "
                f"singleton ({SETTINGS_SINGLETON})")
            return
        if dotted == DJANGO_SETTINGS or (
                dotted and dotted.startswith(DJANGO_SETTINGS + ".")):
            self._flag(
                "django_settings_mutation", node,
                f"{verb} attribute '{node.attr}' of django.conf.settings")
            return
        if dotted and _is_production_path(dotted):
            self._flag(
                "production_attr_mutation", node,
                f"{verb} attribute '{node.attr}' of shared production object "
                f"'{dotted}'")
            return
        if base_info and base_info.kind in (
                _Provenance.CONSTRUCTED, _Provenance.CONSTANT):
            return

    def _subscript_mutation(self, node, verb):
        dotted = self._dotted_of(node.value)
        if dotted in ("os.environ", "environ"):
            self._flag("environ_mutation", node,
                       f"{verb} os.environ entry")
        elif dotted in ("sys.modules", "modules"):
            self._flag("sys_modules_mutation", node,
                       f"{verb} sys.modules entry")

    def visit_Assign(self, node):
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                self._attribute_mutation(target, "assignment to")
            elif isinstance(target, ast.Subscript):
                self._subscript_mutation(target, "assignment to")
            else:
                self._record_assignment(target, node.value)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        if isinstance(node.target, ast.Attribute):
            self._attribute_mutation(node.target, "augmented assignment to")
        elif isinstance(node.target, ast.Subscript):
            self._subscript_mutation(node.target, "augmented assignment to")
        self.generic_visit(node)

    def visit_Delete(self, node):
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                self._attribute_mutation(target, "deletion of")
            elif isinstance(target, ast.Subscript):
                self._subscript_mutation(target, "deletion of")
        self.generic_visit(node)

    # -- call analysis -------------------------------------------------------

    def _resolve_patch_callable(self, func):
        """Return 'patch', 'patch.object', 'patch.dict' when func is one of
        the unittest.mock patchers (through any import alias), else None."""
        dotted = self._dotted_of(func)
        if not dotted:
            return None
        for root in ("unittest.mock.patch", "mock.patch", "patch"):
            if dotted == root:
                return "patch"
            if dotted == root + ".object":
                return "patch.object"
            if dotted == root + ".dict":
                return "patch.dict"
        # `from unittest import mock` binds mock -> unittest.mock; the chain
        # above already covers mock.patch because _dotted_of extends it.
        return None

    def _check_patch_call(self, node, form):
        args = node.args
        if form in ("patch", "patch.dict"):
            if not args:
                return
            target = self._resolve_string(args[0])
            if target is not None:
                if _is_production_path(target):
                    self._flag(
                        "patch_shared", node,
                        f"{form}('{target}') replaces a shared production "
                        f"attribute visible to every parallel test thread")
                elif target in ("os.environ",):
                    self._flag("environ_mutation", node,
                               f"{form} of os.environ")
                elif target in ("sys.modules",):
                    self._flag("sys_modules_mutation", node,
                               f"{form} of sys.modules")
                return
            # Non-string first argument (patch.dict(os.environ, ...)).
            dotted = self._dotted_of(args[0])
            if dotted in ("os.environ", "environ"):
                self._flag("environ_mutation", node, f"{form} of os.environ")
                return
            if dotted in ("sys.modules", "modules"):
                self._flag("sys_modules_mutation", node, f"{form} of sys.modules")
                return
            if dotted and _is_production_path(dotted):
                self._flag(
                    "patch_shared", node,
                    f"{form} of shared production object '{dotted}'")
                return
            self._flag(
                "patch_unresolved", node,
                "patch target provenance cannot be resolved — name the "
                "production path directly or patch a locally constructed "
                "instance")
            return

        # patch.object(target, "attr", ...)
        if not args:
            return
        target = args[0]
        dotted = self._dotted_of(target)
        if dotted == SETTINGS_SINGLETON or (
                dotted and dotted.startswith(SETTINGS_SINGLETON + ".")):
            self._flag(
                "patch_shared", node,
                "patch.object of the shared settings singleton "
                f"({SETTINGS_SINGLETON})")
            return
        if dotted and _is_production_path(dotted):
            self._flag(
                "patch_shared", node,
                f"patch.object of shared production object '{dotted}'")
            return
        if dotted:
            # Resolvable but not a production namespace (tests.*, stdlib) —
            # allowed.
            return
        if isinstance(target, ast.Name):
            info = self._lookup(target.id)
            if info:
                if info.kind == _Provenance.CONSTRUCTED:
                    return
                if info.kind == _Provenance.RETURNED:
                    self._flag(
                        "patch_unresolved", node,
                        f"patch.object target '{target.id}' came back from a "
                        "helper call — its provenance cannot be proven local; "
                        "construct the instance in the test or patch by "
                        "explicit production path")
                    return
                if info.kind == _Provenance.IMPORTED_OBJECT and info.dotted \
                        and not _is_production_path(info.dotted):
                    return
        self._flag(
            "patch_unresolved", node,
            "patch.object target provenance cannot be resolved — name the "
            "production path directly or patch a locally constructed instance")

    def _chain_parts(self, node):
        """Unwrap an Attribute/Call chain: returns (root_node, [(name, call_node|None), ...])
        ordered root-first. `Setting.objects.filter(...).delete()` yields
        root=Name(Setting), parts=[('objects', None), ('filter', Call), ('delete', Call)].
        """
        parts = []
        current = node
        while True:
            if isinstance(current, ast.Call):
                func = current.func
                if isinstance(func, ast.Attribute):
                    parts.append((func.attr, current))
                    current = func.value
                    continue
                return func, list(reversed(parts))
            if isinstance(current, ast.Attribute):
                parts.append((current.attr, None))
                current = current.value
                continue
            return current, list(reversed(parts))

    def _setting_keys_from_call(self, call):
        """Resolve key=/key__in= kwargs of one call. Returns (keys, resolved)."""
        keys = []
        found = False
        for kw in call.keywords:
            if kw.arg == "key":
                found = True
                value = self._resolve_string(kw.value)
                if value is None:
                    return None, True
                keys.append(value)
            elif kw.arg == "key__in":
                found = True
                values = self._resolve_string_collection(kw.value)
                if values is None:
                    return None, True
                keys.extend(values)
        return keys, found

    def _check_setting_chain(self, node):
        """node is a Call. Detect Setting ORM writes with protected keys."""
        root, parts = self._chain_parts(node)
        if not isinstance(root, ast.Name):
            return False
        info = self._lookup(root.id)
        if not info or not info.dotted:
            return False
        if not info.dotted.endswith(".models.Setting") and info.dotted != "Setting":
            return False
        names = [p[0] for p in parts]
        if "objects" not in names:
            return False
        terminal = names[-1] if names else None
        if terminal not in _ORM_WRITE_METHODS:
            return False

        keys = []
        any_constraint = False
        unresolved = False
        for name, call in parts:
            if call is None:
                continue
            if name in ("filter", "exclude", "get") or name in _ORM_WRITE_METHODS:
                resolved_keys, found = self._setting_keys_from_call(call)
                if found:
                    any_constraint = True
                    if resolved_keys is None:
                        unresolved = True
                    else:
                        keys.extend(resolved_keys)
            if name == "bulk_create" and call.args:
                collection = call.args[0]
                if isinstance(collection, (ast.List, ast.Tuple)):
                    for element in collection.elts:
                        if isinstance(element, ast.Call):
                            resolved_keys, found = self._setting_keys_from_call(element)
                            if found:
                                any_constraint = True
                                if resolved_keys is None:
                                    unresolved = True
                                else:
                                    keys.extend(resolved_keys)
                else:
                    unresolved = True

        if unresolved or not any_constraint:
            self._flag(
                "protected_setting_unresolved", node,
                f"Setting.{terminal}() whose key constraint cannot be "
                "resolved — a dynamic or absent key cannot be proven outside "
                "the protected roster")
            return True
        protected = sorted({k for k in keys if is_protected_key(k)})
        if protected:
            self._flag(
                "protected_setting_write", node,
                f"Setting.{terminal}() writes protected configuration "
                f"key(s) {', '.join(protected)} visible to every parallel module")
        return True

    def _check_service_writer(self, node):
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name not in _SERVICE_WRITERS:
            return
        key_index = _SERVICE_WRITERS[name]
        if key_index is None:
            self._flag(
                "protected_setting_write", node,
                f"{name}() writes protected configuration through the "
                "settings service")
            return
        if len(node.args) > key_index:
            key = self._resolve_string(node.args[key_index])
            if key is None:
                self._flag(
                    "protected_setting_unresolved", node,
                    f"{name}() key argument cannot be resolved")
            elif is_protected_key(key):
                self._flag(
                    "protected_setting_write", node,
                    f"{name}('{key}') writes a protected configuration key")

    def _check_rest_write(self, node):
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        if func.attr not in _REST_WRITE_METHODS:
            return
        if not node.args:
            return
        path = self._resolve_string(node.args[0])
        if path is None:
            return
        if path == _REST_PROTECTED_PATH or path.startswith(_REST_PROTECTED_PATH + "/"):
            self._flag(
                "protected_rest_write", node,
                f"{func.attr.upper()} {path} writes production settings "
                "through the live server, visible to every parallel module")

    def _check_environ_calls(self, node):
        func = node.func
        if not isinstance(func, ast.Attribute):
            dotted = self._dotted_of(func)
            if dotted in ("os.putenv", "os.unsetenv"):
                self._flag("environ_mutation", node, f"{dotted}() call")
            return
        base = self._dotted_of(func.value)
        if base in ("os.environ", "environ") and func.attr in _ENVIRON_WRITE_METHODS:
            self._flag("environ_mutation", node,
                       f"os.environ.{func.attr}() call")
        elif base in ("sys.modules", "modules") and func.attr in _SYS_MODULES_WRITE_METHODS:
            self._flag("sys_modules_mutation", node,
                       f"sys.modules.{func.attr}() call")
        elif base == "os" and func.attr in ("putenv", "unsetenv"):
            self._flag("environ_mutation", node, f"os.{func.attr}() call")

    def visit_Call(self, node):
        form = self._resolve_patch_callable(node.func)
        if form:
            self._check_patch_call(node, form)
        else:
            if not self._check_setting_chain(node):
                self._check_service_writer(node)
                self._check_rest_write(node)
            self._check_environ_calls(node)
        self.generic_visit(node)


def scan_source(source, filename="<source>"):
    """Scan one test source text. Returns a list of violation objicts."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as error:
        return [violation("scan_error", filename, error.lineno or 0,
                          f"source could not be parsed: {error.msg}")]
    scanner = _FileScanner(filename)
    scanner.visit(tree)
    return scanner.violations


def scan_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
    except OSError as error:
        return [violation("scan_error", str(path), 0,
                          f"source could not be read: {error}")]
    return scan_source(source, filename=str(path))


def _iter_test_files(package_path):
    try:
        entries = sorted(os.listdir(package_path))
    except OSError:
        return
    for entry in entries:
        if not entry.endswith(".py"):
            continue
        if entry in ("__init__.py", "setup.py") or entry.startswith("_"):
            continue
        yield os.path.join(package_path, entry)


def scan_package(package_path):
    """Scan every test file of one package directory."""
    violations = []
    files_scanned = 0
    for path in _iter_test_files(package_path):
        files_scanned += 1
        violations.extend(scan_file(path))
    return objict(files_scanned=files_scanned, violations=violations)


def evaluate_package_state(config, violations, *, origin, has_config):
    """Validate one repository package against the two-state contract.

    Returns a list of problem strings (empty = valid). Applies only to
    records whose origin is the repository — consumer/application roots keep
    their existing selection contract.

    Valid states for a repository package:
      default: default_core=True, no requires_extra, no policy violation.
              serial=True is allowed only for a violation-free package that
              is serial for execution reasons (signal handlers), never as
              isolation cover for mutation.
      opt-in: default_core absent/False with a nonempty requires_extra;
              serial=True is mandatory whenever the policy finds mutation.
    """
    if origin != "django_mojo":
        return []
    problems = []
    if not has_config:
        problems.append(
            "no readable literal TESTIT config — a repository test package "
            "must declare default_core=True or a nonempty requires_extra "
            "(fail-closed; the permissive default is not inherited)")
        return problems

    config = config or {}
    default_core = bool(config.get("default_core", False))
    requires_extra = config.get("requires_extra") or []
    if isinstance(requires_extra, str):
        requires_extra = [item.strip() for item in requires_extra.split(",") if item.strip()]
    serial = bool(config.get("serial", False))
    has_violations = bool(violations)

    if default_core:
        if requires_extra:
            problems.append(
                "default_core=True cannot also declare requires_extra — a "
                "package is default or opt-in, never both")
        if has_violations:
            problems.append(
                "default_core=True but the isolation policy found "
                f"{len(violations)} mutation violation(s) — convert to local "
                "injection/test-owned data, or move the coverage to an "
                "opt-in serial package")
        if serial and has_violations:
            problems.append(
                "serial=True does not license mutation in the default tier — "
                "serial ordering is not process isolation")
    else:
        if not requires_extra:
            problems.append(
                "repository package declares neither default_core=True nor a "
                "nonempty requires_extra — it is in no valid state")
        if has_violations and not serial:
            problems.append(
                "opt-in package carries mutation violations but is not "
                "serial=True — requires_extra alone is not isolation: --all "
                "still runs eligible modules in parallel")
    return problems


def format_violations(violations):
    if not violations:
        return "no isolation violations"
    lines = []
    for row in violations:
        lines.append(f"{row.file}:{row.line}: [{row.code}] {row.detail}")
    return "\n".join(lines)
