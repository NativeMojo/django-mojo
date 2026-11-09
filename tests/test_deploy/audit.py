import os
import tempfile
import time

from testit import helpers as th


@th.unit_test("audit policy is the sudo watch alone")
def test_render_policy_is_selective(opts):
    from mojo.deploy.audit import AuditError, render_policy

    text = render_policy(1000)
    th.assert_in("-w /usr/bin/sudo -p x -k mojosec-sudo", text,
                 "sudo executable execution must have an exact path watch")
    th.assert_true("-S execve" not in text and "execveat" not in text,
                   "the retired exec capture must not come back: it was ~800 records a minute")
    for retired in ("mojosec-root-exec", "mojosec-app-exec"):
        th.assert_true(retired not in text,
                       f"the retired rule key must not be rendered: {retired}")
    th.assert_true("fork" not in text and "clone" not in text,
                   "the policy must not record fork-only process activity")
    th.assert_true("task,never" not in text,
                   "the AL2023 task-never seed must not survive managed policy")
    th.assert_true(text.startswith("# managed-by: django-mojo mojosec-audit-v1\n-D\n"),
                   "the managed marker and the delete-all directive must lead the policy")
    with th.assert_raises(AuditError):
        render_policy(0)


@th.unit_test("Audit command status rejects malformed numerics")
def test_status_numeric_contract_is_bounded(opts):
    from mojo.deploy import audit

    healthy_status = (
        "enabled 1\nfailure 1\nrate_limit 0\nbacklog_limit 8192\n"
        "backlog 0\nlost 0\n")
    parsed = audit.parse_status(healthy_status + "pid 123\nfuture_counter 9\n")
    th.assert_eq(tuple(parsed), audit.STATUS_FIELDS,
                 "only the recognized status fields may cross the command boundary")
    for value in (healthy_status + "enabled 1\n",
                  healthy_status.replace("lost 0\n", ""),
                  healthy_status.replace("lost 0", "lost nope"),
                  healthy_status.replace("lost 0", f"lost {2 ** 63}")):
        with th.assert_raises(audit.AuditError):
            audit.parse_status(value)


@th.unit_test("audit inventory rejects unknown rule sources")
def test_inventory_only_admits_known_seed_or_managed(opts):
    from mojo.deploy.audit import AuditError, inventory_sources

    with tempfile.TemporaryDirectory() as root:
        rules = os.path.join(root, "rules.d")
        os.mkdir(rules)
        seed = os.path.join(rules, "audit.rules")
        with open(seed, "w", encoding="utf-8") as handle:
            handle.write("-D\n-a task,never\n")
        found = inventory_sources(rules, generated_path="", active_rules="-a task,never\n")
        th.assert_eq(found["state"], "seed", "the exact AL2023 seed should be admitted")
        with open(seed, "a", encoding="utf-8") as handle:
            handle.write("-w /tmp -p wa\n")
        with th.assert_raises(AuditError):
            inventory_sources(rules, generated_path="", active_rules="-a task,never\n")


@th.unit_test("audit inventory adopts an empty rules.d with inert active rules")
def test_inventory_adopts_empty_rules_dir(opts):
    from mojo.deploy.audit import AuditError, inventory_sources

    with tempfile.TemporaryDirectory() as root:
        rules = os.path.join(root, "rules.d")
        os.mkdir(rules)
        found = inventory_sources(rules, generated_path="", active_rules="-a never,task\n")
        th.assert_eq(found["state"], "seed",
                     "an empty rules.d with inert active rules is a never-adopted "
                     "node and must adopt exactly like the pristine seed (item 2002)")
        with th.assert_raises(AuditError):
            inventory_sources(rules, generated_path="", active_rules="-w /etc -p wa\n")


@th.unit_test("audit inventory admits the stock AL2023 seed with its comments")
def test_inventory_admits_commented_stock_seed(opts):
    from mojo.deploy.audit import AuditError, inventory_sources

    # Byte-for-byte shape of the file the distro actually ships (observed in
    # production, item 2002): the seed directives wrapped in comments and
    # blank lines. Classification must read effective directives, not bytes.
    stock = (
        "## This set of rules is to suppress the performance effects of the\n"
        "## audit system. The result is that you only get hardwired events.\n"
        "-D\n"
        "\n"
        "## This suppresses syscall auditing for all tasks started\n"
        "## with this rule in effect.  Remove it if you need syscall\n"
        "## auditing.\n"
        "-a task,never\n"
        "\n"
    )
    with tempfile.TemporaryDirectory() as root:
        rules = os.path.join(root, "rules.d")
        os.mkdir(rules)
        with open(os.path.join(rules, "audit.rules"), "w", encoding="utf-8") as handle:
            handle.write(stock)
        found = inventory_sources(rules, generated_path="", active_rules="-a never,task\n")
        th.assert_eq(found["state"], "seed",
                     "the distro-shipped seed file must classify as seed despite "
                     "its comments and blank lines (item 2002)")
        with open(os.path.join(rules, "extra.rules"), "w", encoding="utf-8") as handle:
            handle.write("# watch passwd\n-w /etc/passwd -p wa\n")
        with th.assert_raises(AuditError):
            inventory_sources(rules, generated_path="", active_rules="-a never,task\n")
