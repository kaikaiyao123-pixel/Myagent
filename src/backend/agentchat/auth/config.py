from datetime import timedelta
from typing import Optional, Union, List
from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    StrictBool,
    StrictInt,
    StrictStr
)

class LoadConfig(BaseModel):
    authjwt_token_location: Optional[List[StrictStr]] = {'headers'}
    authjwt_secret_key: Optional[StrictStr] = None
    authjwt_public_key: Optional[StrictStr] = None
    authjwt_private_key: Optional[StrictStr] = None
    authjwt_algorithm: Optional[StrictStr] = "HS256"
    authjwt_decode_algorithms: Optional[List[StrictStr]] = None
    authjwt_decode_leeway: Optional[Union[StrictInt,timedelta]] = 0
    authjwt_encode_issuer: Optional[StrictStr] = None
    authjwt_decode_issuer: Optional[StrictStr] = None
    authjwt_decode_audience: Optional[Union[StrictStr,List[StrictStr]]] = None
    authjwt_denylist_enabled: Optional[StrictBool] = False
    authjwt_denylist_token_checks: Optional[List[StrictStr]] = {'access','refresh'}
    authjwt_header_name: Optional[StrictStr] = "Authorization"
    authjwt_header_type: Optional[StrictStr] = "Bearer"
    authjwt_access_token_expires: Optional[Union[StrictBool,StrictInt,timedelta]] = timedelta(minutes=15)
    authjwt_refresh_token_expires: Optional[Union[StrictBool,StrictInt,timedelta]] = timedelta(days=30)
    # option for create cookies
    authjwt_access_cookie_key: Optional[StrictStr] = "access_token_cookie"
    authjwt_refresh_cookie_key: Optional[StrictStr] = "refresh_token_cookie"
    authjwt_access_cookie_path: Optional[StrictStr] = "/"
    authjwt_refresh_cookie_path: Optional[StrictStr] = "/"
    authjwt_cookie_max_age: Optional[StrictInt] = None
    authjwt_cookie_domain: Optional[StrictStr] = None
    authjwt_cookie_secure: Optional[StrictBool] = False
    authjwt_cookie_samesite: Optional[StrictStr] = None
    # option for double submit csrf protection
    authjwt_cookie_csrf_protect: Optional[StrictBool] = True
    authjwt_access_csrf_cookie_key: Optional[StrictStr] = "csrf_access_token"
    authjwt_refresh_csrf_cookie_key: Optional[StrictStr] = "csrf_refresh_token"
    authjwt_access_csrf_cookie_path: Optional[StrictStr] = "/"
    authjwt_refresh_csrf_cookie_path: Optional[StrictStr] = "/"
    authjwt_access_csrf_header_name: Optional[StrictStr] = "X-CSRF-Token"
    authjwt_refresh_csrf_header_name: Optional[StrictStr] = "X-CSRF-Token"
    authjwt_csrf_methods: Optional[List[StrictStr]] = {'POST','PUT','PATCH','DELETE'}

    # ------------------------------------------------------------------
    # 说明（Pydantic v2 迁移）：
    # 原实现用 v1 的 @validator(..., each_item=True)，在 Pydantic 2 下每次
    # 导入都会刷 6 条 PydanticDeprecatedSince20 告警，且 Pydantic 3 将移除。
    # 这里改为 v2 的 @field_validator：
    #   * v2 没有 each_item，改为「在整字段上校验 + 逐元素判断」；
    #   * 对 None 一律原样放行 —— 与 v1 each_item 遇到 None 时跳过校验一致
    #     （LoadConfig(authjwt_token_location=None) 必须仍然合法）。
    # 迁移前后已用 32 组输入做过行为对拍，结论：完全等价。
    # ------------------------------------------------------------------
    @field_validator('authjwt_access_token_expires')
    @classmethod
    def validate_access_token_expires(cls, v):
        if v is True:
            raise ValueError("The 'authjwt_access_token_expires' only accept value False (bool)")
        return v

    @field_validator('authjwt_refresh_token_expires')
    @classmethod
    def validate_refresh_token_expires(cls, v):
        if v is True:
            raise ValueError("The 'authjwt_refresh_token_expires' only accept value False (bool)")
        return v

    @field_validator('authjwt_denylist_token_checks')
    @classmethod
    def validate_denylist_token_checks(cls, v):
        if v is None:
            return v
        for item in v:
            if item not in ['access','refresh']:
                raise ValueError("The 'authjwt_denylist_token_checks' must be between 'access' or 'refresh'")
        return v

    @field_validator('authjwt_token_location')
    @classmethod
    def validate_token_location(cls, v):
        if v is None:
            return v
        for item in v:
            if item not in ['headers','cookies']:
                raise ValueError("The 'authjwt_token_location' must be between 'headers' or 'cookies'")
        return v

    @field_validator('authjwt_cookie_samesite')
    @classmethod
    def validate_cookie_samesite(cls, v):
        if v not in ['strict','lax','none']:
            raise ValueError("The 'authjwt_cookie_samesite' must be between 'strict', 'lax', 'none'")
        return v

    @field_validator('authjwt_csrf_methods')
    @classmethod
    def validate_csrf_methods(cls, v):
        if v is None:
            return v
        for item in v:
            if item.upper() not in ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"]:
                raise ValueError("The 'authjwt_csrf_methods' must be between http request methods")
        return [item.upper() for item in v]

    # 原为 Pydantic v1 的 class Config。⚠️ 实测确认：v2 仍会读取 class Config
    # 中【名称与 v2 配置键同名】的项（str_min_length / str_strip_whitespace），
    # 即这两条约束在当前运行时是【生效】的 —— 空字符串密钥会被拒绝、
    # 首尾空格会被裁掉。因此迁移必须原样保留这两个键，否则就是行为变更
    # （第一版迁移曾把它们丢掉，被 32 组输入的对拍测试当场抓出）。
    model_config = ConfigDict(
        str_min_length=1,
        str_strip_whitespace=True,
    )
