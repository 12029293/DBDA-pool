"""dola-pool 配置：全部走环境变量，带默认值。"""
import os
from pathlib import Path


def _load_local_env():
    """Load ignored .env.local for local/tunnel runs; real environment wins."""
    path = Path(__file__).with_name(".env.local")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()
# 监听地址由 uvicorn 命令行给（--host/--port）；这里的 PORT 只用于 PUBLIC_BASE 的默认值。
PORT = int(os.getenv("DOLA_PORT", "8000"))

# 服务对外 API Key（逗号分隔多个；留空 = 不鉴权，仅内网调试用）
API_KEYS = [k.strip() for k in os.getenv("DOLA_API_KEYS", "").split(",") if k.strip()]

# 同时跑的视频任务上限（0 = 不限制，默认）。这是全局唯一一道并发闸门。
# 默认不限：任务提交后立即开跑，等待只可能来自「没有空闲账号」。
# 设正数可防风控 / 防同号并发，但会限制吞吐。
MAX_CONCURRENCY = int(os.getenv("DOLA_MAX_CONCURRENCY", "0"))

# 全局待处理任务上限（queued + processing），0 = 不限制。
MAX_PENDING_TASKS = int(os.getenv("DOLA_MAX_PENDING_TASKS", "100"))

# 视频生成超时（秒）
VIDEO_TIMEOUT = int(os.getenv("DOLA_VIDEO_TIMEOUT", "300"))
# 单个任务的总时限（秒）：在此时间内持续换号调度，超时即判定生成失败。
TASK_DEADLINE = int(os.getenv("DOLA_TASK_DEADLINE", "3600"))
# 单个任务最多尝试的账号次数；0 表示不限次数，只受 TASK_DEADLINE 约束。
VIDEO_MAX_ATTEMPTS = int(os.getenv("DOLA_VIDEO_MAX_ATTEMPTS", "0"))

# SQLite 任务库
DB_PATH = os.getenv("DOLA_DB_PATH", "tasks.db")

# 号池配额/账号元数据库（代理记录也放这里）
POOL_DB_PATH = os.getenv("DOLA_POOL_DB_PATH", "pool_usage.db")

# 视频下载目录（出片后下载转存，FastAPI 以静态文件方式对外提供）
DOWNLOAD_DIR = os.getenv("DOLA_DOWNLOAD_DIR", "downloads")

# ============================================================================
# 【三段出口策略 · hint 830 / i831 落地】用户明确要求按用途分三段出口：
#   ① 注册段  send_code / register_verify_login / login_check / 临时邮箱
#             -> 本机 Clash HTTP 代理  http://127.0.0.1:7897   (REGISTER_PROXY 默认值)
#                直连会被 1206 限流；127.0.0.1:7890 本机无监听 → ProxyError
#                /「Max retries exceeded」，严禁使用。
#   ② 提交段  POST /chat/completion(ability_type=17) + 窗口嗅探
#             -> iprocket 住宅 socks5h（本机无 pysocks，走 i529/i728/i749 的裸 socket
#                SOCKS5 shim；故 config.PROXY 置空=requests 层不加代理，由 shim 隧道
#                www.dola.com）。iprocket 同池混好坏端点，CONNECT reply code=1 时换端点重试。
#   ③ 下载段  get_play_info 的 CDN 直链(v16-dola.dola.com) -> 本地直连(proxy="")。
# SUBMIT_EXIT 取值：residential(默认) | direct | http://host:port
# 兼容旧行为时把 DOLA_SUBMIT_EXIT 设成旧 PROXY 值即可回退到单一出口。
# ============================================================================

# 浏览器显式代理（P0 结论：不能依赖系统代理，Clash 规则变化会把 dola 分流到直连被墙）。
# 【2026-10-15 修正】旧默认 http://127.0.0.1:7890 本机无监听，是「chat/completion request
# failed after 4 attempts: Max retries exceeded」「发码失败 [None]」的根因；改为三段策略下
# 提交段默认走住宅 socks5h，此处留空表示 requests 层不加代理（由 three_segment_exit 的
# 裸 socket SOCKS5 shim 隧道 www.dola.com）。
PROXY = os.getenv("DOLA_PROXY", "")

# 注册专用代理（register_service）：注册只需要非大陆出口，与出片的住宅要求解耦。
# 【2026-10-15 修正】默认改为本机 Clash 7897（已实测通；旧空值+直连会被 1206 限流）。
REGISTER_PROXY = os.getenv("DOLA_REGISTER_PROXY", "http://127.0.0.1:7897")

