from testit import helpers as th


def _record(serial, kind, **values):
    record = {
        "_BOOT_ID": "a" * 32,
        "__MONOTONIC_TIMESTAMP": str(values.pop("monotonic", 100)),
        "MESSAGE": f"audit(1780000000.123:{serial}): type={kind}",
        "_AUDIT_TYPE_NAME": kind,
    }
    record.update(values)
    return record


@th.unit_test("audit compounds assemble across polls and terminate only at EOE")
def test_compound_assembly(opts):
    from mojo.mojosec.lineage import CompoundAssembler

    assembler = CompoundAssembler()
    first = assembler.ingest([
        _record(77, "EXECVE", argc="2", a0="/usr/bin/sudo", a1="-n"),
        _record(77, "SYSCALL", pid="42", ppid="10", auid="1000", uid="1000",
                euid="0", ses="9", tty="pts0", exe="/usr/bin/sudo"),
    ])
    th.assert_eq(first["complete"], [], "a compound without EOE must remain pending")
    second = assembler.ingest([_record(77, "EOE")])
    th.assert_eq(len(second["complete"]), 1, "EOE must complete the pending compound")
    node = second["complete"][0]
    th.assert_eq(node["pid"], 42, "the syscall PID must survive compound assembly")
    th.assert_eq(node["argv"], ["/usr/bin/sudo", "-n"],
                 "EXECVE arguments must be reconstructed by numeric index")


@th.unit_test("audit compounds reject conflicting duplicate fields")
def test_compound_conflict_is_ambiguous(opts):
    from mojo.mojosec.lineage import CompoundAssembler

    assembler = CompoundAssembler()
    result = assembler.ingest([
        _record(88, "SYSCALL", pid="42", ppid="10", exe="/usr/bin/sudo"),
        _record(88, "SYSCALL", pid="43", ppid="10", exe="/usr/bin/sudo"),
        _record(88, "EOE"),
    ])
    th.assert_true(result["complete"][0]["ambiguous"],
                   "conflicting rows in one Audit serial must fail ambiguous")


@th.unit_test("event lineage projection is bounded")
def test_project_ancestors_is_bounded(opts):
    from mojo.mojosec.lineage import project_ancestors

    ancestors = [{"pid": i, "exe": f"/bin/p{i}"} for i in range(20)]
    projected = project_ancestors(ancestors)
    th.assert_eq(len(projected), 8, "central Event evidence may carry at most eight ancestors")
