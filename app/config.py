"""
Central application configuration.

Every value is read from the environment (populated locally via a .env file
and on Render via the dashboard's Environment Variables screen). Nothing in
this file should ever be hardcoded per-deployment - the same code runs for
every portal, only the environment differs.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Shared portal APIs - same APIs consumed by every portal (FMS, CRM, ...).
    api_1_url: str = ""
    api_2_url: str = ""
    api_auth_token: str = ""

    app_name: str = "FMS Portal"
    environment: str = "development"

    # Used to build absolute deep-links / QR codes from the Developer Hub.
    public_base_url: str = "http://127.0.0.1:8000"

    request_timeout: float = 15.0
    port: int = 8000

    # Database/API target for the generic FMS gateway.
    # Leave blank to select automatically: development -> API_1 (Test),
    # production -> API_2 (Production). Set TEST/PRODUCTION explicitly only
    # when you intentionally want to override the deployment default.
    fms_db_target: str = ""

    # One Portal identity provider integration.
    one_portal_auth_enabled: bool = False
    one_portal_identity_capture_enabled: bool = True
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"

    # One Portal -> FMS ticket SSO (see app/auth/sso_session.py).
    # FMS_SSO_SECRET must equal the secret configured for this app in One
    # Portal's SSO_APPS setting (48+ random characters).
    fms_sso_secret: str = ""
    sso_session_minutes: int = 480
    # "none" is required when FMS is shown in an iframe on a different site.
    sso_cookie_samesite: str = "none"

    # Comments widget configuration (rendered only when both URLs are set).
    comments_origin: str = ""
    one_portal_origin: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    """Settings are cached - env vars are only read once per process."""
    return Settings()


settings = get_settings()
