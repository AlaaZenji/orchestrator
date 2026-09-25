# Contributing to orchestrator-runtime

Thanks for your interest in contributing. The orchestrator is intentionally small — most PRs should fit one of four categories.

## Code of conduct

Be respectful. We're all here to ship reliable software.

## How to contribute

1. Fork the repo
2. Create a feature branch (`git checkout -b feat/your-feature`)
3. Make your change with tests
4. Run the smoke tests locally: `pip install -e . && pytest tests/ -v`
5. Open a PR against `master`

## What to work on

The four categories of accepted PRs:

### 1. New runtime script

If you're adding a new orchestration primitive (e.g. a metrics collector, a release-tagger, an experiment-runner):

- Add it under `src/orchestrator_runtime/templates/scripts/`
- Use `ROOT = Path(__file__).parent.parent.parent` to resolve the project root
- Make it stdlib-only unless the dependency is justified
- Add a docstring explaining what it does and why
- Add a smoke test under `tests/`

### 2. New state-store backend

If you're adding a backend beyond `postgres`/`sqlite`/`file`/`skip` (e.g. `redis`, `dynamodb`):

- Implement the same `claim()` / `heartbeat()` / `release()` interface as `lease.py`
- Implement the same `enqueue()` / `mark_applied()` interface as `outbox.py`
- Add a migration file under `src/orchestrator_runtime/templates/migrations/<backend>/`
- Update the CLI's `_init_state_store()` to handle the new backend
- Add `--state-store=<backend>` to the CLI choices

### 3. New verification lens

If you're adding a new adversarial verification perspective (beyond correctness / per_tenant / failure_modes / reversibility / security / performance / idempotency):

- Add a prompt template under `src/orchestrator_runtime/templates/prompts/`
- Document the question it answers and the adversarial posture
- Update `WORKFLOW.md §Step 5` to list the new lens
- Update the CLI's verifier count logic

### 4. New doc section

If you're adding a new section to `ARCHITECTURE.md` / `WORKFLOW.md` / `CONVENTIONS.md`:

- Make it project-agnostic (no domain-specific examples)
- Make it procedural (Steps 0-N, decision trees, checkboxes)
- Add it to the right doc — `ARCHITECTURE.md` for invariants, `WORKFLOW.md` for the ticket lifecycle, `CONVENTIONS.md` for status enum / schema / commit format

## What NOT to contribute

- Domain-specific content (healthcare, fintech, etc.) — the package is project-agnostic
- Per-prefix slash commands — these are user-written, not package-shipped
- Specialist prompts (Hospital Ops, Regulatory, etc.) — user-written
- Hardcoded project paths — env vars only
- Non-stdlib dependencies without justification

## Testing

Before opening a PR:

```bash
pip install -e .
pytest tests/ -v
```

The CI workflow at `.github/workflows/ci.yml` runs:
1. **Lint** — Python compile + SQL sanity check
2. **Smoke test** — bootstrap + doctor end-to-end (Python 3.10–3.13)
3. **Scrub check** — verifies no project-specific content has crept in

## Project-agnosticism check

Every PR must keep the package project-agnostic. The CI scrub check enforces this — it greps the source tree for healthcare terms (HL7, FHIR, clinical, etc.), project names, and ticket IDs. Don't add any of these.

## Commit messages

```
<ticket-id>: <short summary>

<paragraph: what changed and why>

<file:line evidence>
```

If your change is small and doesn't have a ticket, just use a short summary.

## License

By contributing, you agree that your contributions will be licensed under MIT.
