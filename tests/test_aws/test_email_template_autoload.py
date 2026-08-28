"""
Tests for EmailTemplate.get_or_load_from_seed() — auto-loading templates
from seed JSON files when not found in the database.
"""
import ast
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from testit import helpers as th


@th.django_unit_setup()
def setup_email_autoload(opts):
    from mojo.apps.aws.models import EmailTemplate

    # Clean up any test templates from previous runs
    EmailTemplate.objects.filter(name__startswith="test_autoload_").delete()
    # Also clean up any auto-loaded "invite" template so seed tests are fresh
    EmailTemplate.objects.filter(name="invite").delete()

    # Create a template that exists in DB
    opts.existing = EmailTemplate.objects.create(
        name="test_autoload_existing",
        subject_template="Existing Subject",
        text_template="Existing Body",
    )

    # Create template with empty body (should NOT be overwritten by seed)
    opts.empty_body = EmailTemplate.objects.create(
        name="test_autoload_empty",
        subject_template="",
        text_template="",
    )


@th.django_unit_test()
def test_get_existing_template(opts):
    """Template exists in DB -> returned directly, no seed consulted."""
    from mojo.apps.aws.models import EmailTemplate

    tpl = EmailTemplate.get_or_load_from_seed("test_autoload_existing")
    assert tpl is not None, "Expected existing template to be returned"
    assert tpl.pk == opts.existing.pk, "Expected same DB record as setup created"
    assert tpl.subject_template == "Existing Subject", (
        f"Expected 'Existing Subject', got '{tpl.subject_template}'"
    )


@th.django_unit_test()
def test_missing_template_with_seed(opts):
    """Template missing from DB, seed file exists -> auto-loaded and saved."""
    from mojo.apps.aws.models import EmailTemplate

    # "invite" has a seed file but we deleted it from DB in setup
    tpl = EmailTemplate.get_or_load_from_seed("invite")
    assert tpl is not None, "Expected 'invite' template to be auto-loaded from seed"
    assert tpl.pk is not None, "Expected template to be persisted to DB"
    assert tpl.name == "invite", f"Expected name='invite', got '{tpl.name}'"
    assert tpl.subject_template != "", "Expected non-empty subject from seed"
    assert tpl.text_template != "", "Expected non-empty text body from seed"


@th.django_unit_test()
def test_missing_template_no_seed(opts):
    """Template missing from DB, no seed file -> returns None."""
    from mojo.apps.aws.models import EmailTemplate

    tpl = EmailTemplate.get_or_load_from_seed("test_autoload_nonexistent_xyz")
    assert tpl is None, "Expected None for template with no DB record and no seed file"


@th.django_unit_test()
def test_shipped_token_link_templates_render_real_urls(opts):
    """Every built-in link sender supplies token_url; seeds must use it."""
    from mojo.apps.aws.services.email_templates import load_shipped_templates

    templates = {row["name"]: row for row in load_shipped_templates()}
    expected = {
        "account_deactivate_confirm",
        "email_change_confirm",
        "email_verify",
        "email_verify_link",
        "invite",
        "magic_login_link",
        "password_reset_link",
    }
    th.assert_eq(expected - set(templates), set(),
                 f"every built-in token-link flow needs a shipped seed: "
                 f"{sorted(expected - set(templates))}")
    for name in sorted(expected):
        row = templates[name]
        combined = row["text_template"] + row["html_template"]
        th.assert_in("{{ token_url }}", combined,
                     f"{name} must render the server-resolved frontend URL")
        th.assert_true("YOUR_APP_HOST" not in combined,
                       f"{name} must never ship a literal placeholder host")
        th.assert_in("token_url", row["metadata"].get("context_keys", []),
                     f"{name} metadata must document its token_url context")


# Local names an account handler binds its send function to so a test can
# inject a fake (`sender = send if send is not None else user.send_template_email`,
# see mojo/apps/account/rest/verify.py). A call through such a name has no
# `.send_template_email` attribute for the AST walk to recognise, so without
# these the scanner below would go green while covering nothing.
SENDER_SEAM_NAMES = {"sender", "send"}


def _referenced_template_names(account_dir):
    """Every literal template name any account source asks to send."""
    referenced = set()
    for source_path in account_dir.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                if node.func.attr != "send_template_email":
                    continue
            elif isinstance(node.func, ast.Name):
                if node.func.id not in SENDER_SEAM_NAMES:
                    continue
            else:
                continue
            template_node = node.args[0] if node.args else None
            for keyword in node.keywords:
                if keyword.arg == "template_name":
                    template_node = keyword.value
                    break
            if isinstance(template_node, ast.Constant) and isinstance(
                    template_node.value, str):
                referenced.add(template_node.value)
    return referenced


@th.django_unit_test()
def test_account_email_senders_have_shipped_templates(opts):
    """Literal account template names must resolve to shipped seed files."""
    import inspect
    import mojo.apps.account as account_app
    from mojo.apps.aws.services.email_templates import load_shipped_templates

    account_dir = Path(inspect.getfile(account_app)).parent
    referenced = _referenced_template_names(account_dir)

    # Positive marker: the scan must actually SEE the seam-injected sends in
    # rest/verify.py. Without it this test passes vacuously the moment a
    # handler routes its send through an injectable local — the exact way the
    # coverage was lost when #3253 added the seam.
    for expected in ("email_verify", "email_verify_code"):
        th.assert_in(
            expected,
            referenced,
            "the scanner must see template names sent through an injected "
            "sender seam — add the local name to SENDER_SEAM_NAMES",
        )

    shipped = {row["name"] for row in load_shipped_templates()}
    th.assert_eq(
        sorted(referenced - shipped),
        [],
        "every literal account send_template_email reference needs a shipped seed",
    )


