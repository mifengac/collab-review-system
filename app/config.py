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

    # 认证模式：local | oa | mixed
    auth_mode: str = "local"

    # OA 登录适配（仅占位，勿写入真实地址以外的敏感信息到仓库）
    oa_base_url: str = ""
    oa_login_path: str = "/hportal/j_security_check"
    oa_profile_path: str = "/hportal/view/GetModuleTree.do"
    oa_login_timeout_seconds: int = 8
    oa_default_role: str = "viewer"
    oa_verify_tls: bool = False
    # 部分环境需先调 checkUserPKI / getUserNum，默认关闭
    oa_precheck_enabled: bool = False
    oa_pki_path: str = "/hportal/Login/checkUserPKI.jsp"
    oa_user_num_path: str = "/hitem/api/getUserNum.jsp"

    # OA 公文池同步（OA 列表每页通常 10 条，与 oa.har 一致）
    oa_sync_on_login: bool = False
    oa_sync_max_pages: int = 3
    oa_sync_page_size: int = 10
    oa_sync_modules: str = "todo,unread,done,read_done,running"
    oa_list_path: str = "/hmoa/s"

    # 模拟 OA（仅开发预览；DEBUG=false 时禁止启用）
    oa_mock_enabled: bool = False

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def upload_path(self) -> Path:
        return Path(self.upload_dir)

    @property
    def auth_mode_normalized(self) -> str:
        mode = (self.auth_mode or "local").strip().lower()
        if mode not in {"local", "oa", "mixed"}:
            return "local"
        return mode

    @property
    def oa_enabled(self) -> bool:
        return self.auth_mode_normalized in {"oa", "mixed"}

    @property
    def oa_sync_module_list(self) -> list[str]:
        parts = [p.strip() for p in (self.oa_sync_modules or "").split(",")]
        return [p for p in parts if p]

    @property
    def oa_mock_banner_enabled(self) -> bool:
        """仅 DEBUG=true 且 OA_MOCK_ENABLED=true 时显示模拟环境标识。"""
        return bool(self.debug) and bool(self.oa_mock_enabled)


@lru_cache
def get_settings() -> Settings:
    return Settings()
