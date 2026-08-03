#!/usr/bin/env python3
"""Build the static Django-MOJO marketing and documentation site."""

import argparse
import difflib
import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "website" / "django_mojo"
DOCS = ROOT / "docs"
DEFAULT_OUTPUT = ROOT / "docs_site"
TRACKS = ("django_developer", "web_developer")
SHARD_TARGET = 145_000
FILE_LIMIT = 200_000
MARKED_URL = "https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js"
MARKED_SHA256 = "3e7e7d7feb3e5d58cb6c804f68ab5c24cc7e5eb6270fd6e5cbb9124739217d0c"
MARKED_LICENSE_URL = "https://cdn.jsdelivr.net/npm/marked@15.0.12/LICENSE.md"
MARKED_LICENSE_SHA256 = "8e3a3f82f59a60958f56ca08f445647c32a4733dc7ca6c2c46f6eb898471ab9c"
DOMPURIFY_URL = "https://cdn.jsdelivr.net/npm/dompurify@3.2.6/dist/purify.min.js"
DOMPURIFY_SHA256 = "89e1fa7647cb495370d3a997ace4387f5d15d9f4c5af12352c53daa400956287"
DOMPURIFY_LICENSE_URL = "https://cdn.jsdelivr.net/npm/dompurify@3.2.6/LICENSE"
DOMPURIFY_LICENSE_SHA256 = "1b02e03c3fb4f87d476c128f0eb9def1f5a1709d28b180465228bd41574623b7"
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
CODE_RE = re.compile(r"```.*?```|`[^`]+`", re.DOTALL)


def run_git(*args):
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def source_commit():
    try:
        commit = run_git("rev-parse", "origin/main")
    except (subprocess.CalledProcessError, FileNotFoundError):
        commit = run_git("rev-parse", "HEAD")
    dirty = subprocess.run(
        ["git", "diff", "--quiet", commit, "--", "docs"], cwd=ROOT
    )
    if dirty.returncode:
        raise RuntimeError(
            "docs differ from the pinned Git commit; commit or restore them before building"
        )
    untracked = run_git("ls-files", "--others", "--exclude-standard", "--", "docs")
    if untracked:
        raise RuntimeError(
            "untracked documentation files are not publishable; commit or remove them before building"
        )
    return commit


def heading_slug(value):
    value = html.unescape(re.sub(r"<[^>]+>", "", value)).lower().strip()
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    return re.sub(r"[-\s]+", "-", value).strip("-")


