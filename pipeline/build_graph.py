#!/usr/bin/env python3
"""Build viz/index.html — a local, offline-except-for-the-CDN graph view of
the Hub.

    python3 pipeline/build_graph.py

Deliberately local, not published. Most shards are confidentiality tier
`confidential`, and a shard's summary was written at the fidelity appropriate
to *that* tier (§9) — appropriate to stay inside this repo's existing trust
boundary (a private git remote, same as every shard file already), not to
leave it for a hosted service. This script only ever writes to disk; nothing
it produces is sent anywhere.

The page is a single self-contained HTML file with the graph data embedded
inline as a JSON script block, so it opens by double-click with no local
server and no fetch()/CORS concerns. Regenerate and reopen after every batch
of captures — this is a build artifact like index/manifest.json, not a
source of truth, and is gitignored for the same reason.

    python3 pipeline/build_graph.py --public SPEC.json --out PATH

writes a *redacted* copy for showing the Hub to people outside it. Redaction
happens in the embedded data, not on screen — the page source is as public as
the page — and it fails closed: a shard keeps its own title and summary only
if every project it belongs to is listed as public in the spec; every other
shard must have its replacement text spelled out there, or the build stops.
A link keeps its stated reason only when both ends kept their own text.
Addresses and staleness markers are never embedded in either mode.
"""

import argparse
import json
import sys
from pathlib import Path

import shard

OUT = shard.REPO / "viz" / "index.html"

# The dataviz skill's categorical palette, first 3 slots — the only prefix
# validated for an *all-pairs* adjacency case (references/palette.md). A
# force-directed layout can put any two nodes next to each other depending on
# the simulation, so this graph is all-pairs, not the fixed adjacent-pairs
# case (stacked bars, ordered lines) the full 8-hue palette is validated for.
# A 4th project does not get a 4th hue: it falls back to the muted neutral
# below, same treatment the skill specifies for an over-cap category ("fold to
# Other") — the label stays true (its real project name), only the color
# is shared.
PALETTE = [
    {"light": "#2a78d6", "dark": "#3987e5"},  # blue
    {"light": "#eb6834", "dark": "#d95926"},  # orange
    {"light": "#1baf7a", "dark": "#199e70"},  # aqua
]
NEUTRAL = {"light": "#898781", "dark": "#898781"}  # muted (axis/labels) token


def project_of(facets: list[str]) -> str:
    tag = next((f for f in facets if f.startswith("project:")), None)
    return tag.removeprefix("project:") if tag else "uncategorized"


# A project's color, once assigned, is permanent — tracked here rather than
# recomputed from current shard counts every render. Ranking by count instead
# would silently recolor a project every time some *other* project grew past
# it: individually "correct" each render, but disorienting across them, which
# is the whole reason this file exists rather than a plain in-function
# ranking. Tracked in git (unlike index/ and viz/, which are pure build
# artifacts): this file *is* the fact of which project owns which color, not
# something re-derivable from the shards alone.
COLOR_MAP_PATH = shard.REPO / "pipeline" / "project_colors.json"


def _assign_colors(by_project: dict[str, int]) -> dict[str, int]:
    """Project name -> palette slot index (see PALETTE above).

    An established project keeps its slot even if a newcomer would now
    outrank it on shard count — a slot is only ever added to the map, never
    reassigned. When a genuinely new project needs a slot and more than one
    is free, the larger project gets first pick; ties resolve arbitrarily
    but stably (dict order). Once every slot is taken, later projects fall
    back to NEUTRAL in `build()`, same as any project not in this map.
    """
    assigned: dict[str, int] = {}
    if COLOR_MAP_PATH.exists():
        assigned = json.loads(COLOR_MAP_PATH.read_text())

    free = [i for i in range(len(PALETTE)) if i not in assigned.values()]
    newcomers = sorted((p for p in by_project if p not in assigned),
                       key=lambda p: -by_project[p])
    for project, slot in zip(newcomers, free):
        assigned[project] = slot

    COLOR_MAP_PATH.write_text(json.dumps(assigned, indent=2, sort_keys=True) + "\n")
    return assigned