# 提交段出口策略（三段策略的中间段）：residential=iprocket 住宅 socks5h(默认)；
# direct=直连（仅调试）；http(s)://host:port=显式 http 代理（回退/对照）。
SUBMIT_EXIT = os.getenv("DOLA_SUBMIT_EXIT", "residential")

# 提交段经住宅出口时被隧道的 host 白名单（只有这些走住宅；CDN 等其它 host 自动直连）。
# 保持 CDN(v16-dola.dola.com) 不在列表内 = 下载段本地直连（hint 830 ③）。
#
# 【IPRocket 流量治理 · 2026-10-17】住宅隧道按 GB 计费，而 shim 的拦截粒度是
# hostname：只要 host 在名单里，那个 host 上的**一切**（API + 任何大对象）都会走
# 住宅隧道，proxies={"http": None} 也绕不开（shim 在 socket 层）。所以这份名单必须
# 是**最小 API 白名单**，绝不能把下载/CDN 域名放进来。
# 实测：出片轮询/提交都在 www.dola.com，成片直链在 CDN 子域，二者本就分离。
SUBMIT_TUNNEL = [h.strip() for h in
                 os.getenv("DOLA_SUBMIT_TUNNEL", "www.dola.com").split(",") if h.strip()]

# 隧道**排除名单**（优先级高于 SUBMIT_TUNNEL）：即使主机被上面列进来，命中这里
# 也强制走本机直连。用来兜住「CDN 恰好挂在 www.dola.com 上」的情况（例如 /video/
# 播放中转、大图大文件），避免成片体积混进住宅流量。
# 留空 = 关闭排除逻辑（回到纯 hostname 精确匹配的旧行为）。
SUBMIT_TUNNEL_EXCLUDE = [h.strip() for h in
                         os.getenv("DOLA_SUBMIT_TUNNEL_EXCLUDE",
                                   "v16-dola.dola.com,v26-vdl.dola.com").split(",")
                         if h.strip()]
# ★ 必须回写环境变量：three_segment_exit.apply() 是从 os.environ 读这份名单的
#   （协议侧的 shim 不 import config）。不回写的话上面这个默认值形同虚设 ——
#   排除逻辑永远不会生效，CDN 大对象照旧走住宅隧道。
os.environ.setdefault("DOLA_SUBMIT_TUNNEL_EXCLUDE", ",".join(SUBMIT_TUNNEL_EXCLUDE))

# 视频文件下载走本地直连（2026-10-07）：成片/分段下载是最大流量项，不再消耗
# IPRocket 代理流量；Dola CDN（Akamai）直连可下载（纯 API 主路径一直是直连）。
# 设 DOLA_DOWNLOAD_DIRECT=0 可恢复「下载走账号代理」的旧行为。
DOWNLOAD_DIRECT = os.getenv("DOLA_DOWNLOAD_DIRECT", "1") not in ("0", "false", "False")

# ============================================================================
# 【IPRocket 流量治理 · 2026-10-17】—— 下面四个开关都只影响「走多少字节」，
# 不改变任何出片协议语义。实测数据（.venv 直连 Clash 测线上 gzip 字节）：
#   GET  /chat/create-image     77.2 KB   只为拿 ttwid（HEAD 0 字节等价）
#   POST /alice/user/launch     47.5 KB   纯拉活，A/B 3:3 可省
#   POST /im/chain/single        3.85 KB  单次轮询
#   POST /chat/completion          19 KB  提交（嗅探器 6s 一发）
# ============================================================================

# ① 会话预热改用 HEAD（0 字节）而不是 GET 整个 SPA（压缩后 77 KB）。
#    warmup_login_session 的唯一目的是让上游种下 ttwid / hook_slardar_session_id，
#    HEAD 同样会收到 Set-Cookie。实测（_diag_headwarm.py）：HEAD 与 GET 后
#    x-tt-agw-login 都是 '1'，anon/launch/链路查询全部正常。
#    设 0 可回退成 GET 全量（只在怀疑上游对 HEAD 有额外校验时用）。
WARMUP_HEAD = os.getenv("DOLA_WARMUP_HEAD", "1") == "1"

# ② 建 session 时跳过 /alice/user/launch（每个号省 47.5 KB）。
#    实测（_diag_launch2.py，各 3 轮）：带 launch 与省 launch 的
#    x-tt-agw-login 均为 '1'、最近会话都是 6 条、/im/chain/single 查询全通。
#    设 0 恢复「建 session 必打 launch」（怀疑上游改了拉活时序时用）。
SKIP_LAUNCH = os.getenv("DOLA_SKIP_LAUNCH", "1") == "1"