def plain_text(markdown):
    text = CODE_RE.sub(lambda match: match.group(0).strip("`"), markdown)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[#>*_|~]", " ", text)
    return " ".join(html.unescape(text).split())


def document_id(path):
    return path.relative_to(DOCS).with_suffix("").as_posix()


def read_documents(commit):
    documents = []
    tree = run_git(
        "ls-tree",
        "-r",
        commit,
        "--",
        *(f"docs/{track}" for track in TRACKS),
    )
    tracked = []
    for line in tree.splitlines():
        metadata, repo_path = line.split("\t", 1)
        mode, object_type, _object_id = metadata.split()
        if not repo_path.endswith(".md"):
            continue
        if object_type != "blob" or mode not in ("100644", "100755"):
            raise RuntimeError(
                f"documentation source must be a regular Git file: {repo_path}"
            )
        tracked.append(repo_path)
    for repo_path in sorted(tracked):
        rel = repo_path.removeprefix("docs/")
        path = DOCS / rel
        markdown = run_git("show", f"{commit}:{repo_path}")
        headings = []
        seen = {}
        for match in HEADING_RE.finditer(markdown):
            label = re.sub(r"[`*]", "", match.group(2)).strip()
            base = heading_slug(label) or "section"
            count = seen.get(base, 0)
            seen[base] = count + 1
            slug = base if count == 0 else f"{base}-{count}"
            headings.append(
                {"level": len(match.group(1)), "title": label, "slug": slug}
            )
        title = (
            headings[0]["title"]
            if headings
            else path.stem.replace("_", " ").title()
        )
        body = plain_text(markdown)
        parts = PurePosixPath(rel).parts
        track = parts[0]
        section = parts[1] if len(parts) > 2 else "overview"
        documents.append(
            {
                "id": document_id(path),
                "source": rel,
                "track": track,
                "section": section,
                "title": title,
                "headings": headings,
                "excerpt": body[:260],
                "search": body.lower(),
                "aliases": {},
                "path": path,
                "markdown": markdown,
            }
        )
    ids = [doc["id"] for doc in documents]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate document ids found")
    return documents


def validate_output_path(output):
    output = output.resolve()
    default = DEFAULT_OUTPUT.resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if output == temporary:
        raise RuntimeError("refusing to use the system temporary root as build output")
    if output != default and temporary not in output.parents:
        raise RuntimeError(
            f"--output must be {default} or a dedicated directory under {temporary}"
        )
    protected = (DOCS.resolve(), SOURCE.resolve())
    if output == ROOT.resolve() or any(
        output == path or path in output.parents for path in protected
    ):
        raise RuntimeError(f"refusing destructive build output path: {output}")
    return output


def normalize_repo_target(source, href):
    raw_target, _, fragment = href.partition("#")
    if not raw_target:
        return source, fragment, "fragment"
    target = PurePosixPath("docs").joinpath(PurePosixPath(source).parent, raw_target)
    collapsed = []
    for part in target.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if collapsed:
                collapsed.pop()
            else:
                collapsed.append("..")
        else:
            collapsed.append(part)
    normalized = PurePosixPath(*collapsed).as_posix()
    for track in TRACKS:
        marker = f"{track}/"
        if marker in normalized and normalized.endswith(".md"):
            return marker + normalized.split(marker, 1)[1], fragment, "docs"
    return normalized, fragment, "repo"


def match_fragment(target_doc, fragment):
    wanted = re.sub(r"[^a-z0-9]", "", fragment.lower())
    scored = []
    for heading in target_doc["headings"]:
        candidate = re.sub(r"[^a-z0-9]", "", heading["slug"].lower())
        if not wanted or not candidate:
            continue
        if wanted == candidate:
            score = 1.0
        elif wanted.startswith(candidate) or candidate.startswith(wanted):
            score = 0.9 if min(len(wanted), len(candidate)) >= 6 else 0.6
        else:
            score = difflib.SequenceMatcher(None, wanted, candidate).ratio()
        wanted_tokens = [token.rstrip("s") for token in re.split(r"[-_]", fragment.lower()) if token]
        candidate_tokens = [token.rstrip("s") for token in re.split(r"[-_]", heading["slug"].lower()) if token]
        if len(wanted_tokens) >= 2 and candidate_tokens[:2] == wanted_tokens[:2]:
            score = max(score, 0.82)
        scored.append((score, heading["slug"]))
    scored.sort(reverse=True)
    if scored and scored[0][0] >= 0.72 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None


def validate_links(documents):
    by_source = {doc["source"]: doc for doc in documents}
    errors = []
    local_links = 0
    for doc in documents:
        for match in LINK_RE.finditer(doc["markdown"]):
            href = match.group(1).strip().split()[0].strip("<>")
            if href.startswith(("http://", "https://", "mailto:", "tel:")):
                continue
            target, fragment, kind = normalize_repo_target(doc["source"], href)
            if kind == "fragment":
                target_doc = doc
            elif kind == "docs" and target.endswith(".md"):
                target_doc = by_source.get(target)
                local_links += 1
                if not target_doc:
                    errors.append(f"{doc['source']}: missing docs target {href}")
                    continue
            else:
                repo_path = ROOT / target
                if target and not repo_path.exists():
                    errors.append(f"{doc['source']}: missing repository target {href}")
                continue
            if fragment:
                wanted = fragment.lower()
                anchors = {heading["slug"].lower() for heading in target_doc["headings"]}
                if wanted not in anchors:
                    matched = match_fragment(target_doc, fragment)
                    if matched:
                        target_doc["aliases"][fragment] = matched
                    else:
                        errors.append(
                            f"{doc['source']}: missing fragment #{fragment} in {target_doc['source']}"
                        )
    if errors:
        sample = "\n".join(errors[:30])
        raise RuntimeError(f"{len(errors)} broken local link(s):\n{sample}")
    return local_links


def compact_catalog(doc):
    return {
        "id": doc["id"],
        "source": doc["source"],
        "track": doc["track"],
        "section": doc["section"],
        "title": doc["title"],
        "headings": doc["headings"],
        "excerpt": doc["excerpt"],
        "aliases": doc["aliases"],
    }


def shard_entries(entries, commit, prefix, output):
    names = []
    current = []
    for entry in entries:
        candidate = current + [entry]
        payload = json.dumps(
            {"commit": commit, "entries": candidate},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if current and len(payload) > SHARD_TARGET:
            names.append(write_shard(prefix, names, current, commit, output))
            current = [entry]
        else:
            current = candidate
    if current:
        names.append(write_shard(prefix, names, current, commit, output))
    return names


def write_shard(prefix, prior_names, entries, commit, output):
    name = f"data/{prefix}-{len(prior_names):03d}.json"
    payload = json.dumps(
        {"commit": commit, "entries": entries},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    path = output / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return name


def download_verified(url, expected_hash, destination):
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_hash:
        raise RuntimeError(f"checksum mismatch for {url}: {actual}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def copy_source(output):
    if not SOURCE.exists():
        raise RuntimeError(f"missing site source: {SOURCE}")
    for path in SOURCE.rglob("*"):
        if not path.is_file() or path.name == "README.md":
            continue
        relative = path.relative_to(SOURCE)
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def validate_output(output):
    required = ("index.html", "docs/index.html", "404.html", "css/site.css", "js/docs.js")
    missing = [name for name in required if not (output / name).is_file()]
    if missing:
        raise RuntimeError(f"missing required output files: {', '.join(missing)}")
    oversize = []
    total = 0
    count = 0
    for path in output.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        count += 1
        total += size
        if size > FILE_LIMIT:
            oversize.append(f"{path.relative_to(output)} ({size} bytes)")
    if oversize:
        raise RuntimeError("files exceed platform cap: " + ", ".join(oversize))
    if total > 8_000_000:
        raise RuntimeError(f"bundle exceeds 8 MB ({total} bytes)")
    return count, total


def build_into(output, final_output):
    commit = source_commit()
    documents = read_documents(commit)
    local_links = validate_links(documents)
    output.mkdir(parents=True, exist_ok=True)
    copy_source(output)
    catalog = [compact_catalog(doc) for doc in documents]
    search = [{"id": doc["id"], "text": doc["search"]} for doc in documents]
    catalog_files = shard_entries(catalog, commit, "catalog", output)
    search_files = shard_entries(search, commit, "search", output)
    build_info = {
        "commit": commit,
        "repository": "NativeMojo/django-mojo",
        "documents": len(documents),
        "tracks": {
            track: len([doc for doc in documents if doc["track"] == track])
            for track in TRACKS
        },
        "catalogs": catalog_files,
        "search": search_files,
    }
    (output / "data" / "build.json").write_text(
        json.dumps(build_info, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    download_verified(MARKED_URL, MARKED_SHA256, output / "vendor" / "marked.min.js")
    download_verified(
        MARKED_LICENSE_URL,
        MARKED_LICENSE_SHA256,
        output / "vendor" / "marked.LICENSE.md",
    )
    download_verified(
        DOMPURIFY_URL,
        DOMPURIFY_SHA256,
        output / "vendor" / "purify.min.js",
    )
    download_verified(
        DOMPURIFY_LICENSE_URL,
        DOMPURIFY_LICENSE_SHA256,
        output / "vendor" / "DOMPURIFY.LICENSE",
    )
    count, total = validate_output(output)
    print(
        json.dumps(
            {
                "output": str(final_output),
                "commit": commit,
                "documents": len(documents),
                "django_developer": build_info["tracks"]["django_developer"],
                "web_developer": build_info["tracks"]["web_developer"],
                "local_links": local_links,
                "catalog_shards": len(catalog_files),
                "search_shards": len(search_files),
                "files": count,
                "bytes": total,
            },
            indent=2,
        )
    )


def build(output):
    output = validate_output_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        build_into(staging, output)
        if output.exists():
            shutil.rmtree(output)
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        with tempfile.TemporaryDirectory(prefix="django-mojo-site-") as folder:
            build(Path(folder))
    else:
        build(args.output.resolve())


if __name__ == "__main__":
    main()