def build() -> dict:
    """Every layer at once, plus the `children` edges that connect them.

    The viewer stacks layers as planes in 3D and flies the camera down onto one
    to read it flat, so it needs the whole Hub rather than a slice: a layer's
    meaning is partly *what sits above it*.
    """
    manifest = shard.build_index()  # also refreshes index/manifest.json
    shards = manifest["shards"]

    by_project: dict[str, int] = {}
    for s in shards:
        proj = project_of(s["facets"])
        by_project[proj] = by_project.get(proj, 0) + 1
    color_slots = _assign_colors(by_project)
    color_for = {p: PALETTE[i] for p, i in color_slots.items()}

    ids = {s["id"] for s in shards}
    nodes, edges = [], []
    for s in shards:
        proj = project_of(s["facets"])
        degree = len(s["links"]) + len(s["linked_by"]) + len(s["children"])
        nodes.append({
            "id": s["id"],
            "title": s["title"],
            "layer": s["layer"],
            "project": proj,
            "facets": s["facets"],
            "confidentiality": s["confidentiality"],
            "address_type": s["address_type"],
            "last_verified": s["last_verified"],
            "summary": s["summary"],
            "children": s["children"],
            "superseded_by": s["superseded_by"],
            "degree": degree,
            "color": color_for.get(proj, NEUTRAL),
        })
        for link in s["links"]:
            if link["id"] in ids:  # defensive; capture already guarantees this
                edges.append({"source": s["id"], "target": link["id"],
                              "why": link["why"], "kind": "link"})
        # Vertical edges. A child edge is structural rather than semantic — it
        # says "this stands for that", not "these are related" — so the viewer
        # draws it differently and the two are never mixed in one count.
        for child in s["children"]:
            if child in ids:
                edges.append({"source": s["id"], "target": child,
                              "why": "", "kind": "child"})

    # The legend lists biggest-first for scanning — a display choice, independent
    # of color, which stays pinned to first-seen order above.
    legend = [{"project": p, "count": by_project[p], "color": color_for.get(p, NEUTRAL)}
              for p in sorted(by_project, key=lambda p: -by_project[p])]

    layers = sorted({s["layer"] for s in shards})
    return {
        "generated": manifest["generated"],
        "layers": layers,
        "layer_counts": {str(l): sum(1 for s in shards if s["layer"] == l) for l in layers},
        "nodes": nodes,
        "edges": edges,
        "legend": legend,
    }


