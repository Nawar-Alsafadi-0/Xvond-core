# Employee compilation and action provisioning

The paid employee compile endpoint provisions `xvond_build` requirements before
saving its response. Draft testing uses the same path, including cached specs.
The employee config row is locked while preparing the spec; the caller commits
the spec, delivery plan, system prompt, action assignment and compiler usage in
one transaction. Failure rolls back the whole preparation.

Each build requirement gets an `action_request` contract in
`AgentToolAssignment.config.actions`. Its saved delivery entry lives under
`AgentConfig.settings.employee_builder.compiled_spec.delivery.action_plan`
(also returned as `employee_builder.delivery`). It records the action key,
generic workflow operation names, contract source and execution status.

Retries reuse the compiled spec without another compiler call. Missing contracts
are repaired; existing operator actions, action fields, confirmation settings,
credentials and disabled assignment/action switches are preserved. Old
`custom_required`/`unsupported` requirement statuses become Xvond-owned build
work. Connection and customer-input requirements stay pending and receive no
action contract. Compile and draft testing never call the workflow gateway,
connect accounts, install schedules or perform external actions.

`xvond_managed` means **contract provisioned**, not execution ready. Generated
contracts target `workflow_engine` and report `adapter_required`. They are not
listed as ready requirements or exposed as executable runtime tools. Go Live
requires a provisioned spec and the existing operational readiness checks; a
generated destination blocks readiness until a real adapter is configured.
The customer portal shows execution setup pending, rather than built/ready.

The existing generic workflow contract remains authoritative:
`<action_key>.(check_availability|execute|cancel)`, scoped to company and employee,
with idempotency keys for mutations. The bundled workflow accepts this envelope
but returns `provider_not_configured` for an unconfigured generated destination.
No arbitrary browser, scheduling, messaging or other side-effect adapter is added
by this change. Permission grants require an exact capability key or purpose
match; otherwise confirmation is required. An exact `never` rule disables the
generated action.

Database-backed tests cover persisted compilation, cached specs, repeated calls,
operator settings, tenant boundaries, customer prerequisites, rollback and draft
testing. A runtime test sends a stored generated contract through
`WorkflowActionRequestTool` and the actual bundled workflow dispatch code to
verify the unconfigured-adapter failure. Production deployment is separate.
