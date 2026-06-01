from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    jellyfin_url: str = "http://localhost:8096"
    jellyfin_username: str = ""
    jellyfin_password: str = ""
    jellyfin_token: str = ""
    jellyfin_user_id: str = ""

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "info"


settings = Settings()