# ③ 出片轮询间隔（秒）。这是单任务流量的**主导项**：
#    3.85 KB/次 × (等待时长/间隔)。
#    30s 默认值时：等 60min = 120 次 ≈ 0.45 MB；等 120min ≈ 0.90 MB。
#    改成 120s：等 60min = 30 次 ≈ 0.11 MB（省 76%）。
#    上游出片常在分钟级到小时级，120s 的探测精度对「拿到成片」没有实质影响，
#    只是晚 1~2 分钟发现。设回 30 可恢复旧行为。
POLL_INTERVAL = int(os.getenv("DOLA_PURE_POLL_INTERVAL", "120"))

# ④ 窗口嗅探器周期（秒）。实测 logs/dola_window.jsonl：
#    8237 次 submit / 18.3h、中位间隔 6.0s → 每次提交约 19 KB，
#    6s 周期 = 约 11 MB/h，一天 260 MB **全部花在「反复确认窗口关着」上**。
#    710022002 是上游全局窗口（15~23 窗/h、每窗 2~12s），60s 周期仍能
#    每小时命中十几次窗口；配合 worker 自己撞到 710 时 note_closed()，
#    漏窗概率极低。设回 6 恢复旧行为。
WINDOW_SNIFF_PERIOD_SEC = float(os.getenv("DOLA_WINDOW_SNIFF_PERIOD_SEC", "60"))
# 浏览器是否无头运行（login.py 永远有头）
HEADLESS = os.getenv("DOLA_HEADLESS", "1") == "1"

# 对外返回视频 URL 的基址（FastAPI 静态文件服务）
PUBLIC_BASE = os.getenv("DOLA_PUBLIC_BASE", f"http://127.0.0.1:{PORT}")

# 管理面板密码（留空 = 面板不鉴权，开发模式）
ADMIN_KEY = os.getenv("DOLA_ADMIN_KEY", "")


# Dola 30 秒/无水印 Chromium 扩展（unpacked extension）
EXTENSION_DIR = os.getenv("DOLA_EXTENSION_DIR", "extensions/dola30")
EXTENSION_ENABLED = os.getenv("DOLA_EXTENSION_ENABLED", "1") == "1"

# Cookie 导入账号启动时使用 Windows 11 Edge 126 的 UA/启动参数伪装
EDGE_FINGERPRINT_ENABLED = os.getenv("DOLA_EDGE_FINGERPRINT", "1") == "1"

# 「验证」按钮对 Google 登录账号追加的聊天探活（账号声誉/风控预检）：
# 发送一句问候，看是否正常回复，还是发送后立刻被踢成未登录。
VERIFY_CHAT_PROBE = os.getenv("DOLA_VERIFY_CHAT_PROBE", "1") == "1"
VERIFY_CHAT_PROMPT = os.getenv("DOLA_VERIFY_CHAT_PROMPT", "你好")
VERIFY_CHAT_WINDOW = int(os.getenv("DOLA_VERIFY_CHAT_WINDOW", "30"))

# Dola 每日限流恢复时区；默认按日本时间恢复。
LIMIT_RESET_TZ = os.getenv("DOLA_LIMIT_RESET_TZ", "Asia/Tokyo")

# Dola 每日免费额度刷新的钟点（LIMIT_RESET_TZ 时区的小时，默认日本时间 00:00）。
# 号池的「额度日」计数与每日自动重置都按这个时点滚动。
# 2026-09-15 修正：原先按 11:00 记界，实测上游不是 11:00 刷新（ck05 于 10:37 JST 用满 4 点，
# 13:31 JST 仍回「今天的生成次数已经达到上限」），改按当地零点对齐；
# 更权威的口径是上游回执里的「今日剩余 N 个视频生成额度」，会实时回写到账号额度上。
LIMIT_RESET_HOUR = int(os.getenv("DOLA_LIMIT_RESET_HOUR", "0"))

# 生成前余额预检的保守最低积分；Dola 当前 2.5/30s 实测成本为 2。
VIDEO_REQUIRED_POINTS = int(os.getenv("DOLA_VIDEO_REQUIRED_POINTS", "2"))

# 30 秒要「一气呵成」：默认**不**把两段 15 秒拼成 30 秒。
# 上游只给短视频（例如 30 秒请求只回 15 秒）时按失败处理、换号重试，
# 直到拿到原生 30 秒成片（实测 ck1001/ck1002/ck1008 都是单次请求直接出 30.09s）。
# 需要恢复旧的「两段拼接」兜底时，把 DOLA_ALLOW_30S_PAIR 设为 1。
ALLOW_30S_PAIR = os.getenv("DOLA_ALLOW_30S_PAIR", "0") == "1"