@th.django_unit_test()
def test_shipped_templates_never_contain_placeholder_hosts(opts):
    from mojo.apps.aws.services.email_templates import load_shipped_templates

    offenders = []
    for row in load_shipped_templates():
        combined = row["subject_template"] + row["text_template"] + row["html_template"]
        if "YOUR_APP_HOST" in combined:
            offenders.append(row["name"])
    th.assert_eq(
        offenders,
        [],
        "shipped templates must never expose a placeholder hostname to recipients",
    )


@th.django_unit_test()
def test_account_link_senders_build_token_urls(opts):
    """Every account link-email sender must resolve token_url server-side."""
    import inspect
    import mojo.apps.account as account_app

    account_dir = Path(inspect.getfile(account_app)).parent
    cases = {
        account_dir / "rest" / "verify.py": ["on_email_verify_send"],
        account_dir / "rest" / "user.py": [
            "on_register",
            "_send_email_change_confirm",
            "on_account_deactivate",
        ],
    }
    for source_path, function_names in cases.items():
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function_name in function_names:
            function_source = functions.get(function_name, "")
            th.assert_in(
                "build_token_url",
                function_source,
                f"{source_path.name}:{function_name} must resolve its frontend URL",
            )
            th.assert_in(
                "token_url",
                function_source,
                f"{source_path.name}:{function_name} must pass token_url to its template",
            )


@th.django_unit_test()
def test_autoloaded_template_persists(opts):
    """Second call returns same DB record — no file re-read."""
    from mojo.apps.aws.models import EmailTemplate

    # Ensure "invite" is not in DB
    EmailTemplate.objects.filter(name="invite").delete()

    tpl1 = EmailTemplate.get_or_load_from_seed("invite")
    assert tpl1 is not None, "Expected 'invite' to be auto-loaded"
    pk1 = tpl1.pk

    tpl2 = EmailTemplate.get_or_load_from_seed("invite")
    assert tpl2 is not None, "Expected 'invite' to be returned on second call"
    assert tpl2.pk == pk1, (
        f"Expected same DB record (pk={pk1}) on second call, got pk={tpl2.pk}"
    )


@th.django_unit_test()
def test_empty_body_not_overwritten(opts):
    """Template with empty body in DB -> NOT overwritten by seed."""
    from mojo.apps.aws.models import EmailTemplate

    tpl = EmailTemplate.get_or_load_from_seed("test_autoload_empty")
    assert tpl is not None, "Expected empty-body template to be returned from DB"
    assert tpl.pk == opts.empty_body.pk, "Expected the same DB record"
    assert tpl.subject_template == "", (
        "Expected empty subject_template to remain unchanged"
    )


@th.django_unit_test()
def test_malformed_seed_returns_none(opts):
    """Malformed seed JSON -> returns None, doesn't crash."""
    import inspect
    from mojo.apps.aws.models import EmailTemplate

    # Use inspect.getfile to locate the model's actual on-disk path —
    # os.path.abspath() of the dotted-module string is relative to the
    # current working directory, which breaks the test when run from a
    # consuming project (e.g. wmx_api) rather than from the django-mojo
    # repo root.
    model_file = inspect.getfile(EmailTemplate)
    seed_dir = os.path.join(
        os.path.dirname(os.path.dirname(model_file)),
        "seeds", "email_templates",
    )

    # Write a malformed JSON seed file
    bad_seed_path = os.path.join(seed_dir, "test_autoload_bad_json.json")
    try:
        with open(bad_seed_path, "w") as f:
            f.write("{this is not valid json!!!")

        tpl = EmailTemplate.get_or_load_from_seed("test_autoload_bad_json")
        assert tpl is None, "Expected None for malformed seed JSON"
    finally:
        # Clean up the temp seed file
        if os.path.exists(bad_seed_path):
            os.remove(bad_seed_path)


@th.django_unit_test()
def test_service_uses_autoload(opts):
    """send_with_template uses get_or_load_from_seed for template lookup."""
    import inspect
    from mojo.apps.aws.services import email as email_service

    # Verify the service function source references get_or_load_from_seed
    source = inspect.getsource(email_service.send_with_template)
    assert "get_or_load_from_seed" in source, (
        "Expected send_with_template to call get_or_load_from_seed, "
        "but it was not found in the function source"
    )


@th.django_unit_test()
def test_missing_only_installer_is_concurrency_idempotent(opts):
    from django.db import close_old_connections
    from mojo.apps.aws.models import EmailTemplate
    from mojo.apps.aws.services.email_templates import install_missing

    EmailTemplate.objects.filter(name="password_reset_code").delete()

    def install():
        close_old_connections()
        try:
            return install_missing()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda ignored: install(), range(2)))
    assert EmailTemplate.objects.filter(name="password_reset_code").count() == 1, \
        "Concurrent missing-only installers must converge on one unique template row"
    assert sum("password_reset_code" in result["created"] for result in results) == 1, \
        f"Exactly one concurrent installer should report the create: {results}"
