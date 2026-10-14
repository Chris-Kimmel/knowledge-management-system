"""On-disk format for knowledge shards.

This module owns the shard file format and nothing else. Everything here is
deterministic: no LLM call, no judgment. See founding_design.md §4 and §5.

The five fields of §4 are split by who is competent to produce them:

  computed here   1 id/timestamp, 5 metadata (address, staleness, layer, ...)
  produced by LLM 2 title, 3 summary, 4 contextualization

That split is deliberate. Address and staleness are facts about a file; asking
a model for them invites hallucination where `git log` gives a right answer.
"""

import hashlib
import json
import re
import subprocess
from datetime import date, datetime
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SHARDS = REPO / "shards"
INDEX = REPO / "index" / "manifest.json"

# Frontmatter key order. Fixed so that git diffs show real changes rather than
# key reshuffling.
KEY_ORDER = [
    "id", "title", "layer", "address", "address_type", "staleness_marker",
    "last_verified", "confidentiality", "facets", "children", "links",
    "supersedes",
]

# Confidentiality tiers (§9). §11 left these undefined; these four close it.
# Part of the format rather than of capture, because more than one stage needs
# them: capture proposes a tier, and synthesize.py inherits one. **The order is
# load-bearing — most restricted first** — so that a synthesized shard can take
# the most restrictive tier among its children by position. Reordering this
# dict would silently let a synthesis of confidential material inherit a public
# tier, so it is ordered, not alphabetised.
CONFIDENTIALITY = {
    "strictly_confidential": "content must never leave its source system; the summary stays abstract",
    "confidential": "internal to the author and named collaborators",
    "public": "shareable outside, but not currently published",
    "fully_public": "already published",
}


def most_restrictive(tiers: list[str]) -> str:
    """The most restrictive tier in `tiers`, by CONFIDENTIALITY order.

    A shard synthesized from others must never be less guarded than the most
    guarded thing it stands for — otherwise synthesis would quietly launder
    confidential material into a public summary. Computed, never proposed by a
    model, for the same reason address and staleness are computed: this is a
    fact about the inputs, not a judgment call.
    """
    order = list(CONFIDENTIALITY)
    known = [t for t in tiers if t in order]
    if not known:
        return "confidential"  # unknown provenance: guard it, don't guess
    return min(known, key=order.index)


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

