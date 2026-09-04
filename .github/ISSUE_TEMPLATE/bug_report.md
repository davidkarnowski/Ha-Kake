---
name: Bug report
about: Something in the dashboard, reader or a decoder is wrong
title: ''
labels: bug
assignees: ''
---

## What happened

<!-- One or two sentences. What did you see? -->

## What you expected

## Setup

- **Vehicle profile** (`--vehicle`, e.g. `leaf_ze0`, `lancer_2009`):
- **Car** (year, model, market — e.g. 2012 Nissan Leaf SL, US):
- **Adapter** (BLE / USB CH340 / other; make and model if known):
- **OS and Python version**:
- **Ha-Kake version or commit**:

## Does it reproduce in demo mode?

<!-- Demo mode needs no car and no adapter: start the web app with
     `--demo` (or `HAKAKE_DEMO=docs/demo`), which serves the canned
     state/history snapshots. Say yes / no / not applicable. This is the single most useful line in the report: it
     tells us whether the bug is in the UI and decode path or in the
     hardware conversation. -->

- [ ] Yes, reproduces in demo mode
- [ ] No, only with a real car/adapter
- [ ] Not applicable / couldn't try

## Steps to reproduce

1.
2.
3.

## Evidence

<!-- Raw frames, adapter transcript, the /api/status JSON, a screenshot,
     the reader's stderr. Paste in a code fence. -->

```
```

## Privacy check

- [ ] I have removed my VIN, adapter UUID/MAC, home paths and any personal
      location data from what I pasted above.
