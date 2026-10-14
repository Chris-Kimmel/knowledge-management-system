#!/usr/bin/env python3
"""Capture a real artifact into an L0 knowledge shard.

    python3 pipeline/capture.py <file.md> [--facet work] [--yes]

Scope (founding_design.md §6): local files, one LLM call, no MCP sources and no
layer generation. Formats are whatever `extract.py` can turn into text — §6 held
that expansion back until the schema had survived real files, which it has.

The flow is retrieve → propose → confirm → write:

  1. retrieval.py ranks existing shards deterministically and keeps the top few.
     Nothing is sent anywhere, and the cost of this step does not grow with the
     Hub the way handing the model every shard did.
  2. One LLM call proposes the fields that need judgment — title, summary,
     context, links, confidentiality, facets — seeing only those candidates.
  3. The human confirms or edits each proposal.
  4. Identity, address and staleness are computed, never proposed, because
     git and SHA-256 give right answers where a model would guess.

Everything captured from a real artifact enters at L0, including artifacts that
are already summaries — a layer is structural depth, not semantic abstraction
(§5). This script therefore has no notion of choosing a layer.
"""

import argparse
import sys
import textwrap
from pathlib import Path

import extract
import llm
import retrieval
import shard

MODEL = llm.DEFAULT_MODEL

# Tier vocabulary lives in shard.py with the rest of the format (§4 field 5).
# Re-exported here because capture's CLI and prompt are both written against
# it, and synthesize.py needs the same ordering to inherit a tier safely.
CONFIDENTIALITY = shard.CONFIDENTIALITY

# The artifact is sent whole. Beyond this it is refused rather than truncated:
# a silently shortened artifact yields a representation that claims to stand for
# the whole thing, which is a worse failure than a loud one.
MAX_ARTIFACT_BYTES = 2_000_000

SYSTEM_PROMPT = """\
You are the capture stage of a Knowledge Hub: a network of representations of \
real work artifacts. You are shown one real artifact and you produce the \
representation of it that will be stored.

The representation is not the artifact. The artifact stays where it is and is \
addressed by path; your output must be usable on its own terms, and must never \
attempt to reproduce the artifact's content. Assume the reader is a future \
session that has never seen this artifact and must decide, from your summary \
alone, whether opening it is worth the cost.

Produce exactly six things.

title    A specific, human-legible name, **under about 60 characters**. Name \
the actual subject, not the file: "Patent geocoding methodology" is a title; \
"Project context notes" is not. It labels a node in a graph view and heads a \
list, so it has to survive truncation — the qualifiers, dates and caveats \
belong in the summary, which is right underneath it.

summary  Prose, and short — roughly 60-120 words, not several paragraphs. The \
Hub's value is in sweeping across many shards to find connections and the big \
picture; that only works if a summary can be read in seconds, with the real \
artifact one click away via the address for anyone who needs the full detail. \
State the specific claim, decision, or method — never "this document \
discusses" — but state it briefly: one sharp sentence beats three hedged ones. \
If the artifact records a decision or failure mode, name it; don't reargue it.

context  Prose explaining where this sits in relation to the rest of the Hub, \
and what it connects to and why. Reference other shards by their title. If no \
candidate is related, say what this shard would connect to once such material \
exists. This is reasoning, not a list.

links    Connections to the candidate shards you are shown, each with the reason \
it connects. The candidates were retrieved mechanically — by one artifact naming \
another, by word overlap, by sharing a directory — and retrieval is not a reason. \
State why the two shards belong together: a shared method, a shared failure mode, \
a dependency, a contradiction. "Both relate to the project" and "retrieved by \
lexical overlap" are not reasons. A candidate that is merely nearby is not a link; \
return fewer links rather than weak ones, and only ever use ids from the candidate \
list.

confidentiality  One of the four tiers you are given. Judge by what the artifact \
would expose if its content were disclosed, not by how it is stored. When torn \
between two tiers, choose the more restrictive one — under-classifying is the \
expensive error.

facets   Optional tags along named dimensions (work/personal, by project, by \
institution, by person). You are shown the vocabulary already in use: reuse an \
existing tag whenever it fits rather than coining a synonym for it. Propose a new \
tag only for a dimension genuinely absent from the vocabulary, and prefer few. \
An empty list is valid.
"""


