# eCom Support Agent Explained — Lesson 1

An interactive, step-by-step walkthrough of how one customer message travels through the agent
(web app → classifier → retriever → Haiku → permission gate → MCP server → reply), and what each
step records in the trace.

## Open it

Double-click `index.html`. No server, build step or internet connection is needed.
Keep this folder together — `index.html` loads its font from `fonts/`.

## Use it

- **Next / Back**, the `←` `→` keys, or click any step number in the bottom bar.
- Append `#9` to the URL to open step 9 directly (the cross-customer denial).
- With *Reduce motion* turned on, the page shows each step without animation.

The walkthrough follows tickets 3 and 5 from `scenarios.py` against `mcp_server/mock_data.py`.
Trace rows are illustrative, not a captured run. Cutoff distances and eval numbers come from the
First-Principles Guide.

## Files

| File | What it is |
|---|---|
| `index.html` | The whole lesson: markup, styles, diagram and script |
| `fonts/` | JetBrains Mono (400/500/700/800), self-hosted, SIL OFL (`fonts/OFL.txt`) |
