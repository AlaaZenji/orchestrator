# orchestrator-runtime example

This is a minimal working example showing how to use orchestrator-runtime
on a fresh project.

## What this demonstrates

1. Bootstrap a fresh project
2. Run the burn queue (empty)
3. File a ticket
4. Run the burn queue again (1 ticket)
5. Run dependency audit
6. Run doctor to verify wiring

## Files

- `bootstrap.sh` — runs `orchestrator-setup init` with sensible defaults
- `verify.sh` — runs burn queue + dependency audit + doctor
- `bootstrap-output.json` — what the bootstrap returned
- `bootstrap-output.txt` — same in human-readable form

## Try it

```bash
cd /tmp
rm -rf my-test-project
mkdir my-test-project
cd my-test-project
bash /path/to/orchestrator-runtime/examples/bootstrap.sh
bash /path/to/orchestrator-runtime/examples/verify.sh
```

You should see:
- 19 files written
- "Found 0 burnable tickets" from the burn queue
- "All QUEUED tickets satisfy the dependency-declaration rule" from the audit
- "ok=True" from the doctor with all checks PASS
