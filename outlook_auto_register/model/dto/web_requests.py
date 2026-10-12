"""Web API request DTOs."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from outlook_auto_register.config.constants import MAIL_TOKEN_MODE as DEFAULT_TOKEN_MODE

class RegisterRequest(BaseModel):
    count: int = 1
    concurrency: int = 2
    prefix: Optional[str] = None
    domain: str = "@outlook.com"
    country: str = "US"
    proxy: Optional[str] = None
    px_mode: str = "solver"
    skip_login: bool = False
    no_mail_token: bool = False
    captcha_key: Optional[str] = None
    token_mode: str = DEFAULT_TOKEN_MODE
    dry_run: bool = False
    # 防封·启动错峰（秒）：留空则用引擎默认（3~8）/ 环境变量 OUTLOOK_REG_JITTER_MIN/MAX
    jitter_min: Optional[float] = None
    jitter_max: Optional[float] = None
    batch_label: Optional[str] = None  # 留空则按 日期-国家-域名-数量-格式 自动生成
    use_proxy_pool: bool = False
    captcha_provider: Optional[str] = None
    proxy_selection: Optional[str] = None
    proxy_provider: Optional[str] = None
    # 执行路线：公共参数由此请求承载，具体执行器在 worker 内分派。
    execution_route: str = "protocol"
    roxy_profile_id: Optional[str] = None
    bitbrowser_profile_id: Optional[str] = None



class ProxyPoolAddRequest(BaseModel):
    templates: list[str] = []
    text: Optional[str] = None  # 多行批量导入
    label: Optional[str] = None
    provider: Optional[str] = None  # 代理商/供应商分组
    country: Optional[str] = None  # 代理出口国家 ISO2，留空则从模板推断



class ProxyPoolUpdateRequest(BaseModel):
    label: Optional[str] = None
    template: Optional[str] = None
    provider: Optional[str] = None
    country: Optional[str] = None
    enabled: Optional[bool] = None



class ProxyPoolDeleteRequest(BaseModel):
    ids: list[str]



class ProxyPoolCheckRequest(BaseModel):
    ids: Optional[list[str]] = None
    timeout: int = 15



class ProxyPoolSettingsRequest(BaseModel):
    strategy: Optional[str] = None
    require_healthy: Optional[bool] = None
    sticky_per_account: Optional[bool] = None



class ProxyPoolBindRequest(BaseModel):
    email: str
    proxy_id: str



class ProxyPoolUnbindRequest(BaseModel):
    emails: list[str]



class ProxyPoolEnsureRequest(BaseModel):
    templates: list[str] = []
    text: Optional[str] = None
    provider: Optional[str] = "web"
    country: Optional[str] = None



class VerifyComboRequest(BaseModel):
    combo: Optional[str] = None
    email: Optional[str] = None
    refresh_token: Optional[str] = None
    proxy: Optional[str] = None
    test_imap: bool = True



class VerifyBatchRequest(BaseModel):
    emails: Optional[list[str]] = None  # 指定账号邮箱；空=全部有 token 的
    combos: Optional[list[str]] = None  # 或直接传 combo 列表
    proxy: Optional[str] = None
    test_imap: bool = False
    concurrency: int = 4



class ImportRequest(BaseModel):
    text: str = ""



class ExportRequest(BaseModel):
    emails: Optional[list[str]] = None
    format: str = "graph"



class DeleteRequest(BaseModel):
    emails: list[str]



class MetaRequest(BaseModel):
    email: str
    note: Optional[str] = None
    tags: Optional[list[str]] = None



class KeepaliveRequest(BaseModel):
    emails: Optional[list[str]] = None  # 选中账号邮箱；空=全部有 token 的
    proxy: Optional[str] = None
    concurrency: int = 5



class RescueRequest(BaseModel):
    emails: Optional[list[str]] = None
    proxy: Optional[str] = None
    proxy_selection: Optional[str] = None
    concurrency: int = 1
    use_proxy_pool: bool = False



class ReplenishRequest(BaseModel):
    emails: Optional[list[str]] = None
    proxy: Optional[str] = None
    verify: bool = True



class SettingsRequest(BaseModel):
    captcha_run_api_key: Optional[str] = None
    ezcaptcha_api_key: Optional[str] = None
    capsolver_api_key: Optional[str] = None
    offcaptcha_api_key: Optional[str] = None
    offcaptcha_soft_id: Optional[str] = None
    default_captcha_provider: Optional[str] = None


