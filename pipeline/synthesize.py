#!/usr/bin/env python3
"""Generate a higher layer from the layer below.

    python3 pipeline/synthesize.py [--layer 0] [--resolution 1.2]

founding_design.md §8: higher layers are *generated, not authored*. Community
detection groups the shards of layer N; one LLM call per group synthesizes
that group into a layer N+1 shard whose `children` are the group's members.

Two kinds of shard come out, and the second is the reason this is more than a
summarizer:

  cluster  a community of densely-linked shards, standing for what they have
           in common.
  bridge   a *bundle of links between two communities*, standing for what the
           connection means. A cluster shard can never express this, because
           the insight lives between clusters rather than inside one — and on
           this Hub those cross-domain connections have been the most valuable
           thing in it (§1 names capturing them as the problem being solved).

Nothing is synthesized without a human seeing the group first. Groups are
proposed, with their statistics, *before* any model call, so rejecting a bad
grouping costs nothing — the same shape as capture.py, where deterministic
machinery proposes and judgment is gated by a person.

Modularity has a resolution limit, and this Hub runs into it: no single
resolution both keeps a 12-report monthly series whole and separates small
projects that share methodology. So resolution is a property of a *run*.
Re-running at a different resolution and accepting different groups is the
intended workflow, not a workaround; supersession (below) absorbs the overlap.
"""

import argparse
import hashlib
import sys
import textwrap
from collections import defaultdict

import community
import llm
import shard

# Middle of the stable plateau measured on the real graph: resolutions 1.2
# through 1.5 all produced the same partition, while 1.0 collapsed three
# projects into one blob and 1.6 began shedding singletons.
DEFAULT_RESOLUTION = 1.2

# Thresholds are ratios wherever the underlying question is a ratio, so they
# keep their meaning as the Hub grows. Values were read off the real graph
# rather than guessed — see the module docstring of community.analyze.
MIN_COHESION = 0.5        # below this, the "community" is a bad cut, not a group
MAX_BRIDGE_RATIO = 1.0    # above this, the boundary carries more than the interior
SUPERSEDE_OVERLAP = 0.5   # Jaccard over children, above which this is a new version

# These two are counts, not ratios, and deliberately so: they express "there is
# enough here to be worth one synthesis", which does not scale with corpus size.
# Three shards is the smallest group where one summary genuinely replaces
# reading the parts; two links is the smallest bundle that can show a *pattern*
# rather than restating the single link's own stated reason.
MIN_CLUSTER_SIZE = 3
MIN_BRIDGE_LINKS = 2

CLUSTER_PROMPT = """\
You are the synthesis stage of a Knowledge Hub: a network of representations of \
real work artifacts, where each layer is one synthesis step further from the \
raw material than the layer below.

You are given a group of shards that link densely to one another. Produce the \
shard that stands for the group as a whole — what a future session should read \
*instead of* all of them, when it wants the shape rather than the detail.

title    Name what the group actually is, specifically, in **under about 60 \
characters**. Not "cluster of 11 shards", not "various documents" — the real \
subject, period or throughline. It labels a node in a graph view, so it has to \
survive truncation: no dashes stacking three clauses, no dates or project \
codes the summary can carry instead.

summary  Prose, 60-120 words. What these shards have in common, what arc or \
argument runs through them, and what someone would come to this group for. Do \
not enumerate the members — a list is what `children` is for, and the reader \
can descend. Say the thing the group collectively establishes that no single \
member states on its own. If the group records a decision, a period, or a \
recurring failure, name it.

context  Prose. Where this group sits relative to the rest of the Hub, and \
what it borders on. Reasoning, not a list.
"""

