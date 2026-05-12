# Domain Docs

This is a single-context project.

## Before Exploring

Engineering skills should read these files when they are relevant to the task:

- `CONTEXT.md`
- `docs/requirements.md`
- `docs/architecture.md`
- ADRs under `docs/adr/`, if any exist

If an expected file does not exist, proceed silently. Do not suggest creating it unless the current task needs it.

## Domain Layout

```text
/
|-- CONTEXT.md
|-- docs/
|   |-- requirements.md
|   |-- architecture.md
|   `-- adr/
```

## Vocabulary

Use the domain terms from `CONTEXT.md` when naming issues, implementation slices, tests, and architecture proposals. Do not invent synonyms for terms that are already defined there.

Important current terms include:

- Personal Movie Recommendation System
- First-Version Goal
- Recommendation Session
- Movie Card
- Wishlist Loop
- Feedback
- Rating Semantics
- Viewing History Source
- Candidate Movie Source
- Candidate Pool Size
- PostgreSQL As System Of Record
- First-Version Strategy
- Hybrid Ranking Signals
- Technical Stack
- MVP Boundary

## ADR Conflicts

If a proposal contradicts an existing ADR, surface that conflict explicitly rather than silently overriding the decision.
