"""Deterministic candidate retrieval for link proposal.

No LLM, no embedding model, no network. Given a new artifact and the existing
shards, rank the shards by how likely they are to connect and return the top
few. Capture then shows the model only those, so the size of a capture prompt
stops depending on the size of the Hub (founding_design.md §3 applied to the
write path).

This answers only *which shards are worth considering*. It deliberately does
not decide whether a link exists or why: a scorer produces a number, and §3 is
explicit that a connection without stated reasoning is collection rather than
knowledge. The reason is written downstream, over a bounded candidate set.

Kept separate from capture.py because the same ranking is what §8's grouping
pass and any later link-suggestion command will need, neither of which captures
anything.
"""

import math
import re
from collections import Counter
from pathlib import Path

DEFAULT_K = 8

# Reserved candidate slots that must go to shards outside the artifact's own
# project, even when they rank below same-project shards on raw score. Without
# this, a project with enough internal material fills every slot with itself —
# measured on the real Hub: 18 Cyan Cycle shards produced 84 internal links and
# 1 cross-domain link, versus another project's 5 shards producing 3 cross-domain
# links from a smaller, less crowded pool. The reservation does not lower the
# bar for what counts as a candidate (§3's per-candidate score threshold still
# applies) — it only protects cross-project candidates that already cleared
# that bar from being outranked purely by within-project density.
DEFAULT_CROSS_PROJECT_MIN = 3

# Signal weights. Reference detection dominates because it is a fact about the
# text rather than an inference from it; the rest only reorder the tail.
W_REFERENCE = 3.0
W_LEXICAL = 1.0
W_SAME_DIR = 0.6
W_FACET = 0.5

BM25_K1 = 1.5
BM25_B = 0.75

STOP = {
    "the", "and", "for", "that", "this", "with", "from", "are", "was", "were",
    "has", "have", "had", "but", "not", "all", "any", "can", "its", "his", "her",
    "they", "them", "then", "than", "when", "what", "which", "who", "how", "why",
    "into", "over", "each", "also", "been", "being", "more", "most", "such",
    "some", "only", "other", "same", "these", "those", "there", "here", "would",
    "could", "should", "will", "one", "two", "use", "used", "using", "via",
}

# Basenames too common to identify an artifact on their own. A mention of one of
# these only counts when the path above it matches too.
GENERIC_STEMS = {
    "readme", "index", "notes", "todo", "changelog", "license", "main", "init",
    "setup", "config", "summary", "overview", "draft", "untitled",
}


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9_]+", text.lower())
            if len(t) >= 3 and t not in STOP]


# --------------------------------------------------------------------------
# signal 1: explicit references
# --------------------------------------------------------------------------

def find_references(artifact_text: str, shards: list[dict]) -> dict[str, int]:
    """Count mentions of each shard's artifact inside the new artifact's text.

    Only this direction is checkable. The reverse — an already-captured
    artifact mentioning the new one — would mean reopening artifacts the Hub
    does not store and may not be able to reach (§7). That is no loss: §4
    stores a link on the shard that declares it and derives the reverse, so the
    direction available is the direction needed.
    """
    haystack = artifact_text.lower()
    hits: dict[str, int] = {}
    for s in shards:
        address = s.get("address")
        if not address:
            continue
        name = Path(address).name.lower()
        stem = Path(address).stem.lower()
        # A generic name identifies nothing by itself, so require the parent
        # directory alongside it before believing the mention.
        needle = f"{Path(address).parent.name.lower()}/{name}" if stem in GENERIC_STEMS else name
        count = haystack.count(needle)
        if count:
            hits[s["id"]] = count
    return hits


# --------------------------------------------------------------------------
# signal 2: lexical similarity (BM25)
# --------------------------------------------------------------------------

