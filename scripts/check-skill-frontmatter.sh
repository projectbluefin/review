#!/usr/bin/env bash
# Validate docs/skills/*.md front-matter and generate docs/skills/index.json
# from it. Front-matter is the source of truth: this script builds the
# manifest and fails if the committed
# index.json differs from the generated content. `--write` rewrites the
# manifest instead of comparing.
#
# Modelled on projectbluefin/common's scripts/check-skill-frontmatter.sh, with
# one difference: every skill in this repo is held to the size limits below
# with no standing exceptions.
#
# Deliberately python3-only: `yq` is not guaranteed on contributor machines or
# in the container image, and the front-matter subset used here (scalars,
# inline/block lists, one nested mapping) is small enough to parse directly.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python3 - "$@" <<'PY'
import json
import os
import sys

SKILL_DIR = "docs/skills"
INDEX = os.path.join(SKILL_DIR, "index.json")
ROUTER = "docs/SKILL.md"

MAX_DESC = 256
MAX_SOFT = 200
MAX_HARD = 500

REQUIRED_KEYS = [
    "name",
    "version",
    "last_updated",
    "id",
    "one_line_purpose",
    "entry_point",
    "category",
    "status",
    "tags",
    "description",
]
VALID_STATUS = {"active", "deprecated", "reserved"}
VALID_CATEGORY = {"ci-ops", "test-authoring", "meta"}
SCHEMA_VERSION = "1.0"
# Manifest fields copied verbatim from front-matter, in output order.
# doc_type is appended separately from metadata.type.
MANIFEST_FIELDS = [
    "id",
    "name",
    "one_line_purpose",
    "entry_point",
    "category",
    "status",
    "tags",
    "description",
    "version",
    "last_updated",
]

errors = []
warnings = []


def error(path, message):
    errors.append((path, message))
    print("::error file=%s::%s" % (path, message))