BRIDGE_PROMPT = """\
You are the synthesis stage of a Knowledge Hub: a network of representations of \
real work artifacts, where each layer is one synthesis step further from the \
raw material than the layer below.

You are given the links that connect two otherwise-separate groups of shards, \
each with the reason it was recorded. Produce the shard that stands for *the \
connection itself*.

This is the one thing a summary of either side can never contain. The material \
on each side is different — different projects, often different domains \
entirely — so do not summarize either group. Say what the connection reveals.

title    Name the pattern or dependency, specifically. "Same failure mode \
across unrelated projects" is a shape; name the actual failure mode.

summary  Prose, 60-120 words. What recurring pattern, shared method, \
dependency or contradiction these links instantiate. If several links point at \
one underlying thing, say what it is. If they point at two distinct things, \
say that rather than forcing a false unity. Concrete over abstract: name the \
specific method or failure, not "these projects are related".

context  Prose. What this connection implies, and what it would take to \
falsify or extend it.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "context": {"type": "string"},
    },
    "required": ["title", "summary", "context"],
    "additionalProperties": False,
}


def _ask(usage: str = "", indent: int = 10) -> str:
    """Read one line, showing the available keys inline. Kept local rather than
    imported from capture.py: two top-level pipelines should not depend on each
    other for eight lines of terminal I/O."""
    hint = f"[{usage}]  " if usage else ""
    try:
        return input(f"{' ' * indent}{hint}> ").strip()
    except EOFError:
        print()
        return ""


# --------------------------------------------------------------------------
# proposing groups
# --------------------------------------------------------------------------

def propose(shards: list[dict], resolution: float, want_bridges: bool) -> list[dict]:
    """Partition the layer and return the groups worth synthesizing.

    Every group carries the statistics that admitted it, so the human confirming
    it can see *why* it is being offered rather than taking the partition on
    faith.
    """
    by_id = {s["id"]: s for s in shards}
    ids = set(by_id)
    edge_list = [(s["id"], l["id"], 1.0)
                 for s in shards for l in (s.get("links") or []) if l["id"] in ids]
    if not edge_list:
        return []

    comm = community.louvain(list(by_id), edge_list, resolution=resolution)
    stats = community.analyze(edge_list, comm)

    members = defaultdict(list)
    for sid in by_id:
        members[comm[sid]].append(sid)

    proposals = []
    for c, group in members.items():
        cohesion = stats["cohesion"].get(c, 0.0)
        if len(group) < MIN_CLUSTER_SIZE or cohesion < MIN_COHESION:
            continue
        proposals.append({
            "kind": "cluster",
            "children": sorted(group),
            "cohesion": cohesion,
            "note": f"{len(group)} shards, cohesion {cohesion:.2f} "
                    f"({stats['internal'].get(c, 0)} internal / "
                    f"{stats['external'].get(c, 0)} external edges)",
        })

    if want_bridges:
        # A link's stated reason is the raw material for a bridge, so they are
        # carried through to the prompt rather than just counted.
        reasons = defaultdict(list)
        for s in shards:
            for link in s.get("links") or []:
                if link["id"] not in ids:
                    continue
                a, b = comm[s["id"]], comm[link["id"]]
                if a != b:
                    reasons[(a, b) if a <= b else (b, a)].append(
                        (s["id"], link["id"], link["why"]))

        for pair, bundle in reasons.items():
            ratio = stats["bridge_ratio"].get(pair, float("inf"))
            if len(bundle) < MIN_BRIDGE_LINKS or ratio > MAX_BRIDGE_RATIO:
                continue
            endpoints = sorted({e for link in bundle for e in link[:2]})
            proposals.append({
                "kind": "bridge",
                "children": endpoints,
                "links": bundle,
                "cohesion": 1.0 - min(ratio, 1.0),
                "note": f"{len(bundle)} links between two communities, "
                        f"bridge ratio {ratio:.2f} "
                        f"({len(endpoints)} shards touched)",
            })

    proposals.sort(key=lambda p: (-p["cohesion"], p["kind"]))
    return proposals


# --------------------------------------------------------------------------
# synthesis
# --------------------------------------------------------------------------

def build_prompt(proposal: dict, by_id: dict) -> str:
    def describe(sid: str) -> str:
        s = by_id[sid]
        return f"- id: {sid}\n  title: {s['title']}\n  summary: {s.get('summary', '')}"

    if proposal["kind"] == "cluster":
        body = "\n\n".join(describe(c) for c in proposal["children"])
        return (f"<group_of_shards>\n{body}\n</group_of_shards>\n\n"
                "Produce the shard that stands for this group.")

    lines = []
    for src, dst, why in proposal["links"]:
        lines.append(f"- {by_id[src]['title']}\n  → {by_id[dst]['title']}\n  why: {why}")
    connecting = "\n\n".join(lines)
    endpoints = "\n\n".join(describe(c) for c in proposal["children"])
    return (f"<links_between_the_groups>\n{connecting}\n</links_between_the_groups>\n\n"
            f"<shards_at_the_endpoints>\n{endpoints}\n</shards_at_the_endpoints>\n\n"
            "Produce the shard that stands for this connection.")


def children_marker(children: list[str], by_id: dict) -> str:
    """Staleness for a derived shard (§7): a hash over the children's ids and
    summaries. A synthesis is stale exactly when what it was synthesized from
    has changed — there is no external artifact to compare against, so the
    inputs themselves are the thing to watch."""
    h = hashlib.sha256()
    for cid in sorted(children):
        h.update(cid.encode())
        h.update((by_id[cid].get("summary") or "").encode())
    return "children:" + h.hexdigest()[:32]


def derive_facets(children: list[str], by_id: dict, kind: str) -> list[str]:
    """Facets a synthesized shard inherits from what it stands for.

    A cluster takes the intersection — the facets *every* member shares, which
    is what the group as a whole is actually about. A bridge takes the union,
    because a connection genuinely touches every project it spans, and an
    intersection across domains would usually be empty.
    """
    sets = [set(by_id[c].get("facets") or []) for c in children]
    if not sets:
        return []
    if kind == "bridge":
        return sorted(set().union(*sets))
    common = set.intersection(*sets)
    return sorted(common) if common else sorted(set().union(*sets))


def find_superseded(children: list[str], existing: list[dict]) -> str | None:
    """An existing shard at this layer that the new one is a new version of.

    Identity is by overlap of what a shard stands for, not by title: if most of
    the same children are being synthesized again, this is a regeneration of
    that shard rather than a new one. Below the threshold it is genuinely a
    different grouping and both are kept.
    """
    new = set(children)
    best, best_overlap = None, 0.0
    for s in existing:
        old = set(s.get("children") or [])
        if not old:
            continue
        overlap = len(new & old) / len(new | old)
        if overlap > best_overlap:
            best, best_overlap = s, overlap
    return best["id"] if best and best_overlap >= SUPERSEDE_OVERLAP else None


def confirm_fields(proposed: dict) -> dict:
    print(f"\n  title   {proposed['title']}")
    if (answer := _ask("↵ accept · type a replacement")):
        proposed["title"] = answer
    for field in ("summary", "context"):
        print(f"\n  {field}")
        for line in textwrap.wrap(proposed[field], 66):
            print(f"          {line}")
        if (answer := _ask("↵ accept · type a replacement")):
            proposed[field] = answer
    return proposed


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate layer N+1 from layer N (§8).")
    ap.add_argument("--layer", type=int, default=0, help="Source layer. Default 0.")
    ap.add_argument("--resolution", type=float, default=DEFAULT_RESOLUTION,
                    help="Louvain resolution. Higher splits more finely. "
                         "Re-run at different values and accept different groups.")
    ap.add_argument("--no-bridges", action="store_true",
                    help="Only synthesize clusters, not the links between them.")
    ap.add_argument("--model", default=llm.DEFAULT_MODEL)
    ap.add_argument("--max-cost", type=float, default=None)
    ap.add_argument("--yes", action="store_true",
                    help="Accept every group and every field unreviewed.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show the proposed groups and exit, without calling the model.")
    args = ap.parse_args()

    if not args.yes and not args.dry_run and not sys.stdin.isatty():
        print("error: the confirm step needs a terminal, and stdin is not one.\n"
              "       Run this from a shell to review each group, or pass --yes\n"
              "       to synthesize every proposed group unreviewed.", file=sys.stderr)
        return 1

    everything = shard.all_shards()
    source = [s for s in everything if s["layer"] == args.layer]
    if not source:
        print(f"error: no shards at layer {args.layer}", file=sys.stderr)
        return 1
    by_id = {s["id"]: s for s in source}
    existing_above = [s for s in everything if s["layer"] == args.layer + 1]

    proposals = propose(source, args.resolution, not args.no_bridges)
    print(f"layer {args.layer}: {len(source)} shards → "
          f"{len(proposals)} group(s) worth synthesizing "
          f"(resolution {args.resolution})")
    if not proposals:
        print("  nothing met the thresholds. Try a different --resolution.")
        return 0

    for i, p in enumerate(proposals, 1):
        print(f"\n  [{i}] {p['kind']}: {p['note']}")
        for cid in p["children"][:6]:
            print(f"        {by_id[cid]['title'][:66]}")
        if len(p["children"]) > 6:
            print(f"        … and {len(p['children']) - 6} more")

    if args.dry_run:
        return 0

    written = 0
    for i, p in enumerate(proposals, 1):
        print(f"\n{'─' * 78}\n  [{i}/{len(proposals)}] {p['kind']}: {p['note']}")
        if not args.yes:
            if _ask("↵ synthesize this group · s skip").lower() == "s":
                print("          skipped")
                continue

        result = llm.call(build_prompt(p, by_id),
                          CLUSTER_PROMPT if p["kind"] == "cluster" else BRIDGE_PROMPT,
                          OUTPUT_SCHEMA, model=args.model, max_cost=args.max_cost)
        out = result["structured_output"]
        if not args.yes:
            out = confirm_fields(out)

        children = p["children"]
        meta = {
            "id": shard.new_id(layer=args.layer + 1),
            "title": out["title"],
            "layer": args.layer + 1,
            # No `address`: a synthesis stands for other shards, not for an
            # artifact out in the world. address_type names how to check it.
            "address_type": "derived",
            "staleness_marker": children_marker(children, by_id),
            "last_verified": shard.today(),
            "confidentiality": shard.most_restrictive(
                [by_id[c].get("confidentiality") for c in children]),
            "facets": derive_facets(children, by_id, p["kind"]),
            "children": children,
            "links": [],
        }
        if (old := find_superseded(children, existing_above)):
            meta["supersedes"] = old
            print(f"          supersedes {old}")

        path = shard.write(meta, out["summary"], out["context"])
        cost = result.get("total_cost_usd")
        print(f"          wrote {path.relative_to(shard.REPO)}  ({meta['confidentiality']}"
              + (f", ${cost:.4f}" if cost is not None else "") + ")")
        written += 1

    manifest = shard.build_index()
    above = sum(1 for s in manifest["shards"] if s["layer"] == args.layer + 1)
    print(f"\n{written} shard(s) written to L{args.layer + 1}. "
          f"Layer {args.layer}: {len(source)} → layer {args.layer + 1}: {above} "
          f"(compression {above / len(source):.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
