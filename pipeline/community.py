"""Community detection over the shard link graph.

Louvain, hand-rolled: no networkx, no igraph, matching the dependency
discipline the rest of pipeline/ already keeps (retrieval.py hand-rolls BM25,
extract.py leans only on tools macOS already ships). It is deterministic —
nodes are visited in sorted order, so the same graph always yields the same
partition, which matters because a partition that shifted between runs would
make founding_design.md §8's versioning unreadable: you could not tell a real
change in the corpus from a coin flip in the algorithm.

This module answers *which shards group together*, and deliberately nothing
about what a group means. That split is the same one retrieval.py draws for
capture: mechanical structure here, judgment downstream, in one bounded LLM
call per group that a human has already approved.

Modularity has a known resolution limit, and this Hub shows it plainly: no
single resolution both keeps a 12-report monthly series whole and separates
three small projects that share methodology. The resolution is therefore a
parameter of a *run*, not a constant to be tuned once — see synthesize.py,
where re-running at a different resolution and accepting different groups is
the intended workflow rather than a workaround.
"""

from collections import defaultdict


def canonical(edge_list: list[tuple]) -> list[tuple]:
    """Collapse a directed edge list into unique undirected edges.

    Links are stored one-directionally on the shard that declares them (§4),
    but a stated connection is a connection: for grouping, A→B and B→A are the
    same edge and are merged rather than double-counted.
    """
    agg: dict[tuple, float] = {}
    for u, v, w in edge_list:
        key = (u, v) if u <= v else (v, u)
        agg[key] = agg.get(key, 0.0) + w
    return [(a, b, w) for (a, b), w in agg.items()]


def _build(edges: list[tuple]):
    """Adjacency, weighted degree and total weight. Self-loops (which appear
    once communities are aggregated into super-nodes) count twice toward
    degree, as the modularity definition requires."""
    adj: dict[str, dict[str, float]] = {}
    deg: dict[str, float] = {}
    for u, v, w in edges:
        adj.setdefault(u, {}); adj.setdefault(v, {})
        deg.setdefault(u, 0.0); deg.setdefault(v, 0.0)
        if u == v:
            adj[u][u] = adj[u].get(u, 0.0) + w
            deg[u] += 2 * w
        else:
            adj[u][v] = adj[u].get(v, 0.0) + w
            adj[v][u] = adj[v].get(u, 0.0) + w
            deg[u] += w
            deg[v] += w
    return adj, deg, sum(deg.values()) / 2.0


def _one_level(adj, deg, m: float, resolution: float) -> dict[str, str]:
    """Local-moving phase: repeatedly move each node to the neighbouring
    community that most improves modularity, until nothing moves."""
    comm = {n: n for n in adj}
    tot = dict(deg)
    moved = True
    while moved:
        moved = False
        for n in sorted(adj):  # sorted, so the partition is reproducible
            own, k_n = comm[n], deg[n]
            links: dict[str, float] = defaultdict(float)
            for nb, w in adj[n].items():
                if nb != n:
                    links[comm[nb]] += w
            tot[own] -= k_n  # take n out before comparing where it should go
            best_c = own
            best_val = links.get(own, 0.0) - resolution * tot[own] * k_n / (2 * m)
            for c, w_in in links.items():
                if c == own:
                    continue
                val = w_in - resolution * tot.get(c, 0.0) * k_n / (2 * m)
                if val > best_val:
                    best_val, best_c = val, c
            tot[best_c] = tot.get(best_c, 0.0) + k_n
            comm[n] = best_c
            if best_c != own:
                moved = True
    return comm


def _aggregate(edges: list[tuple], comm: dict[str, str]) -> list[tuple]:
    """Collapse each community into one node for the next level. Edges that
    were internal to a community become that node's self-loop."""
    agg: dict[tuple, float] = {}
    for u, v, w in edges:
        cu, cv = comm[u], comm[v]
        key = (cu, cv) if cu <= cv else (cv, cu)
        agg[key] = agg.get(key, 0.0) + w
    return [(a, b, w) for (a, b), w in agg.items()]


def modularity(edges: list[tuple], comm: dict[str, str]) -> float:
    """Q for a partition — reported so a run's quality is visible rather than
    assumed. Roughly: how much more edge weight falls inside communities than
    chance would put there."""
    _, deg, m = _build(edges)
    if m == 0:
        return 0.0
    tot: dict[str, float] = defaultdict(float)
    internal: dict[str, float] = defaultdict(float)
    for n, d in deg.items():
        tot[comm[n]] += d
    for u, v, w in edges:
        if comm[u] == comm[v]:
            internal[comm[u]] += w
    return sum(internal[c] / m - (tot[c] / (2 * m)) ** 2 for c in tot)


def louvain(nodes: list[str], edge_list: list[tuple], resolution: float = 1.2) -> dict[str, str]:
    """Partition nodes into communities. Returns node id -> community id.

    Isolated nodes (no links at all) come back in a community of their own,
    which synthesize.py then drops on the size floor — an unlinked shard has
    nothing to be synthesized *with*.
    """
    edges = canonical(edge_list)
    node_to_comm = {n: n for n in nodes}
    level_edges = edges
    while True:
        adj, deg, m = _build(level_edges)
        if m == 0:
            break
        comm = _one_level(adj, deg, m, resolution)
        before, after = len(adj), len(set(comm.values()))
        node_to_comm = {n: comm.get(node_to_comm[n], node_to_comm[n]) for n in nodes}
        if after == before:  # nothing merged; further levels cannot help
            break
        level_edges = _aggregate(level_edges, comm)
    return node_to_comm


def analyze(edge_list: list[tuple], comm: dict[str, str]) -> dict:
    """Per-community and per-boundary statistics used to judge a partition.

    `cohesion` is internal / (internal + external) for a community: the share
    of its edges that stay home. `bundles` maps each community pair to the
    number of edges crossing between them, and `bridge_ratio` divides that by
    the smaller community's internal edge count.

    Both are ratios rather than absolute counts on purpose, so the thresholds
    that use them keep meaning as the Hub grows: "half this group's edges
    leave it" and "this boundary carries more traffic than the interior it
    borders" are scale-free statements, where "more than 20 edges" is not.
    """
    edges = canonical(edge_list)
    internal: dict[str, int] = defaultdict(int)
    external: dict[str, int] = defaultdict(int)
    bundles: dict[tuple, int] = defaultdict(int)
    for u, v, _w in edges:
        cu, cv = comm[u], comm[v]
        if cu == cv:
            internal[cu] += 1
        else:
            external[cu] += 1
            external[cv] += 1
            bundles[(cu, cv) if cu <= cv else (cv, cu)] += 1

    cohesion = {}
    for c in set(comm.values()):
        total = internal[c] + external[c]
        cohesion[c] = internal[c] / total if total else 0.0

    bridge_ratio = {}
    for (a, b), n in bundles.items():
        denom = min(internal[a], internal[b])
        bridge_ratio[(a, b)] = n / denom if denom else float("inf")

    return {
        "internal": dict(internal),
        "external": dict(external),
        "cohesion": cohesion,
        "bundles": dict(bundles),
        "bridge_ratio": bridge_ratio,
        "modularity": modularity(edges, comm),
    }
