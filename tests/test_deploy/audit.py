import os
import tempfile
import time

from testit import helpers as th


@th.unit_test("audit policy is the sudo watch only")
def test_render_policy_is_selective(opts):
    from mojo.deploy.audit import AuditError, render_policy

    text = render_policy(1000)
    th.assert_in("-w /usr/bin/sudo -p x -k mojosec-sudo", text,
                 "sudo executable execution must keep an exact path watch")
    th.assert_true("-S execve" not in text and "execveat" not in text,
                   "the policy must not record every exec any more")
    th.assert_true("mojosec-app-exec" not in text and "mojosec-root-exec" not in text,
                   "the retired exec rule keys must not survive in the managed policy")
    th.assert_eq([line for line in text.splitlines() if line.startswith("-a")], [],
                 "the managed policy must carry no syscall rule at all")
    th.assert_true("task,never" not in text,
                   "the AL2023 task-never seed must not survive managed policy")
    for flag in ("-D", "-b 8192", "-f 1", "-r 0"):
        th.assert_in(flag, text.splitlines(), f"the policy must keep {flag}")
    with th.assert_raises(AuditError):
        render_policy(0)


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