def output_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "context": {"type": "string"},
            "confidentiality": {"type": "string", "enum": list(CONFIDENTIALITY)},
            "facets": {"type": "array", "items": {"type": "string"}},
            "links": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "why": {"type": "string"}},
                    "required": ["id", "why"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["title", "summary", "context", "confidentiality", "facets", "links"],
        "additionalProperties": False,
    }


def build_prompt(path: Path, content: str, candidates: list[dict], vocabulary: list[str]) -> str:
    if candidates:
        listing = "\n\n".join(
            f"- id: {c['id']}\n  title: {c['title']}\n"
            f"  retrieved because: {c['retrieved_because']}\n  summary: {c['summary']}"
            for c in candidates
        )
    else:
        listing = ("(No existing shard was retrieved as a candidate. Return an empty "
                   "links list.)")

    tiers = "\n".join(f"- {name}: {desc}" for name, desc in CONFIDENTIALITY.items())
    vocab = ", ".join(vocabulary) if vocabulary else "(none yet — this is the first shard)"

    return (
        f"<confidentiality_tiers>\n{tiers}\n</confidentiality_tiers>\n\n"
        f"<facet_vocabulary_in_use>\n{vocab}\n</facet_vocabulary_in_use>\n\n"
        f"<candidate_shards>\n{listing}\n</candidate_shards>\n\n"
        f"<artifact address=\"{path}\">\n{content}\n</artifact>\n\n"
        "Produce the representation of this artifact."
    )


def call_claude(prompt: str, model: str, max_cost: float | None) -> dict:
    """One LLM call, structured output validated against this stage's schema.

    The mechanics live in llm.call — the one place in pipeline/ that knows how
    to reach a model — so swapping the Processor is a change to that file
    alone (§2). This wrapper only supplies capture's own prompt and schema.
    """
    return llm.call(prompt, SYSTEM_PROMPT, output_schema(),
                    model=model, max_cost=max_cost)


# --------------------------------------------------------------------------
# human in the loop
# --------------------------------------------------------------------------

def _ask(usage: str = "", indent: int = 10) -> str:
    """Read one line, showing what the available keys do on the prompt itself.

    The usage sits inline rather than on a line above, so it stays next to the
    cursor and survives in a scrolled-back transcript. End-of-input here means
    the human pressed Ctrl-D, which is taken as accepting the current field;
    a run with no terminal at all is refused up front by main() instead, so
    "nobody was there" can never be silently recorded as "the human agreed".
    """
    hint = f"[{usage}]  " if usage else ""
    try:
        return input(f"{' ' * indent}{hint}> ").strip()
    except EOFError:
        print()
        return ""


