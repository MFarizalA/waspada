"""Allow ``python -m waspada.bench_society`` to run the benchmark cleanly
(avoids the runpy double-import warning from running the submodule directly)."""
from .run_bench import main

raise SystemExit(main())