def redact(data: dict, spec: dict) -> dict:
    """The graph data with everything not explicitly cleared replaced.

    Built as a whitelist: each node is rebuilt from named fields rather than
    copied and pruned, so a field added to `build()` later stays private until
    someone decides otherwise here.
    """
    public = set(spec.get("public_projects", []))
    names = spec.get("project_names", {})
    overrides = spec.get("shards", {})

    def display(p: str) -> str:
        return names.get(p, p if p in public else "Project ?")

    kept = set()
    nodes = []
    for n in data["nodes"]:
        projects = [f.removeprefix("project:") for f in n["facets"] if f.startswith("project:")]
        own_text = bool(projects) and all(p in public for p in projects)
        o = overrides.get(n["id"])
        if not own_text and o is None:
            raise SystemExit(f"{n['id']} ({', '.join(projects) or 'no project'}) is not public "
                             f"and has no replacement text in the spec — refusing to build.")
        if own_text and o is None:
            kept.add(n["id"])
        o = o or {}
        nodes.append({
            "id": n["id"],
            "title": o.get("title", n["title"] if own_text else ""),
            "layer": n["layer"],
            "project": display(n["project"]),
            "facets": [("project:" + display(f.removeprefix("project:")))
                       if f.startswith("project:") else f
                       for f in n["facets"] if f.startswith("project:") or f in ("work", "personal")],
            "confidentiality": n["confidentiality"],
            "address_type": n["address_type"],
            "last_verified": n["last_verified"],
            "summary": o.get("summary", n["summary"] if own_text else ""),
            "children": n["children"],
            "superseded_by": n["superseded_by"],
            "degree": n["degree"],
            "color": n["color"],
            "redacted": n["id"] not in kept,
            "kind": o.get("kind", ""),
        })

    edges = [{"source": e["source"], "target": e["target"], "kind": e["kind"],
              "why": e["why"] if e["source"] in kept and e["target"] in kept else ""}
             for e in data["edges"]]
    legend = [dict(l, project=display(l["project"])) for l in data["legend"]]
    return dict(data, nodes=nodes, edges=edges, legend=legend, note=spec.get("note", ""))


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Knowledge Hub</title>
<style>
  :root {
    color-scheme: light;
    --surface-1:      #fcfcfb;
    --surface-2:      #f2f1ee;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #898781;
    --edge:           #898781;
    --child:          #6d6b66;
    --border:         #e3e2dd;
    --bg-3d:          #f4f3f0;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface-1:      #1a1a19;
      --surface-2:      #232322;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #898781;
      --edge:           #8d8b84;
      --child:          #a8a59c;
      --border:         #333331;
      --bg-3d:          #141413;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1:      #1a1a19;
    --surface-2:      #232322;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --edge:           #8d8b84;
    --child:          #a8a59c;
    --border:         #333331;
    --bg-3d:          #141413;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg-3d); }
  body {
    font: 13px/1.45 -apple-system, "Helvetica Neue", Arial, sans-serif;
    color: var(--text-primary); overflow: hidden;
  }
  #graph { position: absolute; inset: 0; }
  .scene-fade { transition: opacity .35s ease; }

  .panel {
    position: absolute; z-index: 5; background: var(--surface-1);
    border: 1px solid var(--border); border-radius: 10px;
    box-shadow: 0 4px 20px rgba(0,0,0,.12);
  }
  #legend { top: 16px; left: 16px; padding: 12px 14px; min-width: 200px; }
  #legend h1 { font-size: 13px; font-weight: 600; margin: 0 0 8px; }
  #legend .row {
    display: flex; align-items: center; gap: 8px; padding: 3px 0;
    cursor: pointer; user-select: none;
  }
  #legend .row.dimmed { opacity: .35; }
  #legend .swatch { width: 10px; height: 10px; border-radius: 50%; flex: none; }
  #legend .name { flex: 1; color: var(--text-secondary); }
  #legend .count { color: var(--text-muted); font-variant-numeric: tabular-nums; }
  #legend .hint {
    margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border);
    color: var(--text-muted); font-size: 11px;
  }

  #stats { bottom: 16px; left: 16px; padding: 8px 12px; color: var(--text-muted); font-size: 11px; }

  #controls { top: 16px; right: 16px; padding: 4px; display: flex; gap: 2px; align-items: stretch; }
  #controls button {
    border: none; background: transparent; color: var(--text-secondary);
    font-size: 12px; padding: 6px 10px; border-radius: 7px; cursor: pointer;
  }
  #controls button:hover { background: var(--surface-2); }
  #controls button.active { background: var(--surface-2); color: var(--text-primary); font-weight: 600; }
  #controls .sep { width: 1px; align-self: stretch; background: var(--border); margin: 2px 4px; }
  #labels-toggle.off { color: var(--text-primary); font-weight: 600; }

  #layers { bottom: 16px; right: 16px; padding: 4px; display: flex; gap: 2px; }
  #layers button {
    border: none; background: transparent; color: var(--text-secondary);
    font-size: 12px; padding: 6px 12px; border-radius: 7px; cursor: pointer;
  }
  #layers button:hover { background: var(--surface-2); }
  #layers button.active { background: var(--surface-2); color: var(--text-primary); font-weight: 600; }
  #layers .n { color: var(--text-muted); font-size: 10.5px; margin-left: 5px; }

  #inspector {
    top: 16px; right: 16px; bottom: 76px; width: 350px;
    padding: 18px; overflow-y: auto; display: none;
  }
  #inspector.open { display: block; }
  #inspector .close {
    float: right; border: none; background: none; color: var(--text-muted);
    font-size: 16px; cursor: pointer; line-height: 1;
  }
  #inspector h2 { font-size: 15px; margin: 0 24px 10px 0; line-height: 1.3; }
  #inspector .pills { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
  #inspector .pill {
    font-size: 10.5px; padding: 2px 8px; border-radius: 100px;
    background: var(--surface-2); color: var(--text-secondary); border: 1px solid var(--border);
  }
  #inspector .pill.tier-strictly_confidential { color: #e34948; border-color: #e34948; }
  #inspector .pill.tier-confidential { color: #eda100; border-color: #eda100; }
  #inspector .pill.stale { color: #e34948; border-color: #e34948; }
  #inspector section { margin-bottom: 16px; }
  #inspector section h3 {
    font-size: 10.5px; text-transform: uppercase; letter-spacing: .04em;
    color: var(--text-muted); margin: 0 0 6px; font-weight: 600;
  }
  #inspector p { margin: 0; color: var(--text-secondary); white-space: pre-wrap; }
  #inspector p.withheld, #inspector .link-why.withheld { font-style: italic; color: var(--text-muted); }
  #inspector .pill.redacted { border-style: dashed; }
  #inspector .pill.kind { color: var(--text-primary); font-weight: 600; }
  #legend .note { max-width: 230px; }
  #inspector .link-item { padding: 8px 0; border-top: 1px solid var(--border); cursor: pointer; }
  #inspector .link-item:hover .link-title { color: var(--text-primary); }
  #inspector .link-title { font-weight: 600; font-size: 12px; display: block; margin-bottom: 2px; }
  #inspector .link-why { color: var(--text-muted); font-size: 11.5px; }
  #inspector .meta-row { display: flex; justify-content: space-between; color: var(--text-secondary); padding: 2px 0; }
  #inspector .meta-row span:last-child { color: var(--text-muted); }

  #hint {
    position: absolute; bottom: 16px; left: 50%; transform: translateX(-50%);
    z-index: 4; color: var(--text-muted); font-size: 11px; text-align: center;
    pointer-events: none;
  }
  .scene-tooltip { font-size: 11.5px !important; }