def bm25(query_tokens: list[str], corpus_tokens: list[list[str]]) -> list[float]:
    """Standard BM25 over the shards' own text. Pure stdlib; milliseconds at
    corpus sizes far beyond anything this Hub will reach."""
    n = len(corpus_tokens)
    if n == 0:
        return []
    doc_counts = [Counter(d) for d in corpus_tokens]
    lengths = [len(d) for d in corpus_tokens]
    avgdl = sum(lengths) / n or 1.0

    df = Counter()
    for d in doc_counts:
        df.update(d.keys())

    # Score against the distinct query terms the corpus actually knows, so a
    # 200 KB artifact does not cost more to rank than a small one.
    query_terms = {t for t in query_tokens if t in df}

    scores = []
    for counts, length in zip(doc_counts, lengths):
        total = 0.0
        for term in query_terms:
            f = counts.get(term, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            denom = f + BM25_K1 * (1 - BM25_B + BM25_B * length / avgdl)
            total += idf * (f * (BM25_K1 + 1)) / denom
        scores.append(total)
    return scores


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------

def _project_tags(facets: list[str]) -> set[str]:
    """The `project:*` facets, which is what "same project" means for the
    cross-project reservation. Axis facets like `work`/`personal` are excluded
    deliberately: two shards from different projects that happen to both be
    `work` are exactly the cross-project case the reservation exists to protect,
    not the same-project case it should be quota'd against."""
    return {f for f in facets if f.startswith("project:")}


def select_candidates(artifact_text: str, artifact_path: Path, shards: list[dict],
                      facets: list[str] | None = None, k: int = DEFAULT_K,
                      cross_project_min: int = DEFAULT_CROSS_PROJECT_MIN) -> list[dict]:
    """Rank existing shards and return the top k, each with why it was retrieved.

    The retrieval reason travels with the candidate so the capture prompt can
    show it. A model told that a shard was retrieved because the artifact names
    it four times writes a different, better-grounded link than one handed an
    unexplained list.

    Ranking alone lets a large project crowd out every cross-project candidate,
    since same-project material scores higher on every signal here by
    construction (shared directory, shared facet, overlapping vocabulary). Up to
    `cross_project_min` of the k slots are therefore reserved for candidates
    outside the artifact's own project, filled from shards that already scored
    above zero on their own merits — never padded with unrelated material just
    to hit the quota. Silently inert when the artifact carries no `project:`
    facet: there is then no "same project" to define, so ranking is left alone.
    """
    if not shards:
        return []

    facets = facets or []
    references = find_references(artifact_text, shards)

    corpus = [tokenize(f"{s.get('title', '')} {s.get('summary', '')}") for s in shards]
    lexical = bm25(tokenize(artifact_text), corpus)

    # Lexical scores are normalized before the 0.15 cutoff is applied, and the
    # comparison set that normalization uses matters. Comparing every candidate
    # against one pool-wide max lets a large, internally similar project drag
    # its own ceiling up (shared report templates, shared terminology) and
    # silently drag every cross-project score under the cutoff with it —
    # measured on the real Hub: a genuine lexical match from another project
    # (raw BM25 8.3) fell to a normalized 0.06 once one project's mutual
    # similarity pushed the pool max to 136, well below the threshold that
    # would have let it through. Normalizing same-project and cross-project
    # candidates against their own bucket's max keeps the cutoff meaning "well
    # above average for its comparison group" for both, instead of "well above
    # average for whichever group happens to be larger".
    artifact_projects = _project_tags(facets)

    def _same_project(cand_facets: list[str]) -> bool:
        # With no project facet on the artifact there is no bucket to compare
        # against, so every candidate shares one pool-wide comparison group —
        # this collapses to the previous, unbucketed behaviour.
        if not artifact_projects:
            return True
        return bool(_project_tags(cand_facets) & artifact_projects)

    same_lex = [l for s, l in zip(shards, lexical) if _same_project(s.get("facets") or [])]
    cross_lex = [l for s, l in zip(shards, lexical) if not _same_project(s.get("facets") or [])]
    top_same = max(same_lex) if same_lex and max(same_lex) > 0 else 1.0
    top_cross = max(cross_lex) if cross_lex and max(cross_lex) > 0 else 1.0

    parent = artifact_path.parent

    ranked = []
    for s, lex in zip(shards, lexical):
        signals = []
        score = 0.0

        if s["id"] in references:
            count = references[s["id"]]
            score += W_REFERENCE
            signals.append(f"this artifact names {Path(s['address']).name} {count}×")

        top_lexical = top_same if _same_project(s.get("facets") or []) else top_cross
        lex_norm = lex / top_lexical
        if lex_norm > 0.15:
            score += W_LEXICAL * lex_norm
            signals.append(f"lexical overlap {lex_norm:.2f}")

        if s.get("address") and Path(s["address"]).parent == parent:
            score += W_SAME_DIR
            signals.append("same directory")

        shared = sorted(set(facets) & set(s.get("facets") or []))
        if shared:
            score += W_FACET * len(shared) / max(len(facets), 1)
            signals.append(f"shared facets: {', '.join(shared)}")

        if score > 0:
            ranked.append({
                "id": s["id"],
                "title": s["title"],
                "summary": s["summary"],
                "score": round(score, 3),
                "retrieved_because": "; ".join(signals),
            })

    ranked.sort(key=lambda c: c["score"], reverse=True)

    if not artifact_projects or cross_project_min <= 0:
        return ranked[:k]

    facets_by_id = {s["id"]: s.get("facets") or [] for s in shards}
    same = [c for c in ranked if _same_project(facets_by_id.get(c["id"], []))]
    cross = [c for c in ranked if not _same_project(facets_by_id.get(c["id"], []))]

    reserved = cross[:min(cross_project_min, k, len(cross))]
    for c in reserved:
        c["retrieved_because"] += "; held a reserved cross-project slot"

    selected = same[:k - len(reserved)] + reserved
    if len(selected) < k:
        # Reserved quota went unfilled (little or no cross-project signal) or
        # same-project ran short — backfill from whatever scored next, in
        # either pool, so a slot is never left empty while candidates remain.
        chosen = {c["id"] for c in selected}
        selected += [c for c in ranked if c["id"] not in chosen][:k - len(selected)]

    selected.sort(key=lambda c: c["score"], reverse=True)
    return selected[:k]
