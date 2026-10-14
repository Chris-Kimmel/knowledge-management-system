# Knowledge Management System — Founding Design

**Status:** v0.1. Written before any implementation exists.
**Scope:** This document defines the system. It is the root design artifact;
everything else in this repository derives from it.

---

## 1. Purpose

A persistent, queryable infrastructure covering all digital work, replacing
fragmented session-by-session LLM work with a single evolving system that any
session can attach to, zoom into, and extend.

The problem being solved: work currently happens in isolated sessions scoped to
individual project folders. Context does not accumulate across them without loss, connections
between projects are never captured, and each session re-derives what earlier
sessions already knew. The system below makes the knowledge persistent and the
processor disposable, rather than the other way around.

---

## 2. The three elements

**1. Knowledge Hub** — a fractal network of *representations* (knowledge
shards). Contains representations only; never real artifacts, confidential or
not. Same node schema at every level of abstraction, from the thinnest leaf to
the most general cluster.

**2. Knowledge Processor** — the LLM (and the human). Stateless and swappable. A
new session, or a different model entirely, is simply a different tool applied to
the same persistent Hub. No knowledge lives in the Processor.

**3. The Link** — everything connecting Processor to Hub. Four distinct
functions:

- **Initiation** — the constant-cost entry layer loaded at the start of every
  session (`CLAUDE.md` plus a small index). Tells the Processor what the Hub is
  and what is in it, without loading any of it.
- **Access layer** — the live, permission-gated connection from a representation
  to the real artifact it stands for.
- **Capture** — the pipeline that turns a real artifact into a knowledge shard
  (the write path).
- **Sync** — staleness detection and refresh of representations whose underlying
  artifacts have changed.

---

## 3. Core principles

**Constant entry cost.** Growth of the Hub must never increase the cost of
starting a session. Achieved by loading a small index plus on-demand zoom, never
the full corpus. This is the central constraint; any design choice that violates
it is wrong regardless of its other merits.

**Pointer, not content.** Real artifacts — confidential or not — stay external
to the Hub. The Hub is representation all the way down to its thinnest leaf.
This holds universally, not just for sensitive material: a single canonical
source plus a reliable address beats two copies that can drift.

**Fractal uniformity.** Identical shard schema at every abstraction level. A
leaf and a top-level cluster differ only in how their fields are populated, never
in their structure. This is what makes arbitrary zoom possible.

**Explicit links.** Connections between shards must state *why* they connect.
Stated separately from the shard's own summary, never merged into it.
(Zettelkasten-derived: connections without stated reasoning are collection, not
knowledge production.)

**Lazy, access-triggered sync.** Representations refresh when visited, not
continuously. Eager background syncing of the whole Hub would violate constant
entry cost.

**Bottom-up emergence.** Higher layers are generated from clusters of lower
layers, not imposed from above. Deliberately no top-down structure in v0.1 — this
is an experiment, and the failure mode it risks is recorded in §8.

**Infrastructure-agnostic leaves.** Real artifacts may live anywhere: local
disk, git repositories, or behind MCP connectors (Google Drive, Gmail, etc.).
The schema's address field simply resolves differently per source type.

