---
name: numeric-summary
description: Compute a summary and a plain-text report for a supplied list of numbers.
---

For a summary of numbers supplied in the conversation, explicitly call
`run_skill_script` with skill_id `numeric-summary`, entry_point `summarize`,
and input `{"values": [1, 2, 6]}`. The separate execution permission must have
been granted in Skills. It returns count, minimum, maximum, mean and a saved
plain-text report. It needs only the Python standard library. Reading these
instructions never runs the script. No filesystem or network access is needed.
