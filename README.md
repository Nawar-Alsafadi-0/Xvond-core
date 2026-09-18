# Xvond Core

Xvond Core is a modular managed AI operations platform built with FastAPI, SQLAlchemy, PostgreSQL, Redis and an optional self-hosted Workflow Engine for business side effects.

## Requirements

- Python 3.14
- PostgreSQL 17
- Redis for production queue/runtime services

## Local setup

1. Create and activate a virtual environment.
2. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and replace every placeholder required by the services you intend to run.
4. Start PostgreSQL:

   ```powershell
   docker compose up -d postgres
   ```

5. Apply database migrations:

   ```powershell
   alembic upgrade head
   ```

6. Create the first super administrator:

   ```powershell
   python scripts/create_superadmin.py
   ```

7. Start the API:

   ```powershell
   uvicorn backend.app.main:app --reload
   ```

Open `http://127.0.0.1:8000/health`, `/admin-ui`, or `/customer-ui`.

## Tests

```powershell
pytest -q
```

GitHub CI also builds a completely fresh PostgreSQL database through Alembic, checks frontend/shell syntax, validates production Compose and builds the production Docker image.

## Production principles

- Set `APP_ENV=production`.
- Use a unique JWT secret of at least 32 random characters.
- Configure at least one real AI provider; Mock is development-only.
- Keep the provider catalog aligned with adapters actually registered by `AIEngine`.
- Configure SMTP before enabling password reset.
- Put the API behind HTTPS and a trusted reverse proxy.
- Use Redis-backed runtime services in production.
- Keep `.env`, database backups, logs and provider/customer credentials out of Git.
- Keep an encrypted off-server database backup in storage approved for the deployment's data-residency requirements.
- Do not expose a business action as live unless the Workflow Engine and its intended execution target have passed acceptance.

## Canonical production deployment

Production releases must use the reviewed repository state and the release script rather than an improvised sequence of Docker commands. `PUBLIC_BASE_URL` is the canonical public Core origin; install/verify the repository-managed Core routes in the HTTPS vhost for that exact hostname before the application cutover:

```bash
python3 scripts/install_nginx_core_routes.py
./scripts/deploy_production.sh
```

The Nginx installer reads `PUBLIC_BASE_URL` from the process environment or repository `.env`, backs up the selected active vhost, validates with `nginx -t`, reloads Nginx and restores the previous vhost automatically if validation or reload fails. It must be run with root privileges.

The release script validates the Git working tree and canonical release branch, production Compose and production environment, brings PostgreSQL/Redis up, takes a fresh database backup before replacing application containers, builds one application image, recreates the API, WhatsApp worker and automation scheduler from that same image, verifies the WhatsApp worker lease and scheduler heartbeat, verifies Workflow Engine health and post-cutover reachability when enabled, verifies image parity across runtime processes, checks the internal `/health/ready`, then requires the canonical `PUBLIC_BASE_URL/health/ready` to succeed over HTTPS and report the production environment before declaring the release complete.

The application entrypoint applies Alembic migrations and safe startup tasks before starting the API. The WhatsApp worker intentionally uses the already-migrated application image and does not run the application entrypoint independently.

For a customer-specific release, configure the acceptance environment values supported by `scripts/deploy_production.sh` so production acceptance runs as part of the release.

## Customer acceptance

Run the pre-live production gate for the target customer/employee:

```bash
python -m scripts.production_acceptance \
  --company-id COMPANY_ID \
  --agent-id AGENT_ID
```

Optionally perform one explicit billable provider health check using the employee's resolved production route. This does not create a customer conversation or execute customer tools:

```bash
python -m scripts.production_acceptance \
  --company-id COMPANY_ID \
  --agent-id AGENT_ID \
  --live-ai
```

For an employee with enabled business actions, production acceptance verifies the canonical Workflow Engine `health_check`. Delivery Readiness also repeats that live workflow health check immediately before Go Live; a configured n8n URL/secret alone is not enough.

After pre-live checks pass, use the controlled production activation sequence documented in `docs/customer-onboarding-runbook.md`: activate only the intended Company/AI Employee/channel, immediately perform real external-channel acceptance, and stop/deactivate the runtime if that acceptance fails. Do not call the service commercially launched merely because configuration or CI is green.

After successful external acceptance, run the post-live gate:

```bash
python -m scripts.production_acceptance \
  --company-id COMPANY_ID \
  --agent-id AGENT_ID \
  --require-live
```

## WhatsApp Coexistence truth

A WhatsApp Coexistence channel keeps two separate facts:

- `connected`: the Meta transport is usable and the required app/WABA/webhook subscriptions are present.
- `coexistence_ready`: a real `smb_message_echoes` event has been observed, proving native WhatsApp Business App human takeover in practice.

A correctly subscribed new Coexistence connection may serve AI traffic before the first native human reply. That first real Business App reply supplies the echo evidence and switches the conversation to human control.

## Encrypted off-server PostgreSQL backups

Local PostgreSQL dumps are created first in the `xvond_backups` volume. The optional `offsite-backup` profile then sends that backup set to a Restic repository. Restic encrypts repository contents client-side using `RESTIC_PASSWORD`.

Configure `RESTIC_REPOSITORY` and `RESTIC_PASSWORD` in `.env`. For S3-compatible repositories, also configure the required AWS-style credentials and region. Choose a repository location that satisfies the deployment's data-residency requirements.

Start encrypted offsite backup when it is part of the deployment:

```bash
docker compose -f docker-compose.production.yml \
  --profile offsite-backup up -d
```

Verify that the remote repository can actually be restored and that the restored PostgreSQL dump checksum is valid:

```bash
docker compose -f docker-compose.production.yml \
  --profile offsite-verify run --rm offsite-restore-verify
```

A successful upload is not considered a verified backup until the restore verification command succeeds.
