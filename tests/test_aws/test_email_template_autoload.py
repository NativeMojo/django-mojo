"""
Tests for EmailTemplate.get_or_load_from_seed() — auto-loading templates
from seed JSON files when not found in the database.
"""
import json
import os
import tempfile

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
