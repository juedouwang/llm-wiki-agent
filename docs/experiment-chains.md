# E-06 Experiment Chains Contract

E-06 adds the machine-only artifact
`.llmwiki/projects/<project_id>/indexes/experiment-chains.json`. It is strict
Schema v1 with `kind=llmwiki-experiment-chains` and
`chain_version=experiment-chains-v1`.

## Explicit experiment observations

The host explicitly supplies configuration, run, result, Claim, and
result-to-Claim observations to
`ResearchCoreService.experiment_chains(project_id, observations=...)`. The
closed chain is:

```text
config -> run -> result -> claim
```

Configuration observations retain bounded structured parameters. Runs bind to
one declared configuration and may retain status, conditions, Evidence IDs,
and uncertainty. Results bind to one declared run and must retain all of the
following:

- a non-empty conditions object;
- a non-empty metrics object;
- at least one duplicate-free Evidence ID.

Claims are explicit host targets. A result-to-Claim link declares one of
`supports`, `contradicts`, `inconclusive`, or `contextualizes`; Core never
chooses that relation from filenames, prose, or metric values.

## Conflicting experiments coexist

Different results may link to the same Claim with different declared
relations. For example, one result may `support` a Claim while another
`contradicts` it. E-06 preserves each configuration, run, result, link, and
complete chain under a stable ID, so one experiment cannot overwrite another.
Every complete chain retains the result conditions and the duplicate-free
Evidence closure from its configuration, run, result, Claim, and link.

This coexistence is structural only. E-06 does not infer a conflict, decide
which result is correct, change Claim lifecycle state, or synthesize a
scientific conclusion.

## Honest metadata fallback

When the host supplies no observations, E-06 derives only bounded candidates
from current Manifest classification metadata. Configuration-classified files
may become config candidates; experiment, notebook, and run-log files may
become run candidates; result and figure files may become result candidates.
Candidates are marked `inferred` with an explicit uncertainty reason.

The fallback emits no semantic configurations, runs, results, Claims, links,
or complete chains. `coverage` and `gaps` explicitly report the missing
observations instead of presenting an empty artifact as successful scientific
understanding.

## Integrity and currentness

The artifact binds to the exact current `project-inventory-v4` Manifest
version, scan generation, ordinary-file count/bytes, and SHA-256. JSON is
strict UTF-8, canonical, duplicate-key-free, closed-field, and fails closed on
legacy/future versions, unknown fields, invalid endpoints or paths,
duplicate IDs, noncanonical ordering, inconsistent projections, and stable-ID
tampering.

Generation and current loading use the shared per-project
`indexes/machine-state.lock`, publish by atomic replacement, and recheck exact
Manifest bytes immediately before replace. Consumers must call
`load_current_experiment_chains(...)` before acting on the artifact. The
current loader rejects stale Manifest bindings and independently rebuilds the
metadata fallback when that mode is declared.

## Explicit non-goals

E-06 does not open registered source bytes, read research binaries, call an
LLM, write curated Markdown, persist new Evidence records, infer scientific
semantics, or add CLI, MCP, Hook, or Web behavior. Rendering and orchestration
remain later R3 units.
