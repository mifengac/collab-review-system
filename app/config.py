"""应用配置，支持通过环境变量 / .env 覆盖。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "材料协同办理系统"
    app_host: str = "0.0.0.0"
    app_port: int = 5009
    secret_key: str = "dev-secret-change-me-in-production"
    access_token_expire_minutes: int = 480
    algorithm: str = "HS256"

    # sqlite:///./data/collab.db 或 postgresql://...
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'collab.db'}"

    admin_username: str = "admin"
    admin_password: str = "Admin@123456"
    admin_display_name: str = "系统管理员"

    upload_dir: str = str(BASE_DIR / "uploads")
    debug: bool = False

    # 是否创建演示账号 handler1 / leader_a / leader_b（生产环境务必 false）
    seed_demo_users: bool = False

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def upload_path(self) -> Path:
        return Path(self.upload_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings()
