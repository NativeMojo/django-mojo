"""mojo/deploy/vhost_install.py — what aborts a deploy, and what only warns.

`install_vhost` runs on every managed node of every consumer on every deploy,
and only where the vhost is ALREADY installed — so it breaks on the second
deploy, not the first. 1.16.0 shipped a parser whose refusals aborted the
release, choked on its own em-dash comments, and needed a hotfix 46 minutes
later. The contract this file pins down is the fix for the class:

    Certificate preservation is an ENHANCEMENT. Failing to preserve produces a
    warning and the repository's bytes. Only the write path — and a repository
    certificate that does not exist on this node — may abort the deploy.

`nginx_etc` here is never `/etc/nginx`, so the module resolves `owner` to this
process's euid and every path below is exercisable unprivileged.

`changed-snapshot`, `changed-stage` and `short-stage-write` are deliberately
NOT here: they need a racing writer, which is a flake source in a
thread-parallel suite. They live in
tests/test_deploy/harness/test_post_deploy_sh.sh instead.
"""

import io
import os
import shutil
import stat
import tempfile

from testit import helpers as th


LINEAGE = "app.example.test"

REPO_VHOST = """\
# HTTPS — repository-owned comments may use UTF-8.
server {
    listen 443 ssl;
    server_name app.example.test;
    ssl_certificate %(cert)s;
    ssl_certificate_key %(key)s;
    add_header X-Repository yes;
}
"""

INSTALLED_VHOST = """\
server {
    listen 443 ssl;
%(name)s%(cert)s
%(key)s
    location /old-operator-route { return 418; }
}
%(extra)s"""


def _tree():
    """A throwaway node: nginx_etc/conf.d, a sibling letsencrypt lineage at
    revision 7, and the repository's snakeoil placeholder."""
    from objict import objict

    root = tempfile.mkdtemp(prefix="vhost-install-")
    node = objict(
        root=root,
        nginx_etc=os.path.join(root, "nginx"),
        le_root=os.path.join(root, "letsencrypt"),
        snakeoil=os.path.join(root, "snakeoil"),
    )
    node.conf_d = os.path.join(node.nginx_etc, "conf.d")
    node.live = os.path.join(node.le_root, "live", LINEAGE)
    node.archive = os.path.join(node.le_root, "archive", LINEAGE)
    node.source = os.path.join(root, "repo", "app.conf")
    node.destination = os.path.join(node.conf_d, "app.conf")
    node.repo_cert = os.path.join(node.snakeoil, "ssl-cert-snakeoil.pem")
    node.repo_key = os.path.join(node.snakeoil, "ssl-cert-snakeoil.key")

    for path in (node.conf_d, node.live, node.archive, node.snakeoil,
                 os.path.dirname(node.source)):
        os.makedirs(path)
    for path in (node.nginx_etc, node.conf_d, node.le_root, node.live,
                 node.archive, os.path.dirname(node.live),
                 os.path.dirname(node.archive)):
        os.chmod(path, 0o755)

    _write(os.path.join(node.archive, "fullchain7.pem"), "certificate\n", 0o644)
    _write(os.path.join(node.archive, "privkey7.pem"), "private-key\n", 0o600)
    os.symlink("../../archive/%s/fullchain7.pem" % LINEAGE,
               os.path.join(node.live, "fullchain.pem"))
    os.symlink("../../archive/%s/privkey7.pem" % LINEAGE,
               os.path.join(node.live, "privkey.pem"))
    _write(node.repo_cert, "placeholder-certificate\n", 0o644)
    _write(node.repo_key, "placeholder-key\n", 0o600)
    write_repository(node)
    return node


def _write(path, text, mode=0o644):
    with open(path, "w") as handle:
        handle.write(text)
    os.chmod(path, mode)


def write_repository(node, cert=None, key=None, raw=None):
    if raw is not None:
        with open(node.source, "wb") as handle:
            handle.write(raw)
        return
    _write(node.source, REPO_VHOST % {
        "cert": node.repo_cert if cert is None else cert,
        "key": node.repo_key if key is None else key,
    })


def write_installed(node, name="app.example.test", cert=None, key=None,
                    certs=None, extra="", mode=0o644):
    """`name=None` omits the server_name directive entirely; `certs` replaces
    the single ssl_certificate line with an arbitrary block."""
    live_cert = os.path.join(node.live, "fullchain.pem")
    live_key = os.path.join(node.live, "privkey.pem")
    if certs is None:
        certs = "    ssl_certificate %s;" % (live_cert if cert is None else cert)
    _write(node.destination, INSTALLED_VHOST % {
        "name": "" if name is None else "    server_name %s;\n" % name,
        "cert": certs,
        "key": "    ssl_certificate_key %s;" % (live_key if key is None else key),
        "extra": extra,
    }, mode)


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


