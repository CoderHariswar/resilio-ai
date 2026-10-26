# Architecture Decision Records

| # | Title | Status |
|---|---|---|
| [0001](0001-postgresql-over-vectordb.md) | PostgreSQL + pgvector instead of a dedicated vector database | Accepted |
| [0002](0002-langgraph-for-hitl-state.md) | LangGraph checkpoints for human-in-the-loop state | Accepted |
| [0003](0003-rule-based-confidence.md) | Rule-based confidence instead of LLM self-reported confidence | Accepted |

## Template

```markdown
# NNNN. Title

- Status: Proposed | Accepted | Rejected | Superseded by NNNN
- Date: YYYY-MM-DD
- Deciders: @handle, @handle

## Context
What forces are at play? What constraints are non-negotiable?

## Decision
What we will do, stated in one paragraph.

## Alternatives considered
Each option with its honest strengths and the reason it lost.

## Consequences
Positive, negative, and what would make us revisit this.
```