</style>
</head>
<body>
<div id="graph"></div>

<div id="legend" class="panel"></div>
<div id="stats" class="panel"></div>
<div id="controls" class="panel">
  <button id="theme-light">Light</button>
  <button id="theme-dark">Dark</button>
  <button id="theme-auto" class="active">Auto</button>
  <span class="sep"></span>
  <button id="labels-toggle" title="Off: titles never appear on hover — only a deliberate click reveals one, in the panel">Labels: Auto</button>
</div>
<div id="layers" class="panel"></div>
<div id="inspector" class="panel"></div>
<div id="hint">drag to orbit · scroll to zoom · click a shard to inspect</div>

<script id="graph-data" type="application/json">__GRAPH_JSON__</script>
<script src="https://cdn.jsdelivr.net/npm/3d-force-graph@1.73.4/dist/3d-force-graph.min.js"></script>
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("graph-data").textContent);
  var byId = {}; DATA.nodes.forEach(function (n) { byId[n.id] = n; });

  // Layers stack along Y like floors of a building. Pinning fy per layer and
  // letting the force settle x/z is what makes the whole thing work: each
  // layer lays itself out as a flat graph, while a parent is pulled toward the
  // centroid of its children by its child edges, so it comes to rest directly
  // above what it stands for.
  var LAYER_GAP = 190;
  var maxLayer = Math.max.apply(null, DATA.layers);
  function layerY(l) { return (l - maxLayer / 2) * LAYER_GAP; }

  // ---- theme -------------------------------------------------------------
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function isDark() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t === "dark") return true;
    if (t === "light") return false;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function applyTheme(mode) {
    var root = document.documentElement;
    if (mode === "auto") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", mode);
    ["light", "dark", "auto"].forEach(function (m) {
      document.getElementById("theme-" + m).classList.toggle("active", m === mode);
    });
    try { localStorage.setItem("kh-theme", mode); } catch (e) {}
    repaint();
  }
  var savedTheme = "auto";
  try { savedTheme = localStorage.getItem("kh-theme") || "auto"; } catch (e) {}

  // ---- graph -------------------------------------------------------------
  var nodes = DATA.nodes.map(function (n) {
    return Object.assign({}, n, { fy: layerY(n.layer) });
  });
  var links = DATA.edges.map(function (e) { return Object.assign({}, e); });

  var maxDeg = Math.max(1, Math.max.apply(null, nodes.map(function (n) { return n.degree; })));
  function nodeSize(n) { return 2 + 5 * (Math.log(n.degree + 1) / Math.log(maxDeg + 1)); }

  var activeProject = null;
  var focusLayer = null;      // null = overview across all layers
  var selectedId = null;
  var neighbours = null;      // ids highlighted by the current selection

  function nodeVisible(n) {
    if (focusLayer !== null && n.layer !== focusLayer) return false;
    if (activeProject && n.project !== activeProject) return false;
    return true;
  }
  function linkVisible(l) {
    var s = typeof l.source === "object" ? l.source : byId[l.source];
    var t = typeof l.target === "object" ? l.target : byId[l.target];
    if (!s || !t) return false;
    // In a single-layer view the vertical edges lead off-screen, so they are
    // hidden rather than left dangling into empty space.
    if (focusLayer !== null && (s.layer !== focusLayer || t.layer !== focusLayer)) return false;
    return nodeVisible(s) && nodeVisible(t);
  }

  var Graph = ForceGraph3D()(document.getElementById("graph"))
    .graphData({ nodes: nodes, links: links })
    .nodeId("id")
    .nodeVal(nodeSize)
    .nodeColor(function (n) {
      if (neighbours && !neighbours[n.id]) return isDark() ? "#3a3a38" : "#d8d7d2";
      return isDark() ? n.color.dark : n.color.light;
    })
    .nodeOpacity(0.95)
    .nodeVisibility(nodeVisible)
    .nodeLabel(function (n) { return labelsEnabled ? n.title : ""; })
    .linkVisibility(linkVisible)
    .linkColor(function (l) {
      if (neighbours) {
        var s = l.source.id || l.source, t = l.target.id || l.target;
        if (!neighbours[s] || !neighbours[t]) return isDark() ? "#2a2a28" : "#e8e7e2";
      }
      return l.kind === "child" ? cssVar("--child") : cssVar("--edge");
    })
    .linkWidth(function (l) { return l.kind === "child" ? 0.7 : 0.4; })
    .linkOpacity(0.5)
    .linkDirectionalArrowLength(function (l) { return l.kind === "child" ? 2.5 : 0; })
    .linkDirectionalArrowColor(function () { return cssVar("--child"); })
    .onNodeClick(function (n) { select(n.id); })
    .onBackgroundClick(function () { clearSelection(); })
    .showNavInfo(false)
    .backgroundColor(cssVar("--bg-3d"));

  Graph.d3Force("charge").strength(-90);
  Graph.d3Force("link").distance(function (l) { return l.kind === "child" ? 60 : 34; });

  function repaint() {
    Graph.backgroundColor(cssVar("--bg-3d"));
    Graph.nodeColor(Graph.nodeColor()).linkColor(Graph.linkColor());
    renderLegend();
  }

  // ---- camera: overview <-> a single layer --------------------------------
  function layerExtent(l) {
    var pts = nodes.filter(function (n) { return n.layer === l && n.x !== undefined; });
    if (!pts.length) return { x: 0, z: 0, radius: 120 };
    var cx = pts.reduce(function (a, n) { return a + n.x; }, 0) / pts.length;
    var cz = pts.reduce(function (a, n) { return a + n.z; }, 0) / pts.length;
    var radius = Math.max.apply(null, pts.map(function (n) {
      return Math.hypot(n.x - cx, n.z - cz);
    }));
    return { x: cx, z: cz, radius: Math.max(radius, 60) };
  }
  // Camera distance is computed from the layer's real spread and the camera's
  // own field of view, rather than a tuned multiplier. zoomToFit() was tried
  // first and abandoned: it re-derives the camera direction from the orbit
  // target, which fought the top-down angle being set here and left the layer
  // viewed edge-on.
  function fitDistance(radius) {
    var cam = Graph.camera();
    var fov = (cam && cam.fov ? cam.fov : 75) * Math.PI / 180;
    return (radius / Math.tan(fov / 2)) * 1.25;  // 25% margin
  }
  function flyToOverview() {
    focusLayer = null;
    refreshVisibility();
    // Framed from where the nodes actually are, once they are anywhere: the
    // layout keeps expanding for a while after load, and a camera placed for
    // the initial clump ends up inside the settled graph.
    var span = (DATA.layers.length || 1) * LAYER_GAP;
    var placed = nodes.filter(function (n) { return n.x !== undefined; });
    var r = placed.length ? Math.max.apply(null, placed.map(function (n) { return Math.hypot(n.x, n.y, n.z); })) : 0;
    var dist = Math.max(span * 1.1, fitDistance(r));
    var zoom = Number(new URLSearchParams(window.location.search).get("zoom")) || 1;
    Graph.cameraPosition({ x: 0, y: dist * 0.5 / zoom, z: dist * 0.866 / zoom }, { x: 0, y: 0, z: 0 }, 800);
  }
  function flyToLayer(l) {
    focusLayer = l;
    refreshVisibility();
    var c = layerExtent(l);
    // Not exactly straight down: a hair of z offset keeps the camera's up-vector
    // well defined, which a perfectly vertical look-down leaves ambiguous.
    Graph.cameraPosition({ x: c.x, y: layerY(l) + fitDistance(c.radius), z: c.z + 1.5 },
                         { x: c.x, y: layerY(l), z: c.z }, 800);
  }
  function refreshVisibility() {
    Graph.nodeVisibility(nodeVisible).linkVisibility(linkVisible);
    renderLayers();
    renderLegend();
    document.getElementById("hint").textContent = focusLayer === null
      ? "drag to orbit · scroll to zoom · click a shard to inspect"
      : "layer " + focusLayer + " seen flat · click Overview to pull back out";
  }

  function renderLayers() {
    var el = document.getElementById("layers");
    var html = '<button data-layer="overview"' + (focusLayer === null ? ' class="active"' : '') +
               '>Overview</button>';
    DATA.layers.slice().reverse().forEach(function (l) {
      html += '<button data-layer="' + l + '"' + (focusLayer === l ? ' class="active"' : '') +
              '>L' + l + '<span class="n">' + (DATA.layer_counts[String(l)] || 0) + '</span></button>';
    });
    el.innerHTML = html;
    el.querySelectorAll("button").forEach(function (b) {
      b.addEventListener("click", function () {
        var v = b.getAttribute("data-layer");
        if (v === "overview") flyToOverview(); else flyToLayer(Number(v));
      });
    });
  }

  // ---- selection ----------------------------------------------------------
  function select(id) {
    selectedId = id;
    neighbours = {};
    neighbours[id] = true;
    DATA.edges.forEach(function (e) {
      if (e.source === id) neighbours[e.target] = true;
      if (e.target === id) neighbours[e.source] = true;
    });
    openInspector(id);
    Graph.nodeColor(Graph.nodeColor()).linkColor(Graph.linkColor());
  }
  function clearSelection() {
    selectedId = null; neighbours = null;
    document.getElementById("inspector").classList.remove("open");
    Graph.nodeColor(Graph.nodeColor()).linkColor(Graph.linkColor());
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function metaRow(k, v) {
    return '<div class="meta-row"><span>' + k + "</span><span>" + escapeHtml(String(v)) + "</span></div>";
  }

  function openInspector(id) {
    var n = byId[id];
    if (!n) return;
    var out = DATA.edges.filter(function (e) { return e.source === id && e.kind === "link"; });
    var inc = DATA.edges.filter(function (e) { return e.target === id && e.kind === "link"; });
    var kids = n.children || [];
    var parents = DATA.edges.filter(function (e) { return e.target === id && e.kind === "child"; })
                            .map(function (e) { return e.source; });

    var html = '<button class="close" id="inspector-close">×</button>';
    html += "<h2>" + escapeHtml(n.title) + "</h2>";
    html += '<div class="pills">';
    html += '<span class="pill">L' + n.layer + "</span>";
    html += '<span class="pill">' + escapeHtml(n.project) + "</span>";
    html += '<span class="pill tier-' + n.confidentiality + '">' + n.confidentiality + "</span>";
    if (n.superseded_by) html += '<span class="pill stale">superseded</span>';
    if (n.kind) html += '<span class="pill kind">' + escapeHtml(n.kind) + "</span>";
    if (n.redacted) html += '<span class="pill redacted">redacted</span>';
    html += "</div>";
    if (n.summary) {
      html += "<section><h3>Summary" + (n.redacted ? " · redacted paraphrase" : "") + "</h3><p>" +
              escapeHtml(n.summary) + "</p></section>";
    } else {
      html += '<section><h3>Summary</h3><p class="withheld">Withheld: this shard is ' +
              escapeHtml(n.confidentiality) + ".</p></section>";
    }

    function listOf(heading, items) {
      if (!items.length) return "";
      var s = "<section><h3>" + heading + " (" + items.length + ")</h3>";
      items.forEach(function (it) {
        var t = byId[it.id];
        s += '<div class="link-item" data-goto="' + it.id + '">' +
             '<span class="link-title">' + (it.prefix || "") + escapeHtml(t ? t.title : it.id) + "</span>" +
             (it.why ? '<span class="link-why">' + escapeHtml(it.why) + "</span>"
                     : (it.link && DATA.note ? '<span class="link-why withheld">reason withheld</span>' : "")) +
             "</div>";
      });
      return s + "</section>";
    }
    html += listOf("Stands for", kids.map(function (c) { return { id: c, prefix: "↓ " }; }));
    html += listOf("Part of", parents.map(function (p) { return { id: p, prefix: "↑ " }; }));
    html += listOf("Links out", out.map(function (e) { return { id: e.target, prefix: "→ ", why: e.why, link: true }; }));
    html += listOf("Linked from", inc.map(function (e) { return { id: e.source, prefix: "← ", why: e.why, link: true }; }));

    html += "<section><h3>Metadata</h3>";
    html += metaRow("facets", (n.facets || []).join(", ") || "none");
    html += metaRow("address type", n.address_type);
    html += metaRow("last verified", n.last_verified);
    html += "</section>";

    var insp = document.getElementById("inspector");
    insp.innerHTML = html;
    insp.classList.add("open");
    document.getElementById("inspector-close").addEventListener("click", clearSelection);
    insp.querySelectorAll(".link-item").forEach(function (item) {
      item.addEventListener("click", function () {
        var target = byId[item.getAttribute("data-goto")];
        if (!target) return;
        // Following a child or parent edge moves between layers, so the view
        // follows too rather than selecting something currently invisible.
        if (focusLayer !== null && target.layer !== focusLayer) flyToLayer(target.layer);
        select(target.id);
      });
    });
  }

  // ---- labels (ambient exposure gate) -------------------------------------
  var labelsEnabled = true;
  try { labelsEnabled = localStorage.getItem("kh-labels") !== "off"; } catch (e) {}
  function applyLabelsToggle() {
    var btn = document.getElementById("labels-toggle");
    btn.textContent = "Labels: " + (labelsEnabled ? "Auto" : "Off");
    btn.classList.toggle("off", !labelsEnabled);
    try { localStorage.setItem("kh-labels", labelsEnabled ? "auto" : "off"); } catch (e) {}
    Graph.nodeLabel(Graph.nodeLabel());
    renderLegend();
  }

  // ---- legend --------------------------------------------------------------
  var REDACTED_NAME = "••••••";
  function renderLegend() {
    var el = document.getElementById("legend");
    var html = "<h1>Knowledge Hub</h1>";
    DATA.legend.forEach(function (l) {
      var dim = activeProject && activeProject !== l.project ? " dimmed" : "";
      var label = labelsEnabled ? l.project : REDACTED_NAME;
      html += '<div class="row' + dim + '" data-project="' + l.project + '">' +
        '<span class="swatch" style="background:' + (isDark() ? l.color.dark : l.color.light) + '"></span>' +
        '<span class="name">' + label + '</span><span class="count">' + l.count + '</span></div>';
    });
    html += '<div class="hint">Click a project to isolate it.</div>';
    if (DATA.note) html += '<div class="hint note">' + escapeHtml(DATA.note) + "</div>";
    el.innerHTML = html;
    el.querySelectorAll(".row").forEach(function (row) {
      row.addEventListener("click", function () {
        var p = row.getAttribute("data-project");
        activeProject = activeProject === p ? null : p;
        refreshVisibility();
      });
    });
  }

  // ---- init ----------------------------------------------------------------
  ["light", "dark", "auto"].forEach(function (m) {
    document.getElementById("theme-" + m).addEventListener("click", function () { applyTheme(m); });
  });
  document.getElementById("labels-toggle").addEventListener("click", function () {
    labelsEnabled = !labelsEnabled;
    applyLabelsToggle();
  });
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      if (!document.documentElement.getAttribute("data-theme")) repaint();
    });
  }
  document.getElementById("stats").innerHTML =
    DATA.nodes.length + " shards &middot; " +
    DATA.edges.filter(function (e) { return e.kind === "link"; }).length + " links &middot; " +
    DATA.edges.filter(function (e) { return e.kind === "child"; }).length + " children &middot; generated " +
    DATA.generated;

  // The canvas follows the window rather than keeping its size at load.
  function fitCanvas() { Graph.width(window.innerWidth).height(window.innerHeight); }
  window.addEventListener("resize", fitCanvas);
  fitCanvas();

  // URL parameters pin a view, so a figure can be regenerated exactly:
  // ?theme=light&labels=off&layer=1&select=<id>&zoom=1.4
  var params = new URLSearchParams(window.location.search);
  if (params.get("theme")) savedTheme = params.get("theme");
  if (params.get("labels")) labelsEnabled = params.get("labels") !== "off";
  applyTheme(savedTheme);
  applyLabelsToggle();
  renderLayers();
  flyToOverview();
  if (params.get("select") && byId[params.get("select")]) select(params.get("select"));
  // Once the layout first settles, frame it properly: a pinned layer
  // (?layer=) or, by default, the overview (see flyToOverview). One handler,
  // since the graph keeps only one.
  var settled = false;
  var pinnedLayer = params.get("layer");
  Graph.onEngineStop(function () {
    if (settled) return;
    settled = true;
    if (pinnedLayer !== null && pinnedLayer !== "overview") flyToLayer(Number(pinnedLayer));
    else if (focusLayer === null) flyToOverview();
  });
})();
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Render the Hub as a local, offline graph view.")
    ap.add_argument("--public", metavar="SPEC", type=Path,
                    help="Write a redacted copy, per the JSON spec (see module docstring).")
    ap.add_argument("--out", type=Path, help="Output path (required with --public).")
    args = ap.parse_args()
    if args.public and not args.out:
        ap.error("--public needs an explicit --out, so a redacted page never lands in viz/ by accident")

    data = build()
    if args.public:
        data = redact(data, json.loads(args.public.read_text()))
    out = (args.out or OUT).resolve()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.replace("__GRAPH_JSON__", payload)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    kinds = {k: sum(1 for e in data["edges"] if e["kind"] == k) for k in ("link", "child")}
    print(f"wrote {out}  ({len(data['nodes'])} nodes across "
          f"{len(data['layers'])} layer(s), {kinds['link']} links, {kinds['child']} children)")
    print(f"open it directly: open {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