def run(node, argv=None):
    """Call main() with our own buffers.

    NEVER redirect_stdout here: the runner executes modules as threads in one
    process, so assigning sys.stdout is a process-wide mutation. The out=/err=
    seams exist for exactly this.
    """
    from mojo.deploy import vhost_install

    out, err = io.StringIO(), io.StringIO()
    if argv is None:
        argv = [node.source, node.destination, node.nginx_etc]
    code = vhost_install.main(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def assert_discloses_nothing(node, text, label):
    th.assert_eq(node.le_root in text, False,
                 "%s must not put a certificate path in a deploy log: %r"
                 % (label, text))
    th.assert_eq(LINEAGE in text, False,
                 "%s must not name a certbot lineage: %r" % (label, text))


def assert_downgraded(node, code, severity, label):
    status, out, err = run(node)

    th.assert_eq(status, 0,
                 "%s must not abort the deploy (stderr: %r)" % (label, err))
    th.assert_eq(read(node.destination), read(node.source),
                 "%s must install the repository bytes verbatim" % label)
    th.assert_in("MOJO-VHOST-WARN %s %s" % (severity, code), out,
                 "%s must emit the %s-severity sentinel for %s, got %r"
                 % (label, severity, code, out))
    th.assert_in("vhost app.conf: %s" % code, err,
                 "%s must name its code on stderr, got %r" % (label, err))
    assert_discloses_nothing(node, out + err, label)


def assert_refused(node, code, label, argv=None):
    before = read(node.destination) if os.path.isfile(node.destination) else None
    status, out, err = run(node, argv)

    th.assert_eq(status, 1, "%s must abort the deploy (stdout: %r)" % (label, out))
    if before is not None:
        th.assert_eq(read(node.destination), before,
                     "%s must leave the installed vhost byte-unchanged" % label)
    th.assert_in("vhost app.conf: %s" % code, err,
                 "%s must name its code on stderr, got %r" % (label, err))
    th.assert_in("TLS vhost convergence refused unsafe or changed state", err,
                 "%s must keep the bounded refusal sentence, got %r"
                 % (label, err))
    assert_discloses_nothing(node, out + err, label)


@th.django_unit_test("a proven lineage survives repository convergence")
def test_preservation_happy_path(opts):
    node = _tree()
    try:
        write_installed(node)
        status, out, err = run(node)
        installed = read(node.destination).decode("utf-8")

        th.assert_eq(status, 0, "a proven lineage must deploy: %r" % err)
        th.assert_eq(out, "",
                     "a preserved lineage must emit no warning sentinel: %r" % out)
        th.assert_in(os.path.join(node.live, "fullchain.pem"), installed,
                     "the proven fullchain lineage must survive convergence")
        th.assert_in(os.path.join(node.live, "privkey.pem"), installed,
                     "the proven private-key lineage must survive convergence")
        th.assert_in("X-Repository yes", installed,
                     "the repository stays authoritative for every other "
                     "directive")
        th.assert_eq("old-operator-route" in installed, False,
                     "an installed-node route must not be carried forward")
        th.assert_eq("ssl-cert-snakeoil" in installed, False,
                     "the repository placeholder must have been replaced by "
                     "the node's issued lineage")

        first = read(node.destination)
        th.assert_eq(run(node)[0], 0, "a repeated deploy must succeed")
        th.assert_eq(read(node.destination), first,
                     "the overlay must be byte-idempotent — a deploy that "
                     "rewrites the file every run churns /etc forever")
    finally:
        shutil.rmtree(node.root, ignore_errors=True)


@th.django_unit_test("a repository file this parser cannot read warns, and installs")
def test_unreadable_repository_downgrades(opts):
    """The 1.16.0 outage class. nginx accepts every one of these files; only
    this parser does not, so its opinion may not veto a release."""
    cases = (
        ("invalid-utf8-config",
         (REPO_VHOST % {"cert": "/tmp/c", "key": "/tmp/k"}).encode() + b"\xff"),
        ("invalid-escape", b"server {\n    listen 443 ssl;\n}\ntrailing\\"),
        ("unterminated-quote",
         b"server {\n    server_name \"app.example.test;\n}\n"),
        ("unbalanced-server", b"server {\n    listen 443 ssl;\n"),
        ("incomplete-directive", b"server {\n    listen 443 ssl\n}\n"),
    )
    for code, raw in cases:
        node = _tree()
        try:
            write_installed(node)
            write_repository(node, raw=raw)
            assert_downgraded(node, code, "cfg", "a %s repository file" % code)
        finally:
            shutil.rmtree(node.root, ignore_errors=True)


@th.django_unit_test("an installed vhost that cannot be matched warns, and installs")
def test_unmatchable_installed_vhost_downgrades(opts):
    second = ("server { listen 443 ssl; server_name second.example.test; "
              "ssl_certificate /tmp/a; ssl_certificate_key /tmp/b; }\n")
    cases = (
        # code, severity, kwargs for write_installed
        ("ambiguous-tls-server", "cfg", {"extra": second}),
        # nginx's own canonical catch-all, and a TLS default_server block.
        ("unsafe-server-name", "cfg", {"name": "_"}),
        ("missing-server-name", "cfg", {"name": None}),
        ("server-name-mismatch", "tls", {"name": "renamed.example.test"}),
        # Pure mode drift, on a vhost that need not involve TLS at all.
        ("unsafe-installed-metadata", "cfg", {"mode": 0o664}),
    )
    for code, severity, kwargs in cases:
        node = _tree()
        try:
            write_installed(node, **kwargs)
            assert_downgraded(node, code, severity, "an installed %s" % code)
        finally:
            shutil.rmtree(node.root, ignore_errors=True)

    # A repository that drops its TLS server entirely.
    node = _tree()
    try:
        write_installed(node)
        write_repository(node, raw=b"server {\n    listen 80;\n"
                                   b"    server_name app.example.test;\n}\n")
        assert_downgraded(node, "repository-dropped-tls-server", "tls",
                          "a repository that dropped its TLS server")
    finally:
        shutil.rmtree(node.root, ignore_errors=True)


@th.django_unit_test("a lineage that cannot be proven warns, and installs")
def test_unprovable_lineage_downgrades(opts):
    """Everything reached while validating a Certbot pair. Skipping
    preservation fully neutralizes what each of these was protecting against,
    so none of them may cost a release."""
    def live(node, leaf):
        return os.path.join(node.live, leaf)

    def two_certificates(node):
        one = "    ssl_certificate %s;" % live(node, "fullchain.pem")
        write_installed(node, certs=one + "\n" + one)

    def relative(node):
        write_installed(node, cert="letsencrypt/live/x/fullchain.pem",
                        key="letsencrypt/live/x/privkey.pem")

    def noncanonical(node):
        write_installed(node, cert="/etc/ssl/../ssl/certs/x.pem",
                        key="/etc/ssl/../ssl/private/x.key")

    def mixed(node):
        write_installed(node, key="/etc/ssl/private/x.key")

    def bad_lineage(node):
        parent = os.path.join(node.le_root, "live", "bad$name")
        write_installed(node, cert=os.path.join(parent, "fullchain.pem"),
                        key=os.path.join(parent, "privkey.pem"))

    def wrong_filename(node):
        write_installed(node, cert=live(node, "cert.pem"))

    def not_a_link(node):
        write_installed(node)
        os.unlink(live(node, "fullchain.pem"))
        _write(live(node, "fullchain.pem"), "certificate\n")

    def split_revision(node):
        _write(os.path.join(node.archive, "privkey8.pem"), "private-key\n", 0o600)
        os.unlink(live(node, "privkey.pem"))
        os.symlink("../../archive/%s/privkey8.pem" % LINEAGE,
                   live(node, "privkey.pem"))
        write_installed(node)

    def group_readable_key(node):
        write_installed(node)
        os.chmod(os.path.join(node.archive, "privkey7.pem"), 0o640)

    def absent_archive(node):
        write_installed(node)
        shutil.rmtree(node.archive)

    def archive_is_a_file(node):
        write_installed(node)
        shutil.rmtree(node.archive)
        _write(node.archive, "not a directory\n")

    def group_writable_lineage(node):
        write_installed(node)
        os.chmod(os.path.dirname(node.live), 0o775)

    def half_deleted_lineage(node):
        # `validated_pair`'s one unguarded probe: the live directory survives a
        # partial `certbot delete` while the link inside it does not. Before
        # this item that FileNotFoundError escaped as a CODELESS fatal.
        write_installed(node)
        os.unlink(live(node, "fullchain.pem"))

    cases = (
        ("ambiguous-certificate-directive", "tls", two_certificates),
        ("relative-certificate-path", "cfg", relative),
        ("noncanonical-certificate-path", "cfg", noncanonical),
        ("mixed-or-noncanonical-live-path", "tls", mixed),
        ("unsafe-lineage", "tls", bad_lineage),
        ("noncanonical-live-path", "tls", wrong_filename),
        ("nonstandard-live-link", "tls", not_a_link),
        ("mixed-lineage-revision", "tls", split_revision),
        ("unsafe-private-key-mode", "tls", group_readable_key),
        ("missing-lineage-material", "tls", absent_archive),
        ("unsafe-lineage-material", "tls", archive_is_a_file),
        ("unsafe-lineage-metadata", "tls", group_writable_lineage),
        ("unpreservable-io", "cfg", half_deleted_lineage),
    )
    for code, severity, arrange in cases:
        node = _tree()
        try:
            arrange(node)
            assert_downgraded(node, code, severity, "a %s lineage" % code)
        finally:
            shutil.rmtree(node.root, ignore_errors=True)


@th.django_unit_test("the two warning severities split by what the operator lost")
def test_severity_split_matches_the_documented_set(opts):
    from mojo.deploy import vhost_install

    th.assert_eq("server-name-mismatch" in vhost_install.LINEAGE_AT_RISK, True,
                 "dropping a matched name drops the node's issued certificate "
                 "reference — that is the tls severity")
    th.assert_eq("invalid-utf8-config" in vhost_install.LINEAGE_AT_RISK, False,
                 "a file this parser could not read never got as far as a "
                 "certificate — that is the cfg severity")
    th.assert_eq("unpreservable-io" in vhost_install.LINEAGE_AT_RISK, False,
                 "an unreadable lineage probe is a cfg-severity downgrade")


@th.django_unit_test("a downgrade never installs a certificate path that is absent")
def test_repository_certificate_must_exist(opts):
    """The headline scenario: rename server_name AND the lineage path to a name
    certbot has not issued. Installing those bytes produces a conf nginx
    rejects, and the deploy then dies ~300 lines later with the broken file
    already on disk. Refuse early instead, with /etc untouched."""
    node = _tree()
    try:
        write_installed(node, name="renamed.example.test")
        os.unlink(node.repo_cert)
        assert_refused(node, "repository-certificate-missing",
                       "a repository certificate that does not exist")
    finally:
        shutil.rmtree(node.root, ignore_errors=True)

    # ...but an unparseable repository file still installs verbatim: nginx is
    # the better judge of a file this parser could not read.
    node = _tree()
    try:
        write_installed(node, name="renamed.example.test")
        os.unlink(node.repo_cert)
        write_repository(node, raw=b"server {\n    listen 443 ssl;\n")
        assert_downgraded(node, "unbalanced-server", "cfg",
                          "an unparseable repository with an absent placeholder")
    finally:
        shutil.rmtree(node.root, ignore_errors=True)


@th.django_unit_test("the write path stays fatal")
def test_write_path_refusals(opts):
    node = _tree()
    try:
        write_installed(node)
        os.unlink(node.source)
        assert_refused(node, "missing-file", "a repository source that vanished")
    finally:
        shutil.rmtree(node.root, ignore_errors=True)

    node = _tree()
    try:
        target = os.path.join(node.root, "symlink-target")
        _write(target, "do-not-overwrite\n")
        os.symlink(target, node.destination)
        status, out, err = run(node)

        th.assert_eq(status, 1, "a symlinked destination must abort the deploy")
        th.assert_in("vhost app.conf: unsafe-file", err,
                     "a symlinked destination must name unsafe-file: %r" % err)
        th.assert_eq(read(target), b"do-not-overwrite\n",
                     "a symlink target must never be followed or overwritten")
        th.assert_eq(os.path.islink(node.destination), True,
                     "the refused destination must remain a symlink")
    finally:
        shutil.rmtree(node.root, ignore_errors=True)

    node = _tree()
    try:
        os.makedirs(node.destination)
        status, out, err = run(node)

        th.assert_eq(status, 1, "a directory destination must abort the deploy")
        th.assert_in("vhost app.conf: non-regular-file", err,
                     "a directory destination must name non-regular-file: %r" % err)
        th.assert_eq(os.path.isdir(node.destination), True,
                     "the refused destination must remain a directory")
    finally:
        shutil.rmtree(node.root, ignore_errors=True)

    node = _tree()
    try:
        write_installed(node)
        os.chmod(node.conf_d, 0o777)
        assert_refused(node, "unsafe-destination-directory",
                       "a group-writable conf.d")
        th.assert_eq(stat.S_IMODE(os.stat(node.conf_d).st_mode), 0o777,
                     "the destination directory is not repaired by refusing it")
    finally:
        os.chmod(node.conf_d, 0o755)
        shutil.rmtree(node.root, ignore_errors=True)

    node = _tree()
    try:
        write_installed(node)
        assert_refused(node, "invalid-invocation", "a two-argument invocation",
                       argv=[node.source, node.destination])
    finally:
        shutil.rmtree(node.root, ignore_errors=True)