def confirm(proposed: dict, candidates: list[dict], vocabulary: list[str]) -> dict:
    """Walk the human through every proposed field before anything is written."""
    titles = {c["id"]: c["title"] for c in candidates}

    print("\n" + "─" * 78)

    # title -----------------------------------------------------------------
    print(f"\n  title   {proposed['title']}")
    if (answer := _ask("↵ accept · type a replacement")):
        proposed["title"] = answer

    # confidentiality -------------------------------------------------------
    levels = list(CONFIDENTIALITY)
    print(f"\n  conf    proposed: {proposed['confidentiality']}")
    for i, name in enumerate(levels, 1):
        mark = " ←proposed" if name == proposed["confidentiality"] else ""
        print(f"          [{i}] {name:<22} {CONFIDENTIALITY[name]}{mark}")
    while (answer := _ask(f"↵ accept · 1-{len(levels)} choose")):
        if answer.isdigit() and 1 <= int(answer) <= len(levels):
            proposed["confidentiality"] = levels[int(answer) - 1]
            print(f"          → {proposed['confidentiality']}")
            break
        print(f"          not a choice: {answer!r}")

    # facets ----------------------------------------------------------------
    facets = list(dict.fromkeys(proposed.get("facets") or []))
    while True:
        if facets:
            print("\n  facets  " + "  ".join(f"[{i}] {f}" for i, f in enumerate(facets, 1)))
        else:
            print("\n  facets  (none)")
        # Name a real index in the hint, so "-1" is shown as something that
        # works rather than as a placeholder to be decoded.
        usage = "↵ accept · " + ("-1 drop #1 · " if facets else "") + "+<tag> add · ? vocabulary"
        answer = _ask(usage)
        if not answer:
            break
        drops, adds, bad = [], [], []
        for token in answer.split():
            if token == "?":
                print("          in use: " + (", ".join(vocabulary) or "(none yet)"))
            elif token.startswith("-") and token[1:].isdigit():
                idx = int(token[1:])
                (drops if 1 <= idx <= len(facets) else bad).append(idx if 1 <= idx <= len(facets) else token)
            elif token.startswith("+") and len(token) > 1:
                adds.append(token[1:])
            else:
                bad.append(token)
        # Drop by descending index so earlier removals do not shift later ones.
        for idx in sorted(set(drops), reverse=True):
            facets.pop(idx - 1)
        facets.extend(a for a in adds if a not in facets)
        for token in bad:
            print(f"          ignored: {token!r}")
    proposed["facets"] = facets

    # links -----------------------------------------------------------------
    kept = []
    if proposed["links"]:
        print(f"\n  links   {len(proposed['links'])} proposed")
        for i, link in enumerate(proposed["links"], 1):
            print(f"\n          [{i}] {link['id']} · {titles.get(link['id'], '(unknown shard)')}")
            for n, line in enumerate(textwrap.wrap(link["why"], 62) or [""]):
                label = "why:" if n == 0 else "    "
                print(f"              {label} {line}")
            answer = _ask("↵ accept · n reject · e edit reason", indent=14).lower()
            if answer == "n":
                print("              rejected")
                continue
            if answer == "e":
                new_reason = _ask("type the corrected reason", indent=14)
                if new_reason:
                    link["why"] = new_reason
            kept.append(link)
    else:
        print("\n  links   none proposed")
    proposed["links"] = kept

    print("\n" + "─" * 78)
    return proposed



def validate_links(proposed: list[dict], known_ids: set[str]) -> tuple[list[dict], list[dict]]:
    """Keep only links whose target actually exists.

    The model is given the candidate list, but a fabricated id would create a
    dangling edge that every later graph operation has to defend against, so
    unknown targets are dropped here rather than written to disk.
    """
    kept = [l for l in proposed if l["id"] in known_ids]
    dropped = [l for l in proposed if l["id"] not in known_ids]
    return kept, dropped


