import os
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


def _looks_like_placeholder(value: str | None) -> bool:
    normalized = str(value or "").strip().upper()
    if not normalized:
        return False
    markers = (
        "GENERATE_",
        "CHANGE_TO_",
        "URL_ENCODED_PASSWORD",
        "REPLACE_ME",
        "YOUR_SECRET",
        "YOUR_PASSWORD",
        "EXAMPLE_SECRET",
    )
    return any(marker in normalized for marker in markers)


class Settings:
    APP_NAME = os.getenv("APP_NAME", "Xvond Core")
    APP_VERSION = os.getenv("APP_VERSION", "1.0.0")
    APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    LOG_JSON = os.getenv("LOG_JSON", "true" if APP_ENV in {"production", "prod"} else "false").strip().lower() in {"1", "true", "yes", "on"}
    DATABASE_URL = os.getenv("DATABASE_URL", "")
    REDIS_URL = os.getenv("REDIS_URL", "")
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    WHATSAPP_HUMAN_HANDOFF_MINUTES = max(5, int(os.getenv("WHATSAPP_HUMAN_HANDOFF_MINUTES", "60")))
    WEBSITE_VISITOR_TOKEN_TTL_SECONDS = max(300, int(os.getenv("WEBSITE_VISITOR_TOKEN_TTL_SECONDS", "2592000")))
    TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() in {"1", "true", "yes", "on"}
    AI_PII_REDACTION_ENABLED = os.getenv(
        "AI_PII_REDACTION_ENABLED",
        "true" if APP_ENV in {"production", "prod"} else "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    JWT_SECRET = os.getenv("JWT_SECRET", "")
    JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256").strip().upper()
    JWT_ISSUER = os.getenv("JWT_ISSUER", "xvond-core")
    JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "xvond-users")
    ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
    SUPERADMIN_EMAIL = os.getenv("SUPERADMIN_EMAIL", "")
    SUPERADMIN_PASSWORD = os.getenv("SUPERADMIN_PASSWORD", "")
    SUPERADMIN_FULL_NAME = os.getenv("SUPERADMIN_FULL_NAME", "Xvond Super Admin")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
    XAI_API_KEY = os.getenv("XAI_API_KEY", "")
    N8N_ENABLED = os.getenv("N8N_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "").strip()
    N8N_CHANNEL_WEBHOOK_URL = (
        os.getenv("N8N_CHANNEL_WEBHOOK_URL", "").strip()
        or (
            N8N_WEBHOOK_URL.rsplit("/", 1)[0] + "/xvond-channels"
            if N8N_WEBHOOK_URL
            else ""
        )
    )
    N8N_SHARED_SECRET = os.getenv("N8N_SHARED_SECRET", "")
    N8N_TIMEOUT_SECONDS = max(1.0, float(os.getenv("N8N_TIMEOUT_SECONDS", "15")))
    N8N_MAX_RETRIES = min(3, max(0, int(os.getenv("N8N_MAX_RETRIES", "1"))))
    KNOWLEDGE_SEMANTIC_ENABLED = os.getenv("KNOWLEDGE_SEMANTIC_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    KNOWLEDGE_EMBEDDING_PROVIDER = os.getenv("KNOWLEDGE_EMBEDDING_PROVIDER", "openai").strip().lower()
    KNOWLEDGE_EMBEDDING_MODEL = os.getenv("KNOWLEDGE_EMBEDDING_MODEL", "text-embedding-3-small").strip()
    KNOWLEDGE_SEMANTIC_MIN_SIMILARITY = min(1.0, max(-1.0, float(os.getenv("KNOWLEDGE_SEMANTIC_MIN_SIMILARITY", "0.35"))))
    KNOWLEDGE_SEMANTIC_WEIGHT = max(0.0, float(os.getenv("KNOWLEDGE_SEMANTIC_WEIGHT", "20")))
    CONFIG_ENCRYPTION_KEY = os.getenv("CONFIG_ENCRYPTION_KEY", "")
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USERNAME)

    @property
    def is_production(self) -> bool:
        return self.APP_ENV in {"production", "prod"}

    @property
    def is_test(self) -> bool:
        return self.APP_ENV in {"test", "testing"}

    def validate(self):
        errors = []
        if not self.DATABASE_URL:
            errors.append("DATABASE_URL is required")
        if not self.JWT_SECRET:
            errors.append("JWT_SECRET is required")
        if self.JWT_ALGORITHM not in {"HS256"}:
            errors.append("JWT_ALGORITHM must be HS256")
        if self.ACCESS_TOKEN_EXPIRE_MINUTES < 5 or self.ACCESS_TOKEN_EXPIRE_MINUTES > 1440:
            errors.append("ACCESS_TOKEN_EXPIRE_MINUTES must be between 5 and 1440")
        if self.KNOWLEDGE_EMBEDDING_PROVIDER not in {"openai"}:
            errors.append("KNOWLEDGE_EMBEDDING_PROVIDER must be a supported provider")
        if self.N8N_ENABLED:
            if not self.N8N_WEBHOOK_URL:
                errors.append("N8N_WEBHOOK_URL is required when n8n is enabled")
            if not self.N8N_CHANNEL_WEBHOOK_URL:
                errors.append("N8N_CHANNEL_WEBHOOK_URL is required when n8n is enabled")
            if not self.N8N_SHARED_SECRET:
                errors.append("N8N_SHARED_SECRET is required when n8n is enabled")
            elif _looks_like_placeholder(self.N8N_SHARED_SECRET):
                errors.append("N8N_SHARED_SECRET is using a placeholder value")
        if self.is_production:
            if not self.REDIS_URL:
                errors.append("REDIS_URL is required in production")
            if not self.PUBLIC_BASE_URL:
                errors.append("PUBLIC_BASE_URL is required in production")
            else:
                parsed_public_url = urlparse(self.PUBLIC_BASE_URL)
                if parsed_public_url.scheme.lower() != "https" or not parsed_public_url.hostname:
                    errors.append("PUBLIC_BASE_URL must use HTTPS in production")
                if (
                    parsed_public_url.username
                    or parsed_public_url.password
                    or parsed_public_url.query
                    or parsed_public_url.fragment
                    or parsed_public_url.path not in {"", "/"}
                ):
                    errors.append("PUBLIC_BASE_URL must be an HTTPS origin without credentials, path, query or fragment")
            if not self.TRUST_PROXY_HEADERS:
                errors.append("TRUST_PROXY_HEADERS must be enabled in production behind the Xvond reverse proxy")
            if _looks_like_placeholder(self.DATABASE_URL):
                errors.append("DATABASE_URL is using a placeholder credential")
            if len(self.JWT_SECRET) < 32:
                errors.append("JWT_SECRET must contain at least 32 characters in production")
            weak_secrets = {
                "change-this-before-production",
                "xvond-development-secret-change-before-production",
                "secret",
                "password",
            }
            if self.JWT_SECRET in weak_secrets or _looks_like_placeholder(self.JWT_SECRET):
                errors.append("JWT_SECRET is using a development or placeholder value")
            if len(self.CONFIG_ENCRYPTION_KEY) < 32:
                errors.append("CONFIG_ENCRYPTION_KEY must contain at least 32 characters in production")
            if _looks_like_placeholder(self.CONFIG_ENCRYPTION_KEY):
                errors.append("CONFIG_ENCRYPTION_KEY is using a placeholder value")
            if not self.SUPERADMIN_EMAIL:
                errors.append("SUPERADMIN_EMAIL is required in production")
            elif self.SUPERADMIN_EMAIL.strip().lower() == "admin@example.com":
                errors.append("SUPERADMIN_EMAIL is using the example address")
            if not self.SUPERADMIN_PASSWORD:
                errors.append("SUPERADMIN_PASSWORD is required in production")
            if self.SUPERADMIN_PASSWORD and len(self.SUPERADMIN_PASSWORD) < 12:
                errors.append("SUPERADMIN_PASSWORD must contain at least 12 characters")
            if _looks_like_placeholder(self.SUPERADMIN_PASSWORD):
                errors.append("SUPERADMIN_PASSWORD is using a placeholder value")
        if errors:
            raise RuntimeError("Invalid Xvond configuration: " + "; ".join(errors))


settings = Settings()
settings.validate()
