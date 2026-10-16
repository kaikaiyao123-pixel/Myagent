import secrets

from pydantic.v1 import BaseSettings

# 定义 Pydantic 的 BaseSettings 类
class Settings(BaseSettings):
    # 安全要求：JWT 签名密钥必须由环境变量 AUTHJWT_SECRET_KEY 提供。
    # 原默认值为字面量 'secret'，公网部署下任何人可据此伪造 token 冒充任意用户（含管理员）。
    # 未配置时退化为「每次启动随机生成」——安全，但会使既有会话失效。
    authjwt_secret_key: str = secrets.token_urlsafe(48)
    authjwt_token_location: list = ['cookies', 'headers']
    authjwt_cookie_csrf_protect: bool = False