from pydantic import field_validator, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    # Independent, opt-in customer AI; the personal Assistant keeps its original profile.
    freeman_customer_ai_enabled: bool = False
    freeman_customer_ai_openai_key: str = ''
    freeman_customer_ai_base_url: str = 'https://api.openai.com/v1'
    freeman_customer_ai_model: str = ''
    freeman_customer_ai_telegram_enabled: bool = False
    freeman_ai_telegram_enabled: bool = False
    freeman_ai_whatsapp_enabled: bool = False
    freeman_ai_enabled: bool = False
    freeman_ai_openai_key: str = ''
    freeman_ai_base_url: str = 'https://api.openai.com/v1'
    freeman_ai_model: str = ''
    freeman_ai_whatsapp_token: str = ''
    freeman_ai_whatsapp_phone_id: str = ''
    freeman_ai_whatsapp_verify_token: str = ''
    freeman_ai_meta_app_secret: str = ''
    freeman_ai_meta_api_version: str = ''
    freeman_ai_daily_token_limit: int = 1000000
    freeman_ai_retention_days: int = 90
    bot_token: str = ''
    bot_username: str = 'Freemanone_bot'
    web_base_url: str = 'https://freemanone.pro'
    support_url: str = ''
    database_url: str = 'sqlite+aiosqlite:///./data/freeman.db'
    redis_url: str = ''
    admin_ids: str = ''
    price_1: int = 400
    price_3: int = 1000
    price_6: int = 1800
    price_12: int = 3000
    device_limit: int = 1
    encryption_key: str = ''
    smtp_host: str = ''
    smtp_port: int = 465
    smtp_ssl: bool = True
    smtp_username: str = ''
    smtp_password: str = ''
    smtp_password_b64: str = ''
    smtp_from: str = ''
    yookassa_shop_id: str = ''
    yookassa_secret_key: str = ''
    yookassa_test_mode: bool = True
    robokassa_merchant_login: str = ''
    robokassa_password1: str = ''
    robokassa_password2: str = ''
    robokassa_result_url: str = ''
    _staff_roles: dict[int, str] = PrivateAttr(default_factory=dict)

    def owner_ids(self):
        return {int(value.strip()) for value in self.admin_ids.split(',') if value.strip().isdigit()}

    def is_owner(self, telegram_id: int) -> bool:
        return telegram_id in self.owner_ids() or self._staff_roles.get(telegram_id)=='administrator'

    def set_staff_roles(self, roles):
        self._staff_roles = {int(key): value for key, value in roles.items() if value in ('support','manager','administrator','partner') and int(key) not in self.owner_ids()}

    def set_staff_role(self, telegram_id: int, role):
        if role in ('support','manager','administrator','partner') and telegram_id not in self.owner_ids():
            self._staff_roles[telegram_id] = role
        else:
            self._staff_roles.pop(telegram_id, None)

    def role(self, telegram_id: int) -> str:
        return 'owner' if telegram_id in self.owner_ids() else self._staff_roles.get(telegram_id, '')

    def is_admin(self, telegram_id: int) -> bool:
        return self.role(telegram_id) in ('owner','administrator','support','manager')

    def can_free_subscription(self, telegram_id: int) -> bool:
        return self.is_owner(telegram_id) or self.role(telegram_id) == 'manager'

    def can_admin_action(self, telegram_id: int, action: str) -> bool:
        role = self.role(telegram_id)
        if role in ('owner','administrator'):
            return True
        if role == 'support':
            return action == 'admin:home' or action.startswith(('admin:t:', 'admin:guide:support', 'admin:guide:history'))
        if role == 'manager':
            return action in ('admin:home','admin:p:input:grant')
        return False

    @field_validator('price_1', 'price_3', 'price_6', 'price_12', 'device_limit')
    @classmethod
    def positive(cls, value):
        if value <= 0:
            raise ValueError('Value must be positive')
        return value

    @field_validator('support_url')
    @classmethod
    def support_link(cls, value):
        if value and not value.startswith('https://'):
            raise ValueError('SUPPORT_URL must use HTTPS')
        return value

    @field_validator('web_base_url')
    @classmethod
    def web_link(cls, value):
        if not value.startswith('https://'):
            raise ValueError('WEB_BASE_URL must use HTTPS')
        return value.rstrip('/')
