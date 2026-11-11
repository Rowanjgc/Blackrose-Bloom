# A compact draft for a precise picture

Use the visible tool schema as the contract. The model authors content, the
host supplies style/character constants and layout defaults. Specific planning
should make a sparse picture more accurate, rather than add visual clutter.

Example first illustration_draft call:

```json
{
  "changes": {
    "core_idea": "Two needles on one thread work through each hole from opposite sides.",
    "facts": [
      "Hand saddle stitching uses two needles attached to opposite ends of one thread.",
      "Both needles pass through each pre-punched hole from opposite sides."
    ],
    "scene": "Two aligned leather layers held in a stitching pony at center. A few completed stitches lead to the active hole. Plenty of white margin; a flat simple sketch.",
    "action": "Xiaohei holds one needle in each hand, pulling the thread ends evenly apart after both pass through the same hole.",
    "objects": [
      {"id": "leather", "description": "Two leather layers with aligned pre-punched holes", "position": "Center"}
    ],
    "labels": [
      {"text": "Same hole", "target": "leather", "position": "Beside the active hole"}
    ],
    "review_notes": "Avoid a whipping loop over the leather edge. The visible thread must pass through aligned holes in both layers."
  }
}
```

Use the returned draft_id and revision. If the tool reports an unclear action,
a repair sends only the changed field:

```json
{
  "draft_id": "draft-1",
  "revision": 1,
  "changes": {
    "action": "Both of Xiaohei's hands grip the needle ends; arms pull outward to tension the shared thread through the active hole."
  }
}
```

When the returned revision is structurally valid, call image_gen:

```json
{"draft_id": "draft-1", "revision": 2}
```

IDs and revisions here are examples; always use the actual returned values.
Drafts are scoped to the current reply and saved on disk. Updates replace
specified top-level fields; all other decisions survive. Labels and connections
must refer to declared objects or xiaohei. Omit connections if no diagram paths
are needed. Empty labels explicitly requests no visible text.

Structural validation does not establish factual correctness. Check unfamiliar
techniques against source material and record uncertainty honestly. Inspect
actual generated pixels separately before claiming visual QA.
