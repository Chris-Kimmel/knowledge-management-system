"""The single seam through which this Hub talks to a model.

Every model call in pipeline/ goes through `call()`. That is the point: the
Processor is meant to be swappable (founding_design.md §2 — stateless, and "a
different model entirely is simply a different tool applied to the same
persistent Hub"), and keeping exactly one function that knows how to reach a
model is what makes that literally true rather than aspirational. Moving to
the Anthropic SDK, or to another provider, is a rewrite of this file and
nothing else.

Structured output is required, not parsed out of prose: the caller supplies a
JSON Schema and gets back a validated object, so a malformed response fails
here instead of becoming a malformed shard.
"""

import json
import subprocess
import tempfile

DEFAULT_MODEL = "claude-opus-5"

# Capture quality compounds — a weak summary is a permanent defect in the Hub
# until something re-captures it, unlike a weak answer in a chat session, which
# the next message fixes. That asymmetry is why this default stays at the
# strongest model regardless of what model the human happens to be running
# their own session on; the two are independent knobs.


def call(prompt: str, system_prompt: str, schema: dict,
         model: str = DEFAULT_MODEL, max_cost: float | None = None,
         timeout: int = 900) -> dict:
    """One call, no tools, structured output validated against `schema`.

    Runs from an empty temporary directory so the call picks up no CLAUDE.md
    and no surrounding project context: the prompt is the entire instruction,
    which keeps a call's behaviour independent of wherever it was invoked from.
    """
    cmd = [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--tools", "",
        "--no-session-persistence",
        "--system-prompt", system_prompt,
        "--json-schema", json.dumps(schema),
    ]
    if max_cost is not None:
        cmd += ["--max-budget-usd", str(max_cost)]

    with tempfile.TemporaryDirectory() as neutral_cwd:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              cwd=neutral_cwd, timeout=timeout)

    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude returned non-JSON: {proc.stdout[:500]}")
    if result.get("is_error"):
        raise RuntimeError(f"claude reported an error: {result.get('result', '')[:500]}")
    if "structured_output" not in result:
        raise RuntimeError(f"no structured_output in response: {proc.stdout[:500]}")
    return result