# ---- 30 秒出片通道（2026-10-06 定案）----
# 30 秒**只能走真实浏览器 + hook30**：把页面本来发的 ability_param.duration 从默认档
# 改写成 30，服务端照单全收且按「一单 2 额度」计费（见桌面《Dola 30秒时长解锁与自动化
# 测试文档.md》）。纯 API 路径 2026-10-06 实测已被上游按 8/10 秒→8 点、15 秒→12 点报价
# 拒单（免费号只有 2 点），30 秒更是直接回「只支持 4 到 15 秒」。
#   pure          = 30 秒走纯 API（2026-10-07 实测上游回执 2 点且稳定，**当前默认**，
#                   且纯 API 无浏览器窗口，大批量 newAPI 任务不占桌面资源）。
#   browser（旧默认）= 30 秒走 browser30.generate_video_30（真实浏览器通道，每任务
#                   弹一个 Chromium 窗口；如需切回：设 DOLA_30S_CHANNEL=browser）。
CHANNEL_30S = os.getenv("DOLA_30S_CHANNEL", "pure").strip().lower()
USE_BROWSER_30S = CHANNEL_30S != "pure"
# 浏览器 30s 等片上限：上游排队常态 15~60 分钟，短了会白扔已扣的额度。
BROWSER_30S_MAX_WAIT = int(os.getenv("DOLA_BROWSER_30S_MAX_WAIT", "2400"))
# ---- 短时长也走浏览器通道（2026-10-07 拍板）----
# 4~29s（含 15s）默认走纯 API 时会被上游按真实档报价拒单（15s 报 12 点 > 免费号 4 点）。
# 开启后这些时长改走 browser30（hook 把页面请求的 duration 改写成目标值，
# 服务端按页面默认档计价 = 一单 2 点，与 30s 浏览器通道同机制）。
# 30s 不受本开关影响，仍按 DOLA_30S_CHANNEL 路由（当前 pure，实测回执 2 点且稳定）。
BROWSER_SHORTS = os.getenv("DOLA_BROWSER_SHORTS", "0") == "1"

# 每账号每日视频生成额度（点数；官方每日免费额度随账号/区域浮动，超出后 Dola 会返回每日上限错误自动停号）
DAILY_LIMIT = int(os.getenv("DOLA_DAILY_LIMIT", "4"))

# ---- 模型额度规则（2026-09-22 对齐 dola-pool-cookie：只看模型，不看时长）----
# 每次出片消耗的点数**只看模型**，与时长无关；账号每日共 DAILY_LIMIT 点（默认 4 点）。
#   seedance-2.5 一条视频 = 2 点 → 一个号一天能出 2 条；
#   seedance-2.0 一条视频 = 3 点 → 一个号一天能出 1 条（剩 1 点不够第二条）。
# 未知模型按最贵的 DEFAULT_CREDIT_COST 算：宁可少派，也不要拿号去白撞一次
# 「额度不足」（与 dola-pool-cookie 的 credit_cost_for_model 同规则）。
# 注意：上游回执里的「将消耗 N 个视频生成额度」是**报价**，与实际扣点并不一致
# （30 秒报价 6 点、实扣 2 点；2.5 的 10 秒档报价 4 点、实测也确实扣满一天 4 点），
# 所以本表是记账口径；上游真实余额由回执里的「今日剩余 N 个」实时回写校正
# （见 browser_pool._set_credit_balance），两者配合不会把号用超。
MODEL_COSTS = {
    "seedance-2.5": 2,
    "seedance-2.0": 3,
}
DEFAULT_CREDIT_COST = 3

# seedance-2.0 只认这三个原生档位；其余（含 30）都必须走 2.5。
V20_DURATIONS = (5, 10, 15)

# ---- 时长白名单 ----
# 原生档位：上游 UI 直出的四个选项，**任何情况下都放行**。30 秒是线上主力流量，
# 绝不能被下面的上限夹掉（夹了就是把 30 秒片悄悄变成 15 秒片）。
NATIVE_DURATIONS = (5, 10, 15, 30)

# 任意时长（非原生档位）上限：0 = 关闭（非原生时长一律拒绝，保持旧行为）。
# >0 时允许 [MIN_DURATION, NATIVE_DURATION_MAX] 区间的非原生时长，且只能走
# seedance-2.5 —— 4~30 是 2.5 的能力；扩展实测就是改 ability_param.duration
# 并把 model 钉在 seedance_v2.5。
# 上游对号池回的话术是「4到15秒 / 超出了单条生成范围」，所以真实上限必须在线上一
# 档一档试出来：15 → 20 → 30。做成环境变量是为了不必每次重建。
MIN_DURATION = 4
NATIVE_DURATION_MAX = int(os.getenv("DOLA_NATIVE_DURATION_MAX", "0"))


