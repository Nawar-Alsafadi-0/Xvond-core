# AGENTS.md — Xvond Core

These instructions apply to the entire repository.

## Required reading before significant work

Read in this order:

1. `BUSINESS.md`
2. `PROJECT_CHECKPOINT.md`
3. relevant documents under `docs/`
4. the actual implementation and tests

Do not begin a large implementation from file names or a single issue alone.

## Mental model

Xvond is an **AI Employee platform**.

The customer starts from a job/outcome, not from technical configuration.

The core promise is:

**Build your employee.**

A working employee may communicate through multiple channels, use business knowledge, execute authorized actions, connect to external systems, run scheduled/background work and be refined over time.

Channels are surfaces, not separate employees.

## Business before local code assumptions

Existing code is runtime truth, but it is not automatically product truth.

If the implementation conflicts with `BUSINESS.md`, identify the mismatch instead of assuming the existing implementation defines the desired product.

If business intent describes a future capability while `PROJECT_CHECKPOINT.md` says it is not service-ready, preserve that distinction.

Do not present aspirational capability as completed functionality.

## Canonical product modes

Keep both delivery models:

- Self-Service — Job-Brief-driven Build your employee flow.
- Xvond Managed — operator-led delivery for managed/complex work.

Do not collapse them into one lifecycle.

## Architecture principles

- One AI Employee owns identity, behavior, knowledge and permissions.
- Channels do not own duplicate employee truth.
- AI providers never directly own application state or external side effects.
- External actions must pass Xvond authorization/validation and use the approved execution path.
- Workflow Engine is an execution plane, not the Xvond business database.
- Tenant boundaries must be enforced everywhere.
- Unknown or unproven external outcomes fail closed.
- Avoid customer-specific hard-coded behavior when a reusable contract can express the job.
- Preserve versioning/refinement so changes do not unnecessarily rebuild unrelated working parts.

## Product planning rule

For any significant task, evaluate:

1. What customer/business problem is being solved?
2. Which employee lifecycle stage does it improve?
3. Is this generic enough to help more than one customer/job?
4. What real runtime dependency is required?
5. How will readiness know it actually works?
6. What test proves the intended business outcome?
7. Does this create new customer setup burden that can be avoided?

A technically correct implementation is incomplete if the real customer path remains impossible.

## Engineering workflow

Before changing code:

1. inspect the existing models/services/routes/UI/tests;
2. map the current end-to-end path;
3. identify existing abstractions;
4. identify business and runtime invariants;
5. plan the smallest coherent vertical slice.

Then:

1. implement;
2. add/update tests;
3. run relevant tests;
4. inspect the final diff;
5. verify migrations/config when applicable;
6. report real remaining blockers.

Do not stop at a plan when safe implementation is possible.

## Database

Use Alembic for schema changes.

Never rewrite already-deployed migration history.

Never destroy production data to simplify a feature.

## Security

Never commit secrets, credentials, tokens, production environment files or customer credentials.

Credential-shaped setup must use protected secret/connection paths.

Fail closed when authorization, tenant ownership or execution configuration is uncertain.

## Readiness

Keep these separate:

- code ready;
- production ready;
- service ready.

Mocks, stored configuration, catalog entries and passing CI are not proof that an external service works.

## Git

Do not force-push or rewrite `main`.

Use dedicated branches for non-trivial work.

Do not weaken tests merely to make a change pass.

Keep PR descriptions explicit about:

- business effect;
- architecture effect;
- implementation;
- migrations;
- tests;
- external acceptance still required;
- known limitations.

## Completion standard

Do not call work complete when only configuration/contracts/UI exist but the required runtime path cannot execute.

For Xvond, **the employee doing the job is the product outcome**.