def warn(path, message):
    warnings.append((path, message))
    print("::warning file=%s::%s" % (path, message))


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_front_matter(text):
    """Parse the leading '---' delimited block into a dict.

    Supports the subset the org skill docs use: scalar values, inline
    (``[a, b]``) and block (``- a``) lists, and one level of nested mapping
    (``metadata:`` / ``  type:``).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None

    body = lines[1:end]
    data = {}
    i = 0
    while i < len(body):
        raw = body[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith((" ", "\t")) or ":" not in raw:
            raise ValueError("unparseable front-matter line: %r" % raw)
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            data[key] = [unquote(item) for item in inner.split(",")] if inner else []
            continue
        if value:
            data[key] = unquote(value)
            continue
        # Empty scalar: the value is the following indented block.
        items = []
        mapping = {}
        while i < len(body) and (not body[i].strip() or body[i].startswith((" ", "\t"))):
            nested = body[i].strip()
            i += 1
            if not nested:
                continue
            if nested.startswith("- "):
                items.append(unquote(nested[2:]))
            elif ":" in nested:
                sub, _, sub_value = nested.partition(":")
                mapping[sub.strip()] = unquote(sub_value)
            else:
                raise ValueError("unparseable front-matter line: %r" % nested)
        data[key] = items if items else mapping
    return data


def check_file(path):
    stem = os.path.basename(path)[: -len(".md")]
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    try:
        fm = parse_front_matter(text)
    except ValueError as exc:
        error(path, str(exc))
        return None
    if fm is None:
        error(path, "missing or unterminated YAML front-matter")
        return None

    for key in REQUIRED_KEYS:
        if key not in fm or fm[key] in ("", None, [], {}):
            error(path, "missing required key '%s'" % key)
    metadata = fm.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("type"):
        error(path, "missing required key 'metadata.type'")

    description = fm.get("description")
    if isinstance(description, str) and len(description) > MAX_DESC:
        error(path, "description is %d chars (max %d)" % (len(description), MAX_DESC))

    tags = fm.get("tags")
    if isinstance(tags, list):
        if not 3 <= len(tags) <= 6:
            error(path, "tags has %d entries (expected 3-6)" % len(tags))
        for tag in tags:
            if tag != tag.lower():
                error(path, "tag '%s' is not lowercase" % tag)
    elif "tags" in fm:
        error(path, "tags must be a list")

    for key in ("id", "name"):
        if fm.get(key) not in (None, stem):
            error(path, "%s '%s' does not match filename stem '%s'" % (key, fm[key], stem))

    expected_entry = "%s/%s.md" % (SKILL_DIR, stem)
    if fm.get("entry_point") not in (None, expected_entry):
        error(path, "entry_point '%s' should be '%s'" % (fm["entry_point"], expected_entry))

    status = fm.get("status")
    if status is not None and status not in VALID_STATUS:
        error(path, "status '%s' not one of %s" % (status, "/".join(sorted(VALID_STATUS))))

    category = fm.get("category")
    if category is not None and category not in VALID_CATEGORY:
        error(path, "category '%s' not one of %s" % (category, "/".join(sorted(VALID_CATEGORY))))

    line_count = len(text.splitlines())
    if line_count > MAX_HARD:
        error(path, "is %d lines (hard max %d)" % (line_count, MAX_HARD))
    elif line_count > MAX_SOFT:
        warn(path, "is %d lines (soft max %d)" % (line_count, MAX_SOFT))

    return fm


def build_manifest(front_matter):
    """Build the index.json content from parsed front-matter.

    Entries are sorted by id so output is deterministic. generated_at tracks
    the newest last_updated so regeneration is byte-stable until a skill
    actually changes.
    """
    skills = []
    for stem in sorted(front_matter):
        fm = front_matter[stem]
        entry = {field: fm.get(field) for field in MANIFEST_FIELDS}
        metadata = fm.get("metadata")
        entry["doc_type"] = metadata.get("type") if isinstance(metadata, dict) else None
        skills.append(entry)
    generated_at = max(
        (fm["last_updated"] for fm in front_matter.values() if fm.get("last_updated")),
        default="",
    )
    return {
        "generated_at": generated_at,
        "schema_version": SCHEMA_VERSION,
        "skills": skills,
    }


def render_manifest(manifest):
    return json.dumps(manifest, indent=2) + "\n"


def check_index(front_matter, write):
    manifest = build_manifest(front_matter)
    rendered = render_manifest(manifest)

    if write:
        if errors:
            error(INDEX, "not writing the manifest while front-matter has errors")
            return
        with open(INDEX, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        print("wrote %s (%d skills)" % (INDEX, len(manifest["skills"])))
        return

    if not os.path.exists(INDEX):
        error(INDEX, "manifest is missing; run scripts/check-skill-frontmatter.sh --write")
        return
    with open(INDEX, "r", encoding="utf-8") as handle:
        committed = handle.read()
    if committed != rendered:
        error(
            INDEX,
            "manifest is stale; regenerate with scripts/check-skill-frontmatter.sh --write",
        )
        import difflib
        sys.stdout.writelines(
            difflib.unified_diff(
                committed.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=INDEX,
                tofile="generated",
            )
        )


def main():
    args = sys.argv[1:]
    write = "--write" in args
    unknown = [arg for arg in args if arg != "--write"]
    if unknown:
        print("::error::unknown argument(s): %s" % " ".join(unknown))
        return 1

    if not os.path.isdir(SKILL_DIR):
        print("::error file=%s::skill directory is missing" % SKILL_DIR)
        return 1

    if not os.path.exists(ROUTER):
        error(ROUTER, "skill router is missing")

    paths = sorted(
        os.path.join(SKILL_DIR, name)
        for name in os.listdir(SKILL_DIR)
        if name.endswith(".md")
    )
    if not paths:
        print("::error file=%s::no skill documents found" % SKILL_DIR)
        return 1

    front_matter = {}
    for path in paths:
        fm = check_file(path)
        if fm is not None:
            front_matter[os.path.basename(path)[: -len(".md")]] = fm

    check_index(front_matter, write)

    print(
        "checked %d skill document(s) and %s: %d error(s), %d warning(s)"
        % (len(paths), INDEX, len(errors), len(warnings))
    )
    for path, message in errors:
        print("  FAIL %s: %s" % (path, message))
    for path, message in warnings:
        print("  WARN %s: %s" % (path, message))
    return 1 if errors else 0


sys.exit(main())
PY