def new_id(layer: int, when: datetime | None = None) -> str:
    """Allocate an unused shard id.

    Second precision, not the minute precision of the §4 example: a batch run
    captures several files inside one minute, and two shards sharing an id
    would make every link pointing at that id ambiguous. On collision a
    numeric suffix keeps the allocation total.
    """
    when = when or datetime.now()
    base = when.strftime("%Y%m%d-%H%M%S")
    candidate, n = base, 1
    while shard_path(layer, candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def shard_path(layer: int, shard_id: str) -> Path:
    """Shards are named by id alone, never by title.

    Ids are immutable and titles are not, so a title-derived filename would
    break every link the first time a shard is retitled. Human legibility is
    recovered from the `title` field, which Obsidian reads as a display name.
    """
    return SHARDS / f"L{layer}" / f"{shard_id}.md"


# --------------------------------------------------------------------------
# address and staleness (§7)
# --------------------------------------------------------------------------

def describe_artifact(path: Path) -> dict:
    """Resolve an artifact's address and its staleness marker.

    Per the §7 table, the marker's form follows the address type, and the
    cheap check is preferred: for a tracked file the marker is the hash of the
    last commit that touched it, which is compared as a string without ever
    opening the artifact. Known limitation: that marker cannot see uncommitted
    working-tree edits. Untracked files therefore fall back to content hashing
    rather than pointing at a commit that does not describe them.
    """
    path = path.resolve()
    commit = _last_commit_touching(path)
    if commit:
        return {
            "address": str(path),
            "address_type": "local_git",
            "staleness_marker": commit,
        }
    return {
        "address": str(path),
        "address_type": "local_file",
        "staleness_marker": "sha256:" + _sha256(path),
    }


def _last_commit_touching(path: Path) -> str | None:
    try:
        inside = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        out = subprocess.run(
            ["git", "-C", str(path.parent), "log", "-1", "--format=%H", "--", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        marker = out.stdout.strip()
        # Empty output means the file is inside a repository but not committed,
        # so there is no commit that describes its current content.
        return marker or None
    except (OSError, subprocess.SubprocessError):
        return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# read / write
# --------------------------------------------------------------------------

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


def read(path: Path) -> dict:
    """Parse a shard file into {**frontmatter, summary, context}."""
    m = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if not m:
        raise ValueError(f"{path} has no YAML frontmatter")
    data = yaml.safe_load(m.group(1)) or {}
    body = m.group(2)
    data["summary"] = _section(body, "Summary")
    data["context"] = _section(body, "Context")
    data["path"] = str(path.relative_to(REPO))
    return data


def _section(body: str, heading: str) -> str:
    m = re.search(rf"^## {heading}\n(.*?)(?=^## |\Z)", body, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


def write(meta: dict, summary: str, context: str) -> Path:
    path = shard_path(meta["layer"], meta["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\n{_dump_frontmatter(meta)}---\n\n"
        f"## Summary\n\n{summary.strip()}\n\n"
        f"## Context\n\n{context.strip()}\n",
        encoding="utf-8",
    )
    return path


def _dump_frontmatter(meta: dict) -> str:
    """Emit YAML by hand, in KEY_ORDER.

    yaml.safe_dump sorts keys and quotes unpredictably, which makes every diff
    noisy. Scalars are written with json.dumps because every JSON string is a
    valid YAML double-quoted scalar, so titles and paths containing colons,
    spaces or quotes survive without a custom escaper.
    """
    lines = []
    for key in KEY_ORDER:
        if key not in meta:
            continue
        value = meta[key]
        if key == "links":
            if not value:
                lines.append("links: []")
                continue
            lines.append("links:")
            for link in value:
                lines.append(f"  - id: {json.dumps(link['id'])}")
                lines.append(f"    why: {json.dumps(link['why'])}")
        elif isinstance(value, list):
            lines.append(f"{key}: [{', '.join(json.dumps(v) for v in value)}]")
        elif isinstance(value, int) and not isinstance(value, bool):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {json.dumps(value)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# derived index (§4)
# --------------------------------------------------------------------------

def all_shards() -> list[dict]:
    return [read(p) for p in sorted(SHARDS.glob("L*/*.md"))]


def build_index() -> dict:
    """Regenerate the manifest aggregate operations read instead of the corpus.

    A build artifact, never a source of truth: it is rewritten wholesale from
    the shard files and is not tracked in git.
    """
    shards = all_shards()

    # Links are stored once, on the shard that declares them (§4 keeps the
    # reverse direction derived so there is never a second place to update).
    # Backlinks are therefore computed here rather than written to disk.
    backlinks: dict[str, list[str]] = {}
    for s in shards:
        for link in s.get("links") or []:
            backlinks.setdefault(link["id"], []).append(s["id"])

    # Same one-direction rule as links and children: a regenerated shard records
    # what it `supersedes`, the older shard is never touched, and the reverse is
    # derived here. §8 requires that regeneration never overwrite, so drift
    # shows up as history instead of a silent rewrite.
    superseded_by: dict[str, str] = {}
    for s in shards:
        if s.get("supersedes"):
            superseded_by[s["supersedes"]] = s["id"]

    entries = []
    for s in shards:
        entries.append({
            "id": s["id"],
            "title": s["title"],
            "layer": s["layer"],
            "path": s["path"],
            "address": s.get("address"),
            "address_type": s.get("address_type"),
            "staleness_marker": s.get("staleness_marker"),
            "last_verified": s.get("last_verified"),
            "confidentiality": s.get("confidentiality"),
            "facets": s.get("facets") or [],
            "children": s.get("children") or [],
            "links": s.get("links") or [],
            "linked_by": sorted(backlinks.get(s["id"], [])),
            "supersedes": s.get("supersedes"),
            "superseded_by": superseded_by.get(s["id"]),
            "summary": s.get("summary", ""),
        })

    manifest = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "shard_count": len(entries),
        "current_count": sum(1 for e in entries if not e["superseded_by"]),
        "shards": entries,
    }
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def today() -> str:
    return date.today().isoformat()
