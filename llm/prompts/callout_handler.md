# System Prompt — Call-Out Handler

Placeholder. To be authored alongside Page 2 (Call Out), Development
Priority #5/#6 (CLAUDE.md).

This prompt will govern the LLM's role in the call-out UI: parsing free-text
call-outs into structured input for `solver.repair.repair_schedule`,
explaining infeasibility, and — on request — ranking candidate swaps via
`recommend_swaps` (see CLAUDE.md's Public API). It must never generate
assignments itself or be the final word on legality.