**Federation as a consequence** Because artifacts are external
and objective while representations are internal and subjective, independent
KMS instances (e.g. a colleague's) could dock onto the same real artifacts while
holding entirely different representations of them. Not built for; not
foreclosed. The human analogy: shared external reality, divergent
internal models, aligned by pointing at the same real things.

---

## 4. The knowledge shard

Every node in the Hub, at every level, has the same five fields:

1. **Unique ID + timestamp** — canonical identity.
2. **Descriptive title** — human-legible name.
3. **Content/summary** — a representation, never the artifact itself. Fidelity
   set by confidentiality tier (§9).
4. **Contextualization** — explicit, reasoned links to neighbouring shards. Link
   targets are machine-readable; the reasoning is prose.
5. **Metadata** — address of the real artifact, staleness marker, confidentiality
   tier, child shard IDs, and state history. Statistics are logged as raw events
   and derived later, rather than pre-defined as fixed fields.

### File format

One file per shard. Markdown with YAML frontmatter. Not one file per layer.

Rationale: per-shard files make `git log` the state history for free (field 5);
they keep reading one shard to the cost of one shard (constant entry cost); they
make the Obsidian graph view largely free when visualization arrives (§10); and
they are the Processor's native working format.

```
---
id: 20260917-1310
title: Patent geocoding methodology
layer: 0
address: ~/Projects/patent-digitization/MEMORY/...
address_type: local_git
staleness_marker: <commit hash at capture>
confidentiality: internal
facets: [work, project:patents]    # optional; see §6
children: []
links:
  - id: 20260917-1315
    why: shares the entity-resolution failure mode
---

## Summary
<field 3 — prose>

## Context
<field 4 — prose, with inline [[wikilinks]]>
```

Parent→child is stored in one direction only (`children`); the reverse is
derived, so there is never more than one place to update.

### Derived index

Aggregate operations (clustering, network statistics, graph building) need
everything at once. These read a **derived index** — a single manifest generated
from the shard files by the pipeline. It is a build artifact, never a source of
truth, and is regenerated rather than edited.

---

## 5. Storage layout

```
Knowledge_Hub/
  CLAUDE.md              # initiation layer — read at session start
  founding_design.md     # this document
  shards/
    L0/                  # leaf shards, bound to real artifacts
    L1/                  # first emergent layer
    ...
  index/                 # derived manifests (build artifacts)
  pipeline/              # capture pipeline code
```

All of it — design, shards, pipeline — lives in one private repository. The
Hub/Processor/Link split is a logical distinction, not a physical one.

The repository is tracked by **git**, locally. GitHub is a remote backup and
nothing more. The system functions fully offline, and no shard's history depends
on GitHub being reachable.

There is no separate "registry" of projects. A project pointer is simply a
knowledge shard whose field 3 is minimal. Thin and thick shards are the same
kind of object, per fractal uniformity.

### What a layer means

A layer number records **structural depth — how many synthesis steps separate a
shard from a real artifact — not semantic abstraction.**

Everything captured from a real artifact enters at **L0**, including artifacts
that are themselves already condensed: a paper's abstract, a meeting summary, a
review article. Structurally these are one hop from an artifact, which is what L0
means, even though their content is more abstract than a raw data file's.

L0 is therefore semantically heterogeneous by design, and no attempt is made to
make a layer internally coherent in level of abstraction. Coherence emerges
upward instead: an L1 shard is synthesized from several L0 shards and is
therefore more general than any of them by construction. Enforcing semantic
homogeneity within a layer would require continuously re-judging where each shard
belongs — the same instability §8 warns about, imported into the one part of the
system that is currently deterministic.

---

## 6. Capture pipeline

Turns a real artifact into a knowledge shard. An artifact is capturable once it
has:

- a **stable, resolvable address** (file path, repo + commit, message ID, file
  ID, URL) — something the access layer can use later to re-locate it;
- enough content to generate a **title and summary**;
- at least one **proposed connection** into the existing graph — an unconnected
  shard is inert under the explicit-links principle;
- an assigned **confidentiality tier**, which sets how much fidelity survives
  into field 3.

Optionally, a shard may also carry **facets** — tags assigned at capture along
named dimensions (work/personal, by project, by institution, by person). Facets
are deliberately optional and additive: a shard is valid without them, they can
be added retroactively, and nothing breaks if they are used inconsistently at
first. They are the system's designated lever for reintroducing top-down
structure later (§8) without committing to a taxonomy now.

**v1 scope discipline:** one script, one source type (local markdown files), one
LLM call producing the five fields. Multi-format and multi-source support comes
only after the schema has survived contact with real files.

---

## 7. Sync and staleness

Two different histories are involved here, and they must not be confused.

**The Hub's own state history** is always available and never depends on where
artifacts live. Every shard is a file in this repository, so a shard's evolution
is simply its file's git history. This works identically whether the shard points
at a GitHub repository, a file on an external SSD, or a document behind an MCP
connector.

**An artifact's staleness** is the separate question of whether a representation
still matches the thing it represents. Refresh is **lazy and access-triggered**:
when the access layer opens a shard — the moment freshness actually matters — it
compares the stored staleness marker against the artifact's current state. On
mismatch, capture re-runs for that one shard.

The marker's form depends on `address_type`:

| address_type | staleness marker | how it is checked |
|---|---|---|
| `local_git` | commit hash at capture | compare two short strings; the artifact itself is never opened or read |
| `local_file` | content hash (SHA-256) | compare file size and modification time first, and hash only if those differ |
| `mcp_*` | whatever the source exposes (version number, modified timestamp) | source-specific |
| immutable | none needed | a sent email or a published DOI cannot change |

**Unreachable is a third state, not a synonym for fresh.** If an external drive
is unplugged or a connector is unauthorized, the check cannot run. The shard is
then marked *unverified*, carrying the date it was last confirmed — never
silently treated as current. This matters most for exactly those artifacts that
are offline most of the time.

No background watcher, no eager full-Hub sync. A periodic sweep — weekly, say —
that revalidates staleness and re-synthesizes upper layers in the background is a
natural later addition, playing roughly the role sleep plays for human memory:
consolidating without being asked. The design does not require one.

---

## 8. Bottom-up layer generation

Higher layers are generated, not authored. A command groups the shards of layer
*N*, then runs an LLM synthesis pass over each group to produce its title,
summary, and context — fields 2, 3 and 4 of a new layer *N+1* shard, whose
`children` are the group's members.

### How shards are grouped

Two mechanisms are available, and they answer different questions.

**Community detection on the link graph** (Louvain or Leiden algorithms) groups
shards by the connections recorded in field 4. It needs no embedding model, runs
in milliseconds, and is interpretable: a group's rationale can be read directly
from the stated reasons on its links. Its limit is that it sees only declared
structure — shards nobody connected will not be grouped, however similar they
are.

**Embedding-based clustering** places shard text in a semantic space and finds
dense regions. It discovers affinities nobody declared, at the cost of an
embedding model, an index to maintain, and groupings that are harder to explain.

**Decision: link-based community detection first.** It requires no new
infrastructure, and it makes the explicit-links discipline pay off mechanically
rather than only philosophically. Embeddings are a later addition, and their best
first use is probably not grouping at all but *link suggestion* — proposing
connections for the human or the Processor to accept, reject, or annotate with a
reason, which then feeds back into the link graph.

Neither mechanism allows the axis of grouping to be specified. Wanting groups
along named dimensions — work versus personal, by institution, by person — is not
clustering but **faceting**: assign tags at capture time and group by tag. That is
deterministic and controllable, and the two coexist. Clustering discovers
structure; facets impose it.

This is where top-down structure re-enters the system, and it is the reason §8
can commit to bottom-up generation without that being a permanent bet. Facets
recorded at capture (§6) can later constrain grouping — restricting a run to one
facet, weighting shared facets in community detection, or generating parallel
hierarchies over the same shards along different dimensions. None of that
requires recapturing anything, because the tags are already on the shards. The
top-down option is kept open at negligible cost rather than exercised now.

At small scale — dozens of shards — neither is needed. Grouping by hand is faster
and more informative than automating it, and it defers this choice until the
corpus is large enough for the difference to matter.

**Known risk — instability under growth.** Purely bottom-up hierarchies are
unstable: adding one leaf later can change which cluster an existing shard
belongs to, silently altering what a higher-level shard used to mean.

**Mitigation.** Regeneration never overwrites. Each aggregation run is versioned
(new ID + timestamp; the previous version superseded but retained), so drift
becomes visible history rather than silent rewriting. This is field 5 doing its
job.

Top-down structure is deliberately absent in v0.1. If the bottom-up-only
approach produces chaos, that is a finding, and the versioned history will show
exactly how it happened.

---

## 9. Confidentiality and access

A confidentiality tier is assigned at capture. It controls two things: how much
fidelity is allowed into field 3, and how the access layer gates reaching the
real artifact.

Enforcement uses existing mechanisms rather than anything bespoke: separate
private repositories, OS file permissions, and Claude Code's `settings.json`
allow/deny rules plus `additionalDirectories`. "Signing off before entering a
confidential tract" means a session's permissions do not include that path until
granted.

Because raw content never enters the Hub's storage, compliance attaches to
exactly two checkpoints — the capture pipeline and the access layer — rather
than diffusing across the whole Hub.

Concrete tier definitions are still open (§11).

---

## 10. Visualization (deferred)

Target: a pan/zoom network view across abstraction layers; clicking a shard
reveals its full abstraction hierarchy as a table of contents.

Candidate: Obsidian Canvas (nested canvases, zoom, and the Advanced Canvas
plugin for zoom-based render breakpoints). The per-file markdown format in §4 is
chosen partly so that this remains cheap to reach.

Revisit once real shards exist.

---

## 11. Open questions

- **Organize axis.** PARA-style actionability vs. Zettelkasten-style emergent
  linking vs. a split by depth. Currently unresolved; bottom-up emergence (§8)
  is being tried first.
- **Express.** The fourth element of CODE (Capture → Organize → Distill →
  Express) has not been designed at all.
- **Confidentiality tiers.** Concrete levels undefined.
- **Embedding model.** Only needed once embeddings are introduced (§8); the
  initial link-based grouping requires none, so this is not urgent.
- **Facet vocabulary.** The facet mechanism is decided (§6, §8), but the actual
  set of tags is undefined and is expected to accrete from use rather than be
  designed upfront.

---

## 12. Decision log

Recorded so these are not re-litigated. Each entry is a choice made *against* a
real alternative.

**Leaves are external, not embedded.** Considered: including real artifacts in
the Hub so that representation-to-artifact transitions are seamless and sync is
automatic. Rejected because it duplicates the source of truth, breaks fractal
uniformity exactly at the leaf level (raw formats are heterogeneous; nodes are
uniform), and forces the whole Hub into the compliance envelope of its most
sensitive item. The seamlessness that motivated it is recovered through
low-friction access (§2) rather than through storage location.

**No separate "registry" concept.** Considered: a distinct registry of project
pointers alongside the shard network. Rejected as a violation of fractal
uniformity — a pointer entry is just a shard with a thin field 3.

**One file per shard, not one file per layer.** Considered: JSONL per layer, one
row per shard. Rejected because it breaks free per-shard git history, forces
whole-layer reads, and is invisible to graph tooling. Queryability is recovered
via a derived index (§4).

**Lazy sync, not continuous.** Considered: a background process keeping all
representations fresh. Rejected as a violation of constant entry cost; refresh
is triggered by access instead.

**Bottom-up only, for now.** Considered: imposing top-down categories to
stabilize the hierarchy. Deferred rather than rejected — trying the pure form
first, with versioning to make its failure legible.

**Layers are structural depth, not semantic abstraction.** Considered: keeping
each layer internally coherent in level of abstraction, so that already-condensed
artifacts (abstracts, summaries) would enter above L0. Rejected because it
replaces a mechanical, deterministic assignment with a continuous judgment call,
and because abstraction emerges upward anyway through synthesis (§5).

**Link-based grouping before embeddings.** Considered: embedding-based clustering
as the primary grouping mechanism. Deferred because it requires infrastructure
the system does not yet have, produces groupings that are hard to explain, and
would leave the explicit-links discipline mechanically unused (§8).

**Facets are the top-down lever, kept optional.** Considered: choosing between
bottom-up emergence and top-down categories. Resolved instead by making them
compose — optional tags at capture (§6) cost nothing, need no taxonomy decided in
advance, and can later constrain or reweight grouping (§8). Bottom-up generation
therefore stops being a bet that forecloses the alternative.

**Staleness strategy varies by address type; Hub history does not.** Considered:
making git the universal substrate for both. Rejected because it would silently
exclude artifacts on external drives, in non-versioned folders, or behind
connectors. The Hub's own history is always git; an artifact's staleness marker
is whatever its source cheaply exposes, and "unreachable" is tracked as its own
state rather than collapsed into "fresh" (§7).
