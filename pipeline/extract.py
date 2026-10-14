"""Turn a real artifact into plain text the capture stage can read.

founding_design.md §6 held multi-format support back until the schema had
survived contact with real files. It has, so this is that expansion — kept
deliberately thin: a table of extension → extractor, nothing configurable.

The extracted text is never stored. It exists only to be read once, inside the
capture call, and the shard that results points at the original artifact by
address. Pointer, not content (§3) applies to every format equally.

Extraction relies only on tools macOS already provides — `pdftotext` from
poppler and `textutil` — so capture gains formats without gaining dependencies.

Refusing is a feature here. §6 makes "enough content to generate a title and
summary" part of what capturable *means*, so an image-only scan is not a
capture that failed; it is an artifact that is not yet capturable, and saying so
is more useful than storing a shard that represents nothing.
"""

import ast
import shutil
import subprocess
import tokenize
import io
from pathlib import Path

# Below this many non-whitespace characters, there is not enough to represent.
# Image-only PDFs land near zero; a genuinely tiny note is also better left
# uncaptured than summarised into something more substantial than it is.
MIN_CHARS = 200


class Unsupported(Exception):
    """The format has no extractor."""


class NotEnoughText(Exception):
    """The file parsed, but yielded too little text to represent."""


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _pdftotext(path: Path) -> str:
    if not shutil.which("pdftotext"):
        raise Unsupported("pdftotext is not installed (brew install poppler)")
    # -layout keeps columns and tables readable, which matters more than prose
    # tidiness for invoices, budgets and settlement tables.
    proc = subprocess.run(["pdftotext", "-q", "-layout", str(path), "-"],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise Unsupported(f"pdftotext failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def _textutil(path: Path) -> str:
    if not shutil.which("textutil"):
        raise Unsupported("textutil is unavailable (macOS only)")
    proc = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise Unsupported(f"textutil failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def _python_structure(path: Path) -> str:
    """Docstrings, comments and bare signatures — never a function body.

    Default extraction for `.py`. What a shard needs to represent is why the
    code exists, not how it is written: `--full-source` on capture.py is the
    escape hatch for the rarer case where the implementation itself is the
    point. Checked against this repo's own pipeline/ before adoption: module
    docstrings ran 12-22 lines per file while inline comments ran 19-44 lines
    *in the same files* — more of the reasoning already lived outside the
    docstring than inside it, so a docstring-only cut would have kept the
    stated purpose and thrown away the harder-won half, the specific "why"
    argued next to the code it justifies.

    Comments are gathered with `tokenize`, not regex or `ast` — comments are
    not part of the AST at all, and a `#` inside a string literal (a hex color
    in this file's own build_graph.py, for instance) must not be mistaken for
    one; the tokenizer already gets exactly that distinction right.

    Each comment is attached to its innermost enclosing function or class, so
    a comment inside `select_candidates` is not shown as if it explained the
    whole module. Nesting goes to any depth: a nested `def` (this codebase
    has exactly one) is walked and shown indented under its parent rather than
    silently discarded along with the body it lives in.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise Unsupported(f"could not parse as Python ({exc}) — "
                          f"try --full-source") from exc
    lines = source.splitlines()

    comments = []  # (lineno, text)
    comment_col = {}  # lineno -> column the comment starts at, for the signature slice below
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                comments.append((tok.start[0], tok.string.lstrip("#").strip()))
                comment_col.setdefault(tok.start[0], tok.start[1])
    except tokenize.TokenizeError:
        pass  # comments are a bonus; a tokenize hiccup shouldn't sink extraction

    # Every function/class, at any depth, in source order, with a running
    # parent chain used only to compute indent — the actual attachment of
    # comments to nodes below doesn't need it.
    defs = []  # (node, depth)
    def walk(body, depth):
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defs.append((stmt, depth))
                walk(stmt.body, depth + 1)
    walk(tree.body, 0)

    def innermost_for(lineno: int):
        best = None
        for node, depth in defs:
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end and (best is None or node.lineno > best[0].lineno):
                best = (node, depth)
        return best

    by_node: dict[int, list[str]] = {}
    module_level: list[str] = []
    for lineno, text in comments:
        hit = innermost_for(lineno)
        (by_node.setdefault(id(hit[0]), []) if hit else module_level).append(text)

    out = []
    doc = ast.get_docstring(tree)
    if doc:
        out.append(doc.strip())
    out.extend(module_level)

    for node, depth in defs:
        indent = "    " * depth
        body_start = node.body[0].lineno if node.body else node.lineno + 1
        # The AST has no notion of comments, so a comment block sitting between
        # the header and the first real statement — the common case for a
        # function whose docstring is a `#` block rather than a string literal,
        # like _same_project below — is invisible to `body_start` and would
        # otherwise get swept into "signature" text verbatim, then printed a
        # second time by the by_node loop a few lines down. Comment lines are
        # dropped here and left to that loop; a trailing comment on a real
        # signature line is truncated, not dropped, so the code before it
        # survives.
        if body_start <= node.lineno:
            # Same-line body ("def f(): return x") — line-range slicing alone
            # can't separate signature from body, so cut the header line at
            # the body's own column instead.
            sig_lines = [lines[node.lineno - 1][:node.body[0].col_offset].rstrip()]
        else:
            sig_lines = []
            for i in range(node.lineno, body_start):
                raw = lines[i - 1]
                if i in comment_col:
                    raw = raw[:comment_col[i]].rstrip()
                if raw.strip():
                    sig_lines.append(raw)
        signature = "\n".join(sig_lines).strip()
        out.append("")
        out.append(indent + signature)
        node_doc = ast.get_docstring(node)
        if node_doc:
            out.append(indent + node_doc.strip().replace("\n", "\n" + indent))
        for text in by_node.get(id(node), []):
            out.append(indent + "# " + text)

    return "\n".join(out)


EXTRACTORS = {
    ".md": _read_text,
    ".markdown": _read_text,
    ".txt": _read_text,
    ".py": _python_structure,
    ".pdf": _pdftotext,
    ".docx": _textutil,
    ".doc": _textutil,
    ".rtf": _textutil,
    ".html": _textutil,
}

# Formats a human might reasonably point at, refused with a specific reason
# rather than a generic "unsupported".
KNOWN_UNSUPPORTED = {
    ".png": "images need vision or OCR, which capture does not do yet",
    ".jpg": "images need vision or OCR, which capture does not do yet",
    ".jpeg": "images need vision or OCR, which capture does not do yet",
    ".xlsx": "spreadsheets need a cell-aware extractor, not a text dump",
    ".csv": "tabular data needs a cell-aware extractor, not a text dump",
}


def supported_extensions() -> list[str]:
    return sorted(EXTRACTORS)


def extract(path: Path, full_source: bool = False) -> str:
    """Return the artifact's text, or raise explaining why it cannot be read.

    `full_source` only changes anything for `.py`: it skips the comments-only
    structure extraction above and reads the file verbatim, like every other
    text format already does. Named for what the human asks for, not for what
    the code does internally, since that's the decision being made at the
    call site.
    """
    if path.name.startswith("~$"):
        raise Unsupported("this is an editor lock file, not a real artifact")

    suffix = path.suffix.lower()
    if suffix in KNOWN_UNSUPPORTED:
        raise Unsupported(KNOWN_UNSUPPORTED[suffix])
    if suffix not in EXTRACTORS:
        raise Unsupported(f"no extractor for {suffix or 'files without a suffix'} "
                          f"(have: {', '.join(supported_extensions())})")

    extractor = _read_text if (suffix == ".py" and full_source) else EXTRACTORS[suffix]
    text = extractor(path)
    if len("".join(text.split())) < MIN_CHARS:
        raise NotEnoughText(
            f"only {len(''.join(text.split()))} characters of text came out of a "
            f"{path.stat().st_size:,} byte file. If this is a scan, it needs OCR "
            f"before it can be represented; capturing it now would store a shard "
            f"that stands for nothing.")
    return text
