from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SignupSession:
    """一次注册会话的上下文。"""

    uaid: str
    signup_url: str
    signup_page_url: str
    cobrandid: str
    contextid: str
    opid: str
    bk: str
    sru: str
    canary: str
    hpgid: int = 200225
    scid: int = 100118
    server_data: dict[str, Any] = field(default_factory=dict)
    telemetry_context: str = ""
    continuation_token: str = ""
    human_sensor_url: str = ""
    px_challenge_meta: dict[str, Any] = field(default_factory=dict)
    # captcha.run 官方：单次 POST 建 task，GET silent→press 共用 taskId
    captcha_run_task: Any = field(default=None, repr=False)
    code_verifier: str = ""
    code_challenge: str = ""
    oauth_redirect_uri: str = ""
    mkt: str = "EN-US"
    lc: str = "1033"
    signup_query: str = ""
    # ServerData-derived endpoints/identifiers used by the z-style pre-create flow.
    risk_initialize_url: str = ""
    risk_verify_url: str = ""
    site_id: str = ""
    px_app_id: str = ""
    px_session_id: str = ""
    px_fpt_url: str = ""
    px_press_target: str = ""

    def common_query_params(self) -> dict[str, str]:
        return {
            "cobrandid": self.cobrandid,
            "contextid": self.contextid,
            "opid": self.opid,
            "bk": self.bk,
            "sru": self.sru,
            "lw": "dob,flname,wld",
            "fl": "1",
            "uiflavor": "web",
            "fluent": "2",
            "client_id": "00000000487A244A",
            "lic": "1",
            "mkt": self.mkt,
            "lc": self.lc,
            "uaid": self.uaid,
        }


@dataclass
class AccountInfo:
    email: str
    password: str
    first_name: str
    last_name: str
    country: str
    birth_date: str  # DD:MM:YYYY


@dataclass
class RegisterResult:
    success: bool
    email: str = ""
    password: str = ""
    error: str = ""
    redirect_url: str = ""
    slt: str = ""
    refresh_token: str = ""
    client_id: str = ""
    # 双令牌 token#2（登录授权 / 第三方 SSO）
    login_client_id: str = ""
    login_refresh_token: str = ""
    recovery_email: str = ""
    recovery_password: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_combo(self, *, dual: bool = False, recovery: bool = False) -> str:
        """输出四段 / 六段 combo。

        - 四段（默认）: email----password----client_id----refresh_token
        - 六段 recovery（login.exe）: email----pwd----cid----rt----recovery_email----recovery_pwd
        - 六段 dual: email----pwd----cid----graph_rt----login_cid----login_rt
        """
        if recovery and self.recovery_email and self.recovery_password:
            return "----".join([
                self.email,
                self.password,
                self.client_id,
                self.refresh_token,
                self.recovery_email,
                self.recovery_password,
            ])
        if dual and self.login_refresh_token:
            return "----".join([
                self.email,
                self.password,
                self.client_id,
                self.refresh_token,
                self.login_client_id,
                self.login_refresh_token,
            ])
        return "----".join([
            self.email,
            self.password,
            self.client_id,
            self.refresh_token,
        ])

    def product_combo(self, mode: str = "") -> str:
        """按产出格式选 combo：graph=四段，graph_recovery/login_exe=六段，dual=双令牌六段。"""
        from outlook_auto_register.config.constants import is_dual_mode, is_graph_recovery_mode, is_recovery_mode, normalize_token_mode

        m = normalize_token_mode(mode)
        if is_graph_recovery_mode(m):
            return self.to_combo(recovery=True) if self.recovery_email else self.to_combo()
        if is_recovery_mode(m):
            return self.to_combo(recovery=True)
        if is_dual_mode(m):
            return self.to_combo(dual=True)
        return self.to_combo()
