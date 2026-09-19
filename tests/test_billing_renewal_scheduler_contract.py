from pathlib import Path


SOURCE = Path("scripts/automation_scheduler.py").read_text(encoding="utf-8")


def test_automation_scheduler_runs_billing_renewal_without_sharing_failure_domain():
    assert "run_due_service_renewals_once" in SOURCE
    automation = SOURCE.index("summary = run_due_schedules_once()")
    renewal = SOURCE.index("renewal = run_due_service_renewals_once()")
    assert automation < renewal
    assert 'logger.exception("Automation scheduler cycle failed")' in SOURCE
    assert 'logger.exception("Billing renewal cycle failed")' in SOURCE


def test_scheduler_logs_only_renewal_counts_not_payment_credentials():
    assert "renewal[\"checked\"]" in SOURCE
    assert "renewal[\"submitted\"]" in SOURCE
    assert "renewal[\"unknown\"]" in SOURCE
    assert "provider_customer_token" not in SOURCE
    assert "provider_card_token" not in SOURCE
    assert "payment_agreement_token" not in SOURCE