def all_durations() -> list[int]:
    """当前允许下发的时长全集（原生档位永远在内）。"""
    if NATIVE_DURATION_MAX <= 0:
        return sorted(NATIVE_DURATIONS)
    return sorted(set(NATIVE_DURATIONS) | set(range(MIN_DURATION, NATIVE_DURATION_MAX + 1)))


ALL_DURATIONS = all_durations()


# 公网参考图片下载限制
REFERENCE_IMAGE_MAX_BYTES = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_BYTES", str(15 * 1024 * 1024)))
REFERENCE_DOWNLOAD_TIMEOUT = int(os.getenv("DOLA_REFERENCE_DOWNLOAD_TIMEOUT", "60"))
REFERENCE_IMAGE_MAX_COUNT = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_COUNT", "30"))
# 内联参考图（data: URL）落盘目录：几 MB 的 base64 绝不写进 tasks.db（会把库撑到几百 MB）
REFERENCE_FILE_DIR = os.getenv("DOLA_REFERENCE_FILE_DIR", "refs")

# 参考图缩略图目录与规格（面板回看用）。
# 任务走到终态时原参考图会被清掉（落盘文件删除 / 临时目录 rmtree），所以要另存一份小图，
# 否则面板里"参考图"永远是空的。默认只存第一张，避免大规模号池把磁盘吃满。
THUMB_DIR = os.getenv("DOLA_THUMB_DIR", "thumbs")
REFERENCE_THUMB_COUNT = int(os.getenv("DOLA_REFERENCE_THUMB_COUNT", "1"))
REFERENCE_THUMB_MAX_PX = int(os.getenv("DOLA_REFERENCE_THUMB_MAX_PX", "160"))
REFERENCE_THUMB_QUALITY = int(os.getenv("DOLA_REFERENCE_THUMB_QUALITY", "80"))
# 单次请求参考图总量上限（落盘后的字节数）
REFERENCE_TOTAL_MAX_BYTES = int(
    os.getenv("DOLA_REFERENCE_TOTAL_MAX_BYTES", str(60 * 1024 * 1024))
)
# ===== 素材库（创作台 · 参考素材库 + 生成产物入库）=====
# 为什么要独立目录：REFERENCE_FILE_DIR（refs/）里的文件是**临时物化产物**，
# 任务终态会被 cleanup_local_references 删除、服务启动会被 sweep_stale_references
# 清掉 24h 以上的文件。素材是用户的长期资产，必须放在**永不自动删**的目录里。
# 详见 media.is_asset_reference() 与方案文档 §3.1。
ASSET_DIR = os.getenv("DOLA_ASSET_DIR", "assets")
ASSET_DB_PATH = os.getenv("DOLA_ASSET_DB_PATH", "assets.db")
# 预览图：统一 512px（不能复用 REFERENCE_THUMB_* —— 那套只存 1 张、160px）
ASSET_THUMB_DIR = os.getenv("DOLA_ASSET_THUMB_DIR", os.path.join(ASSET_DIR, ".thumbs"))
ASSET_THUMB_MAX_PX = int(os.getenv("DOLA_ASSET_THUMB_MAX_PX", "512"))
ASSET_THUMB_QUALITY = int(os.getenv("DOLA_ASSET_THUMB_QUALITY", "82"))
# 上传校验：与 media._ALLOWED_IMAGE_FORMATS / server._save_uploaded_image 对齐
ASSET_UPLOAD_MAX_BYTES = int(
    os.getenv("DOLA_ASSET_UPLOAD_MAX_BYTES", str(15 * 1024 * 1024))
)
ASSET_UPLOAD_MAX_COUNT = int(os.getenv("DOLA_ASSET_UPLOAD_MAX_COUNT", "30"))
# 素材总量上限：超限**拒绝新上传并提示**，绝不自动删用户素材
ASSET_MAX_BYTES = int(os.getenv("DOLA_ASSET_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
# 按内容 sha256 去重（同一张图重复上传只占一份）
ASSET_DEDUP = os.getenv("DOLA_ASSET_DEDUP", "1") == "1"

# 单条 prompt 上限（超出直接 422；store 里还有一层截断兜底）
MAX_PROMPT_CHARS = int(os.getenv("DOLA_MAX_PROMPT_CHARS", "20000"))
# 已结束任务的保留天数：超期自动删记录 + 删对应视频文件（0 = 不清理）
TASK_RETENTION_DAYS = float(os.getenv("DOLA_TASK_RETENTION_DAYS", "14"))
# 出片文件目录体积上限（超过就按时间从旧到新删）
DOWNLOAD_MAX_BYTES = int(os.getenv("DOLA_DOWNLOAD_MAX_BYTES", str(20 * 1024 * 1024 * 1024)))
# 排队进度预估：各档位出片平均耗时（秒）兜底值（样本够多时用近 3 天实测均值）
ETA_FALLBACK_SECONDS = {5: 130, 10: 260, 15: 390, 30: 560}
ETA_DEFAULT_SECONDS = int(os.getenv("DOLA_ETA_DEFAULT_SECONDS", "420"))
# 排队等待上限：还没提交到上游（没有 conversation）且等了这么久 → 明确失败，别让客户端悬着
MAX_QUEUE_WAIT_SECONDS = int(os.getenv("DOLA_MAX_QUEUE_WAIT_SECONDS", "1800"))
# 看门狗的「没进展」判定：processing 任务超过这么久没有任何轮询更新 → 重新派发/回收
STALE_TASK_SECONDS = int(os.getenv("DOLA_STALE_TASK_SECONDS", "600"))
# 看门狗巡检间隔（秒）
WATCHDOG_INTERVAL_SECONDS = int(os.getenv("DOLA_WATCHDOG_INTERVAL_SECONDS", "60"))

# 带参考图片的任务给 Dola 更长的异步生成窗口（秒）。
REFERENCE_VIDEO_TIMEOUT = int(os.getenv("DOLA_REFERENCE_VIDEO_TIMEOUT", "900"))

# 参考图上传要打金山 CDN（imagex-*.bytevcloudapi.com），实测部分代理出口对该域名
# CONNECT 直接 403。1 = 上传段遇代理拦截时只用直连重传一次（生成仍走账号代理）。
REFERENCE_UPLOAD_DIRECT_FALLBACK = os.getenv("DOLA_REFERENCE_UPLOAD_DIRECT_FALLBACK", "1") == "1"

# ===== 号池工程化增强（移植自 api-pool，默认关闭/不破坏现有行为）=====

# 同出口隔离：同一代理出口（host:port）同时只允许一个号提交。
# 多个号共用同一个旋转网关时建议开 true，避免风控把同一出口并发送勤打断。
ISOLATE_SHARED_EGRESS = os.getenv("DOLA_ISOLATE_SHARED_EGRESS", "0") == "1"

# 连续失败分达到该值则冷却该号；0 = 不按失败分冷却。
FAIL_SCORE_CAP = int(os.getenv("DOLA_FAIL_SCORE_CAP", "0"))

# 【已废弃 2.1.0 P3】上游瞬时限流（710022002）的冷却时长。
# 「限流」概念已按拍板删除：这类拒绝现在只换号重试，不冷却、不标状态。
# 变量保留只为兼容旧 run.sh，不再被任何代码读取。
TRANSIENT_COOLDOWN_SEC = int(os.getenv("DOLA_TRANSIENT_COOLDOWN_SEC", "600"))

# 【异常】组的自动恢复时长（秒）：5 分钟无回执 / 连续失败 3 次 / profile 缺失时进组，
# 到期自动恢复。
ABNORMAL_COOLDOWN_SEC = int(os.getenv("DOLA_ABNORMAL_COOLDOWN_SEC", "1800"))

# 派发后「上游必须给出任何回应」的时限（秒）：超时且一句回执都没有 → 该号进【异常】组，
# 任务报「生视频过程中出现异常情况，请重试」。按拍板取 5 分钟。
NO_ACK_SECONDS = int(os.getenv("DOLA_NO_ACK_SECONDS", "300"))

# ---- 「你好」风控探测（2026-09-23 P4）----
# 派发前对该号发一句「你好」：被登出（x-tt-agw-login != 1）或没有回复 → 判风控。
HELLO_PROBE_PROMPT = os.getenv("DOLA_HELLO_PROBE_PROMPT", "你好")
# 同号探测结果的复用时长（秒）：0 = 每个任务都真探（更严格，但每个任务多花 ~3 秒，
# 且要和出片抢同一条上游提交通道）。
HELLO_PROBE_CACHE_SECONDS = int(os.getenv("DOLA_HELLO_PROBE_CACHE_SECONDS", "300"))
# 单次「你好」等待回复的上限（秒）。
HELLO_PROBE_TIMEOUT = int(os.getenv("DOLA_HELLO_PROBE_TIMEOUT", "60"))
# 探测通过后、真正提交出片前的间隔（秒）：探测和出片走同一条上游提交通道，
# 挨着发容易被上游判「访问频繁」(710022002)，所以留出提交间隔（0 = 不等，最激进）。
HELLO_PROBE_SUBMIT_GAP_SECONDS = int(os.getenv("DOLA_HELLO_PROBE_SUBMIT_GAP_SECONDS", "15"))

# 同一号两次向上游提交的最小间隔（秒）；0 = 不限制。
MIN_SUBMIT_INTERVAL_SECONDS = int(os.getenv("DOLA_MIN_SUBMIT_INTERVAL_SECONDS", "0"))

# ===== 窗口嗅探 + 整池爆发（2026-10-15，goal 612 渠道改造）=====
# 核心事实：710022002「目前服務使用量過大」不是「封禁/换号可解」的瞬时限流，而是
# 【单一全局网关周期性开窗】——实测 ≈15~23 窗口/h、每次约 2~12s，窗口开时整池同刻放行。
# 因此 710022002 的正确处置是【交回全局窗口嗅探器排队】，而不是换号重试。
# 1 = 启用窗口嗅探引擎（dola_window.py）；0 = 退回旧的「撞 710 换号重试」行为。
WINDOW_SNIFF_ENABLED = os.getenv("DOLA_WINDOW_SNIFF", "1") == "1"
# 哨兵探测周期（秒）：一个探针号按此周期打一发 /chat/completion 探全局闸门。
# 真源在文件上方「IPRocket 流量治理」段的 WINDOW_SNIFF_PERIOD_SEC（默认 60s）。
WINDOW_SNIFF_PERIOD_SEC = WINDOW_SNIFF_PERIOD_SEC
# 窗口命中后，整池爆发提交的最大并发（同时对多少个号发 POST /chat/completion）。
WINDOW_BURST_CONCURRENCY = int(os.getenv("DOLA_WINDOW_BURST_CONCURRENCY", "32"))
# 参与爆发的准入配额阈值：当日剩余额度 >= 该值的号才进池（避免耗尽号的无效空打）。
WINDOW_QUOTA_THRESHOLD = int(os.getenv("DOLA_WINDOW_QUOTA_THRESHOLD", "1"))
# 单任务「等窗口 + 提交」的整体时限（秒）：超时仍没命中窗口则如实返回 WINDOW_CLOSED。
WINDOW_WAIT_DEADLINE_SEC = int(os.getenv("DOLA_WINDOW_WAIT_DEADLINE_SEC", "1800"))
# 单次窗口提交的网络超时（秒）。
WINDOW_SUBMIT_TIMEOUT_SEC = int(os.getenv("DOLA_WINDOW_SUBMIT_TIMEOUT_SEC", "60"))
# 窗口引擎的事实记录（jsonl）路径；留空 = 写 <workdir>/logs/dola_window.jsonl。
WINDOW_REPORT_PATH = os.getenv("DOLA_WINDOW_REPORT_PATH", "")
# 改造后 710022002 不再进 TRANSIENT_THROTTLE_MARKERS 的「换号重试」，而交回窗口嗅探器。
WINDOW_TAKEOVER_710 = os.getenv("DOLA_WINDOW_TAKEOVER_710", "1") == "1"

# 账号级测试生成（test_generate）遇上游瞬时限流（710022002「当前服务访问频繁」）的重试。
# 号池路径遇 transient 换号重试；测试生成钉死指定账号，只能原地等待后重交。
# RETRIES=0 恢复旧行为（一次被拒即任务失败）。
TESTGEN_TRANSIENT_RETRIES = int(os.getenv("DOLA_TESTGEN_TRANSIENT_RETRIES", "2"))
TESTGEN_TRANSIENT_WAIT_SEC = int(os.getenv("DOLA_TESTGEN_TRANSIENT_WAIT_SEC", "60"))

# seedance-2.0（每条 3 点）只能用【满额】组账号；满额暂时用完时排队等待的上限（秒）。
# 0 = 不排队，立刻失败。
V20_WAIT_SECONDS = int(os.getenv("DOLA_V20_WAIT_SECONDS", "600"))

# 同/近一次探活的最小间隔（秒）。避免空转打代理。
PROBE_INTERVAL_SECONDS = int(os.getenv("DOLA_PROBE_INTERVAL_SECONDS", "120"))

# 同时最多打开多少个号去提交/占用。0 = 不限制。
MAX_OPEN_ACCOUNTS = int(os.getenv("DOLA_MAX_OPEN_ACCOUNTS", "0"))

# 出片/探测都跑在 asyncio.to_thread 里，而 Python 默认线程池只有 min(32, CPU+4) 个线程 ——
# 不限并发时它会变成真正的瓶颈（第 33 个任务只能排队等）。0 = 自动（max(64, CPU×8)）。
THREAD_POOL_MAX = int(os.getenv("DOLA_THREAD_POOL_MAX", "0"))

# ===== 纯 API 出片（cookie 账号，对齐 dola-pool-cookie）=====
# cookie 来源账号（source=cookie 且有 cookie_state.json）出片走纯 API + bdms 签名，
# 不再依赖浏览器 profile。1 = 开启；0 = 全部走浏览器。
PURE_API_ENABLED = os.getenv("DOLA_PURE_API", "1") == "1"
# 纯 API 协议参数（对齐 dola-pool-cookie 的 region /pc_version）。
PURE_API_REGION = os.getenv("DOLA_PURE_REGION", "JP")
PURE_API_PC_VERSION = os.getenv("DOLA_PURE_PC_VERSION", "3.33.11")
# 是否走无水印（第三方 nowatermark 解析）。
PURE_API_REMOVE_WATERMARK = os.getenv("DOLA_PURE_REMOVE_WATERMARK", "1") == "1"
# 纯 API 出片轮询/下载目录（复用 DOWNLOAD_DIR）。
# 2026-10-10：出片等待上限放宽到 2 小时（7200s）——上游排队/滞留实测超 40 分钟，
# 15 分钟旧窗口会把还在排队的单提前判死。要回旧行为把 DOLA_PURE_TIMEOUT 设回 900。
PURE_API_TIMEOUT = int(os.getenv("DOLA_PURE_TIMEOUT", "7200"))
# 30 秒任务专用轮询窗口：上游晚高峰排队实测可达 2 小时（受理播报「预计等待 2 小时」），
# 旧窗口 1800s 会把已受理的任务提前判死、白白烧掉 2 点额度（19:36 批次 9 号全灭的根因）。
# 必须直出 30s，所以等得起；要回旧行为把 DOLA_30S_TIMEOUT 设回 1800。
PURE_API_30S_TIMEOUT = int(os.getenv("DOLA_30S_TIMEOUT", "9000"))
# 30 秒任务的任务级总时限（含换号）：单次窗口最长 PURE_API_30S_TIMEOUT，
# 外加 30 分钟给「快速失败换号」用。3600s 的通用总时限会在上游 2h 排队下腰斩任务。
TASK_DEADLINE_30S = int(os.getenv("DOLA_30S_TASK_DEADLINE", str(PURE_API_30S_TIMEOUT + 1800)))
# 真源在文件上方「IPRocket 流量治理」段的 POLL_INTERVAL（默认 120s，30s 时代
# 单任务轮询流量是现在 4 倍）。
PURE_API_POLL_INTERVAL = POLL_INTERVAL

# ===== 三段出口策略落地（hint 830 / i831）=========================
# ①注册走 Clash 7897 ②提交段由 DOLA_SUBMIT_EXIT 决定（residential=iprocket 住宅
# socks5h 裸 socket shim；clash/direct/http://host:port=走 http 代理或直连）
# ③下载 CDN 直链本地直连。
# ★ 注意：这里传的是 apply(None)（config=None），所以 apply() 里所有
#   `config.PROXY = ...` 的赋值都不会生效 —— config.PROXY 早已在第 70 行由
#   DOLA_PROXY 决定。因此「切 Clash 出片」必须靠 .env.local 里的
#   `DOLA_SUBMIT_EXIT=clash` + `DOLA_PROXY=http://127.0.0.1:7897` 两者配合。
try:
    import os as _os831, sys as _sys831
    _sys831.path.insert(0, _os831.path.dirname(_os831.path.abspath(__file__)))
    import three_segment_exit as _TS831
    # 把 clash 别名解析成规范值（会同时补上 DOLA_PROXY），再让 PROXY 跟随它 ——
    # 这样面板(server.py)与引擎(run_pool.py)两条路径都拿到同一个出口。
    if _TS831.is_clash_alias(_TS831.SUBMIT_EXIT):
        _os831.environ.setdefault("DOLA_PROXY", _TS831.CLASH_PROXY)
        PROXY = _os831.environ["DOLA_PROXY"]
    THREE_SEGMENT = _TS831.apply(None)
    _TS831.start_keeper()
except Exception as _e831:      # 传输层不可用时不阻断引擎（退回旧单出口行为）
    THREE_SEGMENT = {"err": repr(_e831)}