def main() -> int:
    ap = argparse.ArgumentParser(description="Capture a local markdown file into an L0 shard.")
    ap.add_argument("artifact", type=Path)
    ap.add_argument("--confidentiality", choices=list(CONFIDENTIALITY), default=None,
                    help="Set the tier directly instead of confirming the proposed one.")
    ap.add_argument("--facet", action="append", default=[], metavar="TAG",
                    help="Seed an extra facet, repeatable (§6).")
    ap.add_argument("--candidates", type=int, default=retrieval.DEFAULT_K,
                    help="How many retrieved shards the model may link to.")
    ap.add_argument("--cross-project-min", type=int,
                    default=retrieval.DEFAULT_CROSS_PROJECT_MIN,
                    help="Candidate slots reserved for shards outside this "
                         "artifact's own project, so a large project cannot "
                         "crowd out every cross-project candidate. 0 disables.")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--max-cost", type=float, default=None,
                    help="Abort the LLM call above this dollar amount.")
    ap.add_argument("--force", action="store_true",
                    help="Capture even if a shard already points at this address.")
    ap.add_argument("--full-source", action="store_true",
                    help="For .py files, capture full source instead of the "
                         "default docstrings+comments-only extraction. Use "
                         "when a script's value is in its implementation, "
                         "not just the reasoning behind it.")
    ap.add_argument("--yes", action="store_true",
                    help="Accept every proposal without confirming. For batch runs.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the assembled prompt and exit without calling the model.")
    args = ap.parse_args()

    # Refuse before spending anything. The confirm step is the only thing
    # standing between a proposal and the Hub, and a redirected or absent stdin
    # would sail through it accepting everything — recording agreement from a
    # human who was never asked. Fail loudly and early instead.
    if not args.yes and not args.dry_run and not sys.stdin.isatty():
        print("error: the confirm step needs a terminal, and stdin is not one.\n"
              "       Run this from a shell to review each proposal, or pass --yes\n"
              "       to accept every proposal unreviewed (batch runs only).",
              file=sys.stderr)
        return 1

    path = args.artifact.resolve()
    if not path.is_file():
        print(f"error: {path} is not a file", file=sys.stderr)
        return 1
    size = path.stat().st_size
    try:
        content = extract.extract(path, full_source=args.full_source)
    except (extract.Unsupported, extract.NotEnoughText) as exc:
        print(f"error: cannot capture {path.name} — {exc}", file=sys.stderr)
        return 1

    # The limit applies to extracted text, since that is what reaches the model:
    # a large PDF may yield little text, and a small one a great deal.
    if len(content) > MAX_ARTIFACT_BYTES:
        print(f"error: {path.name} yielded {len(content):,} characters, above the "
              f"{MAX_ARTIFACT_BYTES:,} capture limit. It is not truncated "
              f"automatically — split it and capture the parts, or raise "
              f"MAX_ARTIFACT_BYTES deliberately.", file=sys.stderr)
        return 1

    existing = shard.all_shards()
    duplicate = next((s for s in existing if s.get("address") == str(path)), None)
    if duplicate and not args.force:
        print(f"already captured: {duplicate['id']} represents this artifact "
              f"({duplicate['path']}). Use --force to add a second shard.")
        # Exit 2, distinct from 1, so a batch runner can tell "already done"
        # apart from "went wrong" and resume without counting skips as errors.
        return 2

    pool = [s for s in existing if s.get("address") != str(path)]
    vocabulary = sorted({f for s in existing for f in (s.get("facets") or [])})

    candidates = retrieval.select_candidates(content, path, pool, facets=args.facet,
                                             k=args.candidates,
                                             cross_project_min=args.cross_project_min)
    prompt = build_prompt(path, content, candidates, vocabulary)

    if args.dry_run:
        print(prompt)
        return 0

    print(f"capturing {path.name} ({size:,} bytes → {len(content):,} chars of text)")
    print(f"  retrieved {len(candidates)} candidate(s) from {len(pool)} shard(s) "
          f"— prompt carries {len(prompt):,} bytes")
    result = call_claude(prompt, args.model, args.max_cost)
    out = result["structured_output"]

    kept, dropped = validate_links(out["links"], {s["id"] for s in existing})
    for link in dropped:
        print(f"  warning: dropped link to unknown id {link['id']!r}", file=sys.stderr)
    out["links"] = kept

    # Seeded facets are the human's, so they join the proposal rather than
    # competing with it; the confirm step is where either can still be removed.
    out["facets"] = list(dict.fromkeys(args.facet + (out.get("facets") or [])))
    if args.confidentiality:
        out["confidentiality"] = args.confidentiality

    if not args.yes:
        out = confirm(out, candidates, vocabulary)

    if not out["links"] and pool:
        print("  note: no links — an unconnected shard is inert (§6).", file=sys.stderr)

    meta = {
        "id": shard.new_id(layer=0),
        "title": out["title"],
        "layer": 0,
        "confidentiality": out["confidentiality"],
        "facets": out["facets"],
        "children": [],
        "links": out["links"],
        "last_verified": shard.today(),
        **shard.describe_artifact(path),
    }
    written = shard.write(meta, out["summary"], out["context"])
    manifest = shard.build_index()

    cost = result.get("total_cost_usd")
    print(f"\nwrote {written.relative_to(shard.REPO)}  ({meta['id']}: {meta['title']})")
    line = (f"  {meta['address_type']}  ·  {meta['confidentiality']}  ·  "
            f"{len(meta['links'])} link(s)  ·  facets: {', '.join(meta['facets']) or 'none'}")
    if cost is not None:
        line += f"  ·  ${cost:.4f}"
    print(line)
    print(f"  index: {manifest['shard_count']} shard(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
