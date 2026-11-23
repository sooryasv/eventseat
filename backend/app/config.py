"""Environment-driven settings with fail-fast secret validation."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-only-change-me-dev-only-change-me"  # noqa: S105 - the public dev default


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://eventseat:eventseat@localhost:5432/eventseat"
    test_database_url: str = "postgresql://eventseat:eventseat@localhost:5433/eventseat_test"
    jwt_secret: str = DEV_JWT_SECRET
    jwt_expire_minutes: int = 60
    cookie_secure: bool = False
    allowed_origins: str = "http://localhost:5173,http://localhost:8000"
    use_null_pool: bool = False
    enable_demo_tools: bool = True
    run_seed_on_startup: bool = True
    hold_ttl_seconds: int = 300
    sweeper_interval_seconds: int = 60

    @model_validator(mode="after")
    def reject_dev_secret_when_secure(self) -> "Settings":
        # A public dev secret behind HTTPS would let anyone forge session cookies.
        if self.cookie_secure and self.jwt_secret == DEV_JWT_SECRET:
            raise ValueError(
                "JWT_SECRET must be changed from the dev default when COOKIE_SECURE=true"
            )
        return self

    @property
    def origins(self) -> frozenset[str]:
        return frozenset(
            o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
