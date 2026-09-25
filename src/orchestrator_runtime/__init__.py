"""orchestrator-runtime — project-agnostic orchestrator engine.

This package is the canonical open-source implementation of the orchestrator
runtime. It's the same engine used in production on multi-project engineering
workloads, packaged for reuse across any project.

Public API:
    from orchestrator_runtime import cli
    cli.main()

Or via the installed entry-point:
    $ orchestrator-setup --help
    $ orchestrator-setup init --target /path/to/project --prefix myproject
    $ orchestrator-setup install-cli --prefix orch
    $ orchestrator-setup doctor --target /path/to/project --prefix myproject

The package itself contains only templates and the CLI. The runtime scripts
(burn_queue, lease, heartbeat, etc.) live under
`orchestrator_runtime/templates/scripts/` and are COPIED into target projects
at bootstrap time — that's how they find the project root via
`Path(__file__).parent.parent.parent`.
"""

__version__ = "0.1.0"
__author__ = "AlaaZenji"
__license__ = "MIT"
