import pytest

from backend.app.core.config.settings import Settings, _looks_like_placeholder


def _valid_production_settings() -> Settings:
    item = Settings()
    item.APP_ENV = "production"
    item.DATABASE_URL = "postgresql+psycopg://xvond:real-password@postgres:5432/xvond"
    item.REDIS_URL = "redis://redis:6379/0"
    item.PUBLIC_BASE_URL = "https://api.xvond.com"
    item.TRUST_PROXY_HEADERS = True
    item.JWT_SECRET = "a-real-jwt-secret-with-more-than-thirty-two-characters"
    item.JWT_ALGORITHM = "HS256"
    item.ACCESS_TOKEN_EXPIRE_MINUTES = 60
    item.CONFIG_ENCRYPTION_KEY = "a-real-config-key-with-more-than-thirty-two-characters"
    item.SUPERADMIN_EMAIL = "ops@xvond.com"
    item.SUPERADMIN_PASSWORD = "a-real-admin-password"
    item.N8N_ENABLED = False
    item.KNOWLEDGE_EMBEDDING_PROVIDER = "openai"
    return item


def test_placeholder_detector_catches_example_secret_shapes():
    for value in (
        "GENERATE_A_LONG_RANDOM_SECRET",
        "CHANGE_TO_A_STRONG_DATABASE_PASSWORD",
        "postgresql://u:URL_ENCODED_PASSWORD@postgres/db",
        "REPLACE_ME",
        "YOUR_SECRET_VALUE",
        "YOUR_PASSWORD_VALUE",
        "EXAMPLE_SECRET_VALUE",
    ):
        assert _looks_like_placeholder(value) is True
    assert _looks_like_placeholder("a-real-production-secret") is False


def test_valid_production_configuration_passes():
    item = _valid_production_settings()
    item.validate()


def test_production_rejects_example_credentials_and_insecure_public_url():
    item = _valid_production_settings()
    item.DATABASE_URL = "postgresql+psycopg://xvond:URL_ENCODED_PASSWORD@postgres:5432/xvond"
    item.PUBLIC_BASE_URL = "http://api.xvond.com"
    item.JWT_SECRET = "GENERATE_A_LONG_RANDOM_SECRET_THAT_IS_LONG_ENOUGH"
    item.CONFIG_ENCRYPTION_KEY = "GENERATE_A_CONFIG_ENCRYPTION_KEY_THAT_IS_LONG_ENOUGH"
    item.SUPERADMIN_EMAIL = "admin@example.com"
    item.SUPERADMIN_PASSWORD = "CHANGE_TO_A_STRONG_ADMIN_PASSWORD"

    with pytest.raises(RuntimeError) as exc_info:
        item.validate()

    message = str(exc_info.value)
    assert "DATABASE_URL is using a placeholder credential" in message
    assert "PUBLIC_BASE_URL must use HTTPS in production" in message
    assert "JWT_SECRET is using a development or placeholder value" in message
    assert "CONFIG_ENCRYPTION_KEY is using a placeholder value" in message
    assert "SUPERADMIN_EMAIL is using the example address" in message
    assert "SUPERADMIN_PASSWORD is using a placeholder value" in message


def test_production_requires_proxy_headers_for_real_client_rate_limits():
    item = _valid_production_settings()
    item.TRUST_PROXY_HEADERS = False

    with pytest.raises(RuntimeError, match="TRUST_PROXY_HEADERS must be enabled in production"):
        item.validate()


def test_workflow_secret_placeholder_is_rejected_when_workflow_enabled():
    item = _valid_production_settings()
    item.N8N_ENABLED = True
    item.N8N_WEBHOOK_URL = "http://workflow-engine:5678/webhook/xvond-actions"
    item.N8N_SHARED_SECRET = "GENERATE_A_LONG_RANDOM_WORKFLOW_SHARED_SECRET"

    with pytest.raises(RuntimeError, match="N8N_SHARED_SECRET is using a placeholder value"):
        item.validate()


@pytest.mark.parametrize(
    "url",
    [
        "https://api.xvond.com/v1",
        "https://api.xvond.com?source=bad",
        "https://api.xvond.com#fragment",
        "https://user:password@api.xvond.com",
    ],
)
def test_production_public_base_url_must_be_clean_origin(url):
    item = _valid_production_settings()
    item.PUBLIC_BASE_URL = url

    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL must be an HTTPS origin"):
        item.validate()
