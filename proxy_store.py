"""代理池存储：代理记录 + 账号-代理映射，存 pool_usage.db。

支持两种出口类型（proxies.mode）：

- ``static``  静态IP：一条记录 = 一个固定出口，host:port 决定出口。
- ``dynamic`` 动态IP：隧道/旋转网关。host:port 固定，出口 IP 由 username 里的
  session 选择器决定（BrightData / Oxylabs / 快代理 / 芝麻等隧道代理都是这个形态）。
  每个号自动分配独立 sticky session，也就是独立出口 IP；session 到期或被上游风控
  时可以随时换一个 —— 换 session 即换 IP，不用换账号。

出口身份（egress key）：
- static  → ``scheme://host:port``（与历史行为一致）
- dynamic → ``scheme://host:port#<session>``，各号互不干扰，
  所以共用同一个旋转网关也不会被同出口隔离误伤。

浏览器每次启动/验证账号前通过 browser.py 查账号对应的代理；
未给账号指定代理时回落到 config.DOLA_PROXY（环境默认代理）。
"""
import os
import re
import secrets
import socket
import sqlite3
try:
    import _safe_sqlite  # thread-safe sqlite3 wrapper (fixes SQLITE_MISUSE)
except Exception:  # pragma: no cover
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import _safe_sqlite

import ssl
import struct
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import config

DB_PATH = Path(config.POOL_DB_PATH if hasattr(config, "POOL_DB_PATH") else "pool_usage.db")

PROXY_MODES = ("static", "dynamic", "extract")
# username 的拼装模板；{user} = 记录里的基础用户名，{session} = 本次 sticky session
DEFAULT_SESSION_TEMPLATE = "{user}-{session}"


def _default_node_ttl() -> int:
    """提取型节点没有 sessiontime 时的默认保质期（秒）。"""
    return int(os.getenv("DOLA_EXTRACT_NODE_TTL", "300"))


def _extract_timeout() -> int:
    return int(os.getenv("DOLA_EXTRACT_TIMEOUT", "30"))


def _default_rotate_min_interval() -> int:
    return int(os.getenv("DOLA_DYNAMIC_ROTATE_MIN_INTERVAL", "60"))


def _default_sticky_ttl() -> int:
    """session 保质期（秒）；0 = 不按时间过期，仅在风控时轮换。"""
    return int(os.getenv("DOLA_DYNAMIC_STICKY_TTL", "0"))


# 已建表/迁移过的库路径。热路径不能碰 DDL：
# CREATE TABLE / ALTER TABLE 都要写锁，而这个库被服务进程里多条长连接共用，
# 一旦取连接时做 DDL 撞上别处的锁就会抛 "database is locked"；
# 异常里残留的 traceback 又会让那条连接（连同一个未释放的写事务）不被回收，
# 于是锁不释放、后续每一次取连接都失败，级联成整片 500。
_ready_paths: set[str] = set()


def _connect() -> sqlite3.Connection:
    conn = _safe_sqlite.connect(str(DB_PATH), check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    # WAL + synchronous=NORMAL：本机 fsync ~40ms/次，代理探测/节点刷新高频写会拖慢面板
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        if str(DB_PATH) not in _ready_paths:
            _init_schema(conn)
            _ready_paths.add(str(DB_PATH))
    except Exception:
        conn.close()
        raise
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """建表 + 迁移，每个库路径在一个进程里只做一次。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proxies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            protocol TEXT NOT NULL DEFAULT 'http',
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            username TEXT DEFAULT '',
            password TEXT DEFAULT '',
            remark TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_proxy (
            account TEXT PRIMARY KEY,
            proxy_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_sessions (
            account TEXT PRIMARY KEY,
            proxy_id TEXT,
            session TEXT NOT NULL,
            rotated_at REAL NOT NULL,
            last_exit_ip TEXT DEFAULT '',
            last_probe_at REAL DEFAULT 0,
            last_probe_ok INTEGER DEFAULT 0,
            last_probe_detail TEXT DEFAULT ''
        )
        """
    )
    # 提取型（mode=extract）拿到的节点池：一条记录 = 一个可直接使用的 host:port:user:pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy_id TEXT NOT NULL,
            protocol TEXT NOT NULL DEFAULT 'http',
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            username TEXT DEFAULT '',
            password TEXT DEFAULT '',
            exit_hint TEXT DEFAULT '',
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL DEFAULT 0,
            dead INTEGER NOT NULL DEFAULT 0,
            fail_count INTEGER NOT NULL DEFAULT 0,
            last_ok_at REAL NOT NULL DEFAULT 0,
            last_exit_ip TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proxy_nodes_pid ON proxy_nodes(proxy_id, dead)"
    )
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """给已有库补上动态代理需要的列（幂等）。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(proxies)")}
    wanted = {
        "mode": "TEXT NOT NULL DEFAULT 'static'",
        "session_template": "TEXT DEFAULT ''",
        "sticky_ttl": "INTEGER NOT NULL DEFAULT 0",
        "rotate_on_risk": "INTEGER NOT NULL DEFAULT 1",
        "rotate_min_interval": "INTEGER NOT NULL DEFAULT 0",
        "extract_url": "TEXT DEFAULT ''",
        "extract_interval": "INTEGER NOT NULL DEFAULT 0",
        "extract_last_at": "REAL NOT NULL DEFAULT 0",
        "extract_last_count": "INTEGER NOT NULL DEFAULT 0",
        "extract_last_error": "TEXT DEFAULT ''",
        "is_default": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, ddl in wanted.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE proxies ADD COLUMN {name} {ddl}")
    scols = {row["name"] for row in conn.execute("PRAGMA table_info(proxy_sessions)")}
    if "node_id" not in scols:
        conn.execute("ALTER TABLE proxy_sessions ADD COLUMN node_id INTEGER DEFAULT 0")


def _safe_dict(row: sqlite3.Row, with_password: bool = False) -> dict:
    data = dict(row)
    data.setdefault("mode", "static")
    data["is_dynamic"] = data.get("mode") == "dynamic"
    if not with_password:
        data["has_password"] = bool(data.get("password"))
        data.pop("password", None)
    return data


def list_proxies() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM proxies ORDER BY enabled DESC, created_at DESC"
        ).fetchall()
        out = []
        for row in rows:
            item = _safe_dict(row)
            used = conn.execute(
                "SELECT COUNT(*) AS c FROM account_proxy WHERE proxy_id=?",
                (row["id"],),
            ).fetchone()["c"]
            item["account_count"] = used
            out.append(item)
        return out
    finally:
        conn.close()


def get_proxy(proxy_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM proxies WHERE id=?", (proxy_id,)
        ).fetchone()
        return _safe_dict(row, with_password=True) if row else None
    finally:
        conn.close()


def create_proxy(name: str, protocol: str, host: str, port: int,
                 username: str = "", password: str = "",
                 remark: str = "", mode: str = "static",
                 session_template: str = "", sticky_ttl: int | None = None,
                 rotate_on_risk: bool = True,
                 rotate_min_interval: int | None = None,
                 extract_url: str = "",
                 extract_interval: int = 0,
                 nodes_text: str = "") -> dict:
    name = (name or "").strip()
    protocol = (protocol or "http").strip().lower()
    host = (host or "").strip()
    mode = (mode or "static").strip().lower()
    nodes_text = (nodes_text or "").strip()
    if not name:
        raise ValueError("代理名称不能为空")
    if protocol not in ("http", "https", "socks4", "socks5"):
        raise ValueError("协议仅支持 http/https/socks4/socks5")
    if mode not in PROXY_MODES:
        raise ValueError("mode 仅支持 static/dynamic/extract")
    if mode == "extract":
        # 动态IP：出口由节点池提供 —— 要么有提取链接，要么直接粘贴节点行。
        if not (extract_url or "").strip() and not nodes_text:
            raise ValueError(
                "动态IP 代理要么填提取链接，要么粘贴节点列表（host:port:user:pass 一行一个）")
        if nodes_text and not parse_extract_lines(nodes_text):
            raise ValueError(
                "粘贴的节点没解析出合法行，格式应为 host:port:user:pass（一行一个）")
        if not host or not int(port or 0):
            # 没填网关地址时，用粘贴的第一行作为记录的 host/port
            first = parse_extract_lines(nodes_text)[:1]
            if first:
                host = first[0]["host"]
                port = int(first[0]["port"])
    if mode != "extract" and (not host or not int(port or 0)):
        # extract 型不需要网关地址：出口由节点池里的节点决定
        raise ValueError("代理名称/地址/端口不能为空")
    ttl = _default_sticky_ttl() if sticky_ttl is None else int(sticky_ttl)
    min_interval = (_default_rotate_min_interval() if rotate_min_interval is None
                    else int(rotate_min_interval))
    proxy_id = "px_" + uuid.uuid4().hex[:12]
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO proxies
                (id, name, protocol, host, port, username, password, remark, enabled,
                 created_at, mode, session_template, sticky_ttl, rotate_on_risk,
                 rotate_min_interval, extract_url, extract_interval)
                VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?)""",
            (proxy_id, name, protocol, host, int(port or 0),
              username or "", password or "", remark or "", time.time(),
              mode, (session_template or "").strip(), ttl,
              1 if rotate_on_risk else 0, min_interval,
              (extract_url or "").strip(), int(extract_interval or 0)),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.close()
        raise ValueError(f"代理名称已存在: {name}") from exc
    if nodes_text:
        add_nodes_from_text(proxy_id, nodes_text)
    row = None
    try:
        row = conn.execute(
            "SELECT * FROM proxies WHERE id=?", (proxy_id,)
        ).fetchone()
    finally:
        conn.close()
    return _safe_dict(row, with_password=False) if row else {}


def update_proxy(proxy_id: str, *, name=None, protocol=None, host=None,
                 port=None, username=None, password=None, remark=None,
                 enabled=None, mode=None, session_template=None,
                 sticky_ttl=None, rotate_on_risk=None,
                 rotate_min_interval=None, extract_url=None,
                 extract_interval=None) -> dict:
    fields = []
    values = []
    if name is not None:
        fields.append("name=?")
        values.append((name or "").strip())
    if protocol is not None:
        fields.append("protocol=?")
        values.append((protocol or "http").strip().lower())
    if host is not None:
        fields.append("host=?")
        values.append((host or "").strip())
    if port is not None:
        fields.append("port=?")
        values.append(int(port))
    if username is not None:
        fields.append("username=?")
        values.append(username or "")
    if password is not None:
        fields.append("password=?")
        values.append(password or "")
    if remark is not None:
        fields.append("remark=?")
        values.append(remark or "")
    if enabled is not None:
        fields.append("enabled=?")
        values.append(1 if enabled else 0)
    if mode is not None:
        mode = (mode or "static").strip().lower()
        if mode not in PROXY_MODES:
            raise ValueError("mode 仅支持 static/dynamic/extract")
        fields.append("mode=?")
        values.append(mode)
    if extract_url is not None:
        fields.append("extract_url=?")
        values.append((extract_url or "").strip())
    if extract_interval is not None:
        fields.append("extract_interval=?")
        values.append(int(extract_interval))
    if session_template is not None:
        fields.append("session_template=?")
        values.append((session_template or "").strip())
    if sticky_ttl is not None:
        fields.append("sticky_ttl=?")
        values.append(int(sticky_ttl))
    if rotate_on_risk is not None:
        fields.append("rotate_on_risk=?")
        values.append(1 if rotate_on_risk else 0)
    if rotate_min_interval is not None:
        fields.append("rotate_min_interval=?")
        values.append(int(rotate_min_interval))
    if not fields:
        return get_proxy(proxy_id) or {}
    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE proxies SET {', '.join(fields)} WHERE id=?",
            (*values, proxy_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            conn.close()
            raise ValueError("代理不存在")
    except ValueError:
        conn.close()
        raise
    row = None
    try:
        row = conn.execute(
            "SELECT * FROM proxies WHERE id=?", (proxy_id,)
        ).fetchone()
    finally:
        conn.close()
    return _safe_dict(row, with_password=False) if row else {}


def delete_proxy(proxy_id: str):
    conn = _connect()
    try:
        conn.execute("DELETE FROM account_proxy WHERE proxy_id=?", (proxy_id,))
        conn.execute("DELETE FROM proxy_sessions WHERE proxy_id=?", (proxy_id,))
        conn.execute("DELETE FROM proxies WHERE id=?", (proxy_id,))
        conn.commit()
    finally:
        conn.close()


def get_account_proxy(account: str) -> dict | None:
    """返回账号绑定的代理（含密码）；未绑定返回 None。"""
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT p.* FROM account_proxy ap
               JOIN proxies p ON p.id = ap.proxy_id
               WHERE ap.account=? AND p.enabled=1""",
            (account,),
        ).fetchone()
        return _safe_dict(row, with_password=True) if row else None
    finally:
        conn.close()


def forget_account(account: str) -> None:
    """账号删除时清掉它的代理绑定与出口会话，避免同名新号继承旧状态。"""
    conn = _connect()
    try:
        conn.execute("DELETE FROM account_proxy WHERE account=?", (account,))
        conn.execute("DELETE FROM proxy_sessions WHERE account=?", (account,))
        conn.commit()
    finally:
        conn.close()


def rename_account(old: str, new: str) -> None:
    """账号改名时搬走以账号名为主键的代理记录（绑定 + 出口会话）。"""
    conn = _connect()
    try:
        conn.execute("UPDATE account_proxy SET account=? WHERE account=?", (new, old))
        conn.execute("UPDATE proxy_sessions SET account=? WHERE account=?", (new, old))
        conn.commit()
    finally:
        conn.close()


def set_account_proxy(account: str, proxy_id: str | None):
    conn = _connect()
    try:
        if proxy_id:
            row = conn.execute(
                "SELECT id FROM proxies WHERE id=?", (proxy_id,)
            ).fetchone()
            if not row:
                raise ValueError("代理不存在")
            conn.execute(
                """INSERT INTO account_proxy(account, proxy_id) VALUES (?,?)
                   ON CONFLICT(account) DO UPDATE SET proxy_id=excluded.proxy_id""",
                (account, proxy_id),
            )
            # 换绑代理后旧 session 作废，下次取用时按新代理重新分配
            conn.execute(
                "DELETE FROM proxy_sessions WHERE account=? AND proxy_id<>?",
                (account, proxy_id),
            )
        else:
            conn.execute(
                "DELETE FROM account_proxy WHERE account=?", (account,)
            )
            conn.execute(
                "DELETE FROM proxy_sessions WHERE account=?", (account,)
            )
        conn.commit()
    finally:
        conn.close()


# ===== 池默认代理：导入/注册的账号自动绑定它 =====


def get_default_proxy_id() -> str | None:
    """返回池默认代理 id（导入账号时自动绑定的那条）；未设置返回 None。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM proxies WHERE is_default=1 "
            "ORDER BY enabled DESC, created_at DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def set_default_proxy(proxy_id: str | None) -> None:
    """把某条代理设为池默认代理（全局唯一）；proxy_id 为 None/空 = 取消默认。"""
    conn = _connect()
    try:
        conn.execute("UPDATE proxies SET is_default=0 WHERE is_default=1")
        if proxy_id:
            row = conn.execute(
                "SELECT id FROM proxies WHERE id=?", (proxy_id,)
            ).fetchone()
            if not row:
                raise ValueError("代理不存在")
            conn.execute("UPDATE proxies SET is_default=1 WHERE id=?", (proxy_id,))
        conn.commit()
    finally:
        conn.close()


def bind_default_proxy(account: str) -> str | None:
    """账号还没有代理绑定时，把它绑到池默认代理（导入自动绑定）。

    已绑定的账号不动（尊重手动指定）；没有默认代理或默认代理已停用时返回 None。
    返回绑定的 proxy_id，未绑定返回 None。
    """
    conn = _connect()
    try:
        bound = conn.execute(
            "SELECT 1 FROM account_proxy WHERE account=?", (account,)
        ).fetchone()
        if bound:
            return None
        row = conn.execute(
            "SELECT id FROM proxies WHERE is_default=1 AND enabled=1 LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    set_account_proxy(account, row["id"])
    return row["id"]


# ===== 动态代理：session（=出口 IP）管理 =====


def _new_session_id() -> str:
    return secrets.token_hex(4)


def _session_row(conn: sqlite3.Connection, account: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM proxy_sessions WHERE account=?", (account,)
    ).fetchone()


def get_session(account: str) -> dict | None:
    conn = _connect()
    try:
        row = _session_row(conn, account)
        return dict(row) if row else None
    finally:
        conn.close()


def rotate_session(account: str, proxy_id: str | None = None) -> str:
    """给账号换一个 sticky session（=换一个出口 IP），返回新的 session id。"""
    session = _new_session_id()
    conn = _connect()
    try:
        if not proxy_id:
            row = conn.execute(
                "SELECT proxy_id FROM account_proxy WHERE account=?", (account,)
            ).fetchone()
            proxy_id = row["proxy_id"] if row else ""
        conn.execute(
            """INSERT INTO proxy_sessions
               (account, proxy_id, session, rotated_at, last_exit_ip,
                last_probe_at, last_probe_ok, last_probe_detail)
               VALUES (?,?,?,?,'',0,0,'')
               ON CONFLICT(account) DO UPDATE SET
                 proxy_id=excluded.proxy_id,
                 session=excluded.session,
                 rotated_at=excluded.rotated_at,
                 last_exit_ip='',
                 last_probe_at=0,
                 last_probe_ok=0,
                 last_probe_detail=''""",
            (account, proxy_id or "", session, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return session


def _session_expired(row: sqlite3.Row, sticky_ttl: int) -> bool:
    if not sticky_ttl or sticky_ttl <= 0:
        return False
    return time.time() - float(row["rotated_at"] or 0) > sticky_ttl


def _ensure_session(account: str, info: dict, rotate: bool = False) -> str:
    """拿到该号当前有效的 session；缺失/过期/显式轮换时换一个。"""
    conn = _connect()
    try:
        row = _session_row(conn, account)
    finally:
        conn.close()
    ttl = int(info.get("sticky_ttl") or 0)
    if row and row["proxy_id"] == info["id"]:
        if rotate or _session_expired(row, ttl):
            return rotate_session(account, info["id"])
        return row["session"]
    return rotate_session(account, info["id"])


def render_session_username(template: str, base_user: str, session: str) -> str:
    """把 session 拼进用户名。模板里可用 {user} 和 {session}。"""
    tpl = (template or DEFAULT_SESSION_TEMPLATE).strip()
    try:
        out = tpl.format(user=base_user or "", session=session)
    except (KeyError, IndexError, ValueError):
        out = f"{base_user}-{session}" if base_user else session
    # {user} 为空时模板会留下前导/尾随分隔符
    return out.strip("-_. ")


def _url_from_record(info: dict, username: str | None = None) -> str:
    user = info.get("username") if username is None else username
    auth = ""
    if user:
        auth = quote(user, safe="")
        if info.get("password"):
            auth += ":" + quote(info["password"], safe="")
        auth += "@"
    return f"{info['protocol']}://{auth}{info['host']}:{info['port']}"


def _egress_of_url(proxy_url: str) -> str:
    """归一化出口为 scheme://host:port（去掉凭据）；空/直连 => 'direct'。"""
    proxy_url = (proxy_url or "").strip()
    if not proxy_url or proxy_url == "direct":
        return "direct"
    if "://" not in proxy_url:
        proxy_url = "http://" + proxy_url
    parts = urlsplit(proxy_url)
    if not parts.hostname:
        return "direct"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{parts.hostname}:{port}"


def proxy_url_for(account: str, *, rotate: bool = False, ensure: bool = True) -> str:
    """账号实际使用的代理 URL（动态代理会带上该号自己的 session）。

    这是全项目构造代理 URL 的唯一入口 —— browser.py / browser_pool.py /
    aiohttp 下载路径都走这里，避免各处重复拼 username。

    ensure=False 只读模式：extract 模式只取当前绑定节点，不补节点/不探活
    （供事件循环上的路径使用，见 egress_key_for 的说明）。
    """
    info = get_account_proxy(account)
    if not info:
        return config.PROXY or ""
    mode = info.get("mode") or "static"
    if mode == "extract":
        if not ensure:
            node = _bound_node(account)
            return _url_from_node(node) if node else (config.PROXY or "")
        node = _ensure_node(account, info, rotate=rotate)
        return _url_from_node(node) if node else (config.PROXY or "")
    if mode != "dynamic":
        return _url_from_record(info)
    if not ensure:
        conn = _connect()
        try:
            row = _session_row(conn, account)
        finally:
            conn.close()
        if row and row["proxy_id"] == info["id"] and row["session"]:
            session = row["session"]
        else:
            session = None
    else:
        session = _ensure_session(account, info, rotate=rotate)
    if not session:
        return _url_from_record(info, username=info.get("username") or "")
    username = render_session_username(
        info.get("session_template") or "", info.get("username") or "", session
    )
    return _url_from_record(info, username)


def _upgrade_socks_dns(url: str) -> str:
    """socks5:// -> socks5h://（其余 scheme 原样返回）。"""
    if url.startswith("socks5://"):
        return "socks5h://" + url[len("socks5://"):]
    return url


def requests_proxy_url(account: str, *, rotate: bool = False,
                       ensure: bool = True) -> str:
    """给 requests / aiohttp 用的代理 URL：socks5 升级为 socks5h（域名交代理远端解析）。

    socks5:// 会让 requests 先在本机解析域名、再把解析出的 IP 交给代理。dola 的 CDN 与
    对象存储走 Akamai，会同时返回 IPv4 和 IPv6，本机挑中的地址代理侧经常连不上，表现为
    「参考图上传失败 ... SOCKSHTTPSConnectionPool(host='tos-...vodupload.com', port=443)
    ... NewConnectionError」。同一个代理换成 socks5h:// 立刻可通（实测 204）。

    注意：只给 requests 这类路径用。Playwright / Chromium 不认 socks5h 这个 scheme，
    浏览器路径必须继续用 proxy_url_for() 返回的 socks5://。
    """
    return _upgrade_socks_dns(proxy_url_for(account, rotate=rotate, ensure=ensure))


def egress_key_for(account: str, *, ensure: bool = True) -> str:
    """账号的出口身份。动态代理按 session 区分，静态代理按 host:port。

    ensure=False 是只读模式，供面板热路径与选号扫描使用：
    只读当前绑定，绝不触发补节点/探活/换 session。那些都是同步网络请求
    或最长 15s 的 sqlite 锁等待，曾被 py-spy 抓到在事件循环上执行
    （面板「刷新半天加载不出来」的根因，2026-10-06）。
    """
    info = get_account_proxy(account)
    if not info:
        return _egress_of_url(config.PROXY or "")
    mode = info.get("mode") or "static"
    if mode == "extract":
        if not ensure:
            node = _bound_node(account)
            if not node:
                return _egress_of_url(config.PROXY or "")
            return f"{_egress_of_url(_url_from_node(node))}#node{node['id']}"
        node = _ensure_node(account, info)
        if not node:
            return _egress_of_url(config.PROXY or "")
        # 每个节点是独立出口，按节点 id 区分
        return f"{_egress_of_url(_url_from_node(node))}#node{node['id']}"
    base = _egress_of_url(_url_from_record(info, username=""))
    if mode != "dynamic":
        return base
    if not ensure:
        conn = _connect()
        try:
            row = _session_row(conn, account)
        finally:
            conn.close()
        if row and row["proxy_id"] == info["id"] and row["session"]:
            return f"{base}#{row['session']}"
        return base
    session = _ensure_session(account, info)
    return f"{base}#{session}"


def rotate_account_session(account: str, *, reason: str = "",
                           min_interval: int | None = None,
                           force: bool = False) -> str | None:
    """风控/异常时给动态代理账号换出口 IP。

    min_interval 用来防止**自动**轮换短时间反复换（把 IP 刷光）：
    距上次轮换不足该秒数则跳过。
    force=True 用于人工操作（面板点「换IP」）—— 操作员是明确意图，不该被间隔挡住。
    返回新 session；跳过或该号不是动态代理时返回 None。
    """
    info = get_account_proxy(account)
    if not info:
        return None
    mode = info.get("mode") or "static"
    if mode == "extract":
        if not info.get("rotate_on_risk", 1):
            return None
        return _rotate_extract_node(account, info, min_interval, force=force)
    if mode != "dynamic":
        return None
    if not info.get("rotate_on_risk", 1):
        return None
    interval = (info.get("rotate_min_interval") if min_interval is None
                else min_interval)
    try:
        interval = int(interval or 0)
    except (TypeError, ValueError):
        interval = 0
    if not force:
        cur = get_session(account)
        if cur and interval > 0 and (time.time() - float(cur["rotated_at"] or 0)) < interval:
            return None
    return rotate_session(account, info["id"])


def _rotate_extract_node(account: str, info: dict,
                         min_interval: int | None,
                         force: bool = False) -> str | None:
    """提取型：换到池子里另一个节点（=换一个出口 IP）。"""
    interval = info.get("rotate_min_interval") if min_interval is None else min_interval
    try:
        interval = int(interval or 0)
    except (TypeError, ValueError):
        interval = 0
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT node_id, rotated_at FROM proxy_sessions WHERE account=?",
            (account,),
        ).fetchone()
    finally:
        conn.close()
    if (not force and row and interval > 0
            and (time.time() - float(row["rotated_at"] or 0)) < interval):
        return None
    old_id = row["node_id"] if row else 0
    # 用**带探活**的选节点（_pick_usable_node）而不是 _pick_node：
    # 坏节点（含 TLS 被中间人拦截的，见 is_tls_cert_error）当场判死再换下一个，
    # 否则「换 IP」可能又换到一个同样不可用的节点上，白换一次。
    node = _pick_usable_node(info["id"], account, exclude_node=old_id, info=info)
    if node is None and _should_extract(info):
        fetch_extract_nodes(info["id"], info)
        node = _pick_usable_node(info["id"], account, exclude_node=old_id, info=info)
    if node is None:
        return None
    _bind_node(account, info["id"], node)
    return node.get("exit_hint") or node.get("username") or f"node{node['id']}"


def account_proxy_url(account: str) -> str | None:
    """给 aiohttp/下载用：返回可直接使用的代理 URL（带账号密码）。"""
    url = proxy_url_for(account)
    return url or None


def record_probe_result(account: str, ip: str, ok: bool, detail: str = "") -> None:
    conn = _connect()
    try:
        conn.execute(
            """UPDATE proxy_sessions
               SET last_exit_ip=?, last_probe_at=?, last_probe_ok=?,
                   last_probe_detail=?
               WHERE account=?""",
            (ip or "", time.time(), 1 if ok else 0, (detail or "")[:300], account),
        )
        conn.commit()
    finally:
        conn.close()


def sessions_snapshot() -> dict[str, dict]:
    """account -> session 记录，供面板展示当前出口 IP。"""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM proxy_sessions").fetchall()
        return {row["account"]: dict(row) for row in rows}
    finally:
        conn.close()


# 出口 IP 查询目标：主用 ip-api（带归属地），被限流/单端口被拦时退到 ipify（只回 IP）。
# 分开的意义：把「代理到底能不能出网」和「IP 库这一跳能不能查」拆开，
# 后者抖动不该让面板报成"探测失败"（面板上曾出现只写到连接池名、看不到原因的报错）。
_IP_LOOKUP_TARGETS = (
    ("ip-api",
     "http://ip-api.com/json/?fields=status,country,countryCode,regionName,isp,query"),
    ("ipify", "https://api.ipify.org?format=json"),
)


def _short_err(exc, limit: int = 300) -> str:
    """把 requests/urllib3 的异常压成面板能看懂的短句。

    urllib3 把真实原因放在 "Caused by" 之后，只按前 200 字截断时剩下的全是连接池名
    （``SOCKSHTTPSConnectionPool(host=...): Max retries exceeded with url: ...``），
    面板上就是个看不出所以然的报错。这里把 Caused by 那段挪到最前面。
    """
    msg = str(exc)
    idx = msg.find("Caused by")
    if idx > 0:
        msg = msg[idx:].strip() + " <= " + msg[:idx].strip()
    return msg[:limit]


def _parse_lookup(kind: str, data: dict) -> dict:
    """归一化两个 IP 库的返回体。"""
    if kind == "ip-api":
        if data.get("status") != "success":
            return {"ok": False, "ip": "", "error": f"ip-api 返回 {str(data)[:120]}"}
        return {
            "ok": True,
            "ip": data.get("query", ""),
            "country": data.get("country", ""),
            "country_code": data.get("countryCode", ""),
            "region": data.get("regionName", ""),
            "isp": data.get("isp", ""),
            "source": "ip-api",
        }
    ip = str(data.get("ip") or "").strip()
    if not ip:
        return {"ok": False, "ip": "", "error": f"ipify 未返回 ip: {str(data)[:120]}"}
    return {"ok": True, "ip": ip, "country": "", "country_code": "",
            "region": "", "isp": "", "source": "ipify"}


def probe_exit_ip(proxy_url: str, timeout: int = 15) -> dict:
    """通过代理查真实出口 IP 与归属地（用于验证动态代理是否真的在换 IP）。"""
    if not proxy_url or proxy_url == "direct":
        return {"ok": False, "ip": "", "error": "未配置代理（直连）"}
    import requests

    kwargs = {"timeout": timeout}
    if proxy_url.startswith("socks"):
        # requests 需要 PySocks 才支持 socks；缺失时明确报错而不是静默失败
        try:
            import socks  # noqa: F401
        except ImportError:
            return {"ok": False, "ip": "",
                    "error": "socks 代理需要 PySocks（pip install PySocks）"}
    kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
    errors: list[str] = []
    for kind, url in _IP_LOOKUP_TARGETS:
        try:
            resp = requests.get(url, **kwargs)
            resp.raise_for_status()
            result = _parse_lookup(kind, resp.json())
        except Exception as exc:  # noqa: BLE001 - 失败原因要回报给面板
            errors.append(f"{kind}: {_short_err(exc, 160)}")
            continue
        if result.get("ok"):
            return result
        errors.append(f"{kind}: {str(result.get('error'))[:140]}")
    return {"ok": False, "ip": "", "error": "；".join(errors)[:400] or "出口 IP 查询失败"}


def _probe_node_tries() -> int:
    """单次探测最多试几个可用节点（一次手工探测，别把提取配额/时间耗光）。"""
    return max(1, int(os.getenv("DOLA_PROBE_NODE_TRIES", "4")))


def _manual_extract_floor() -> int:
    """手工探测时补节点的最小间隔：防止连点"探测"把提取接口配额刷爆。"""
    return max(5, int(os.getenv("DOLA_PROBE_EXTRACT_FLOOR", "30")))


# 明确的「节点/网关自身坏了」信号：只有命中这些才判死，
# 超时之类的软失败不判死（可能只是本机网络或 www.dola.com 侧抖动，
# 一失败就判死会在探活目标抖动时把整池节点误杀）。
_NODE_FATAL_MARKERS = (
    "账密认证被拒", "网关不接受", "CONNECT 被拒", "TLS 证书校验失败",
    "refused", "10061", "10013", "unreachable", "no route",
)


def _is_node_fatal(detail: str) -> bool:
    low = (detail or "").lower()
    return any(k.lower() in low for k in _NODE_FATAL_MARKERS)


def _probe_extract_record(info: dict) -> dict:
    """提取型探测：挨个试池子里的可用节点，坏节点当场判死。

    旧实现只探 ``nodes[0]``：只要排在最前那个节点（或它背后的网关）当前是坏的，
    面板就永远报「探测失败」，而池子里其实还有能用的节点 —— 现场 16:0x 那次的
    ``SOCKSHTTPSConnectionPool(...) Max retries exceeded`` 就是这种假失败。
    """
    pid = info["id"]
    tried: list[int] = []
    last_err = ""
    for _ in range(_probe_node_tries()):
        pool = [n for n in list_nodes(pid, alive_only=True) if n["id"] not in tried]
        if not pool:
            break
        node = pool[0]
        tried.append(node["id"])
        # 已验证过（last_ok_at 新鲜）的节点省掉一次探活，直接查出口 IP
        fresh_verified = (time.time() - float(node.get("last_ok_at") or 0)) < 600
        if not fresh_verified:
            ok_node, detail = _probe_node(node)
            if not ok_node:
                if _is_node_fatal(detail):
                    mark_node_dead(node["id"], "探测: " + detail)
                last_err = f"{node['host']}:{node['port']} → {detail}"
                continue
        result = probe_exit_ip(_url_from_node(node))
        result["node_id"] = node["id"]
        result["node_host"] = f"{node['host']}:{node['port']}"
        if result.get("ok"):
            mark_node_ok(node["id"], result.get("ip", ""))
            return result
        # 节点探活通过、只有 IP 库查不动（被限流/被墙）→ 链路是好的，别报"探测失败"
        if not fresh_verified:
            mark_node_ok(node["id"], "")
            return {"ok": True, "ip": "", "ip_unknown": True,
                    "node_id": node["id"], "node_host": result["node_host"],
                    "note": "链路可用，出口 IP 查询失败：" + str(result.get("error"))[:160]}
        if _is_node_fatal(str(result.get("error"))):
            mark_node_dead(node["id"], "探测: " + str(result.get("error"))[:80])
        last_err = f"{node['host']}:{node['port']} → {str(result.get('error'))[:120]}"
    if not tried:
        return {"ok": False, "ip": "", "error": "节点池为空（无可用的未过期节点）"}
    return {"ok": False, "ip": "",
            "error": f"已试 {len(tried)} 个可用节点全部失败（最后：{last_err or '未知原因'}）"}


def probe_proxy_record(proxy_id: str, session: str | None = None) -> dict:
    """探测一条代理记录的出口 IP。

    动态代理用一个临时 session 探测（等于"这个网关现在能出什么 IP"），
    不影响任何账号已经占用的 sticky session；这个 session 不通时换个新的再探一次。
    提取型逐个试池子里的可用节点，坏节点当场判死（见 _probe_extract_record）。
    """
    info = get_proxy(proxy_id)
    if not info:
        return {"ok": False, "ip": "", "error": "代理不存在"}
    mode = info.get("mode") or "static"
    if mode == "extract":
        result = _probe_extract_record(info)
        if result.get("ok"):
            return result
        # 可用节点全废 → 手工探测允许补一批新节点再试（有硬地板，防连点刷配额）
        if (info.get("extract_url") or "").strip() and \
                (time.time() - float(info.get("extract_last_at") or 0)) >= _manual_extract_floor():
            fetched = fetch_extract_nodes(proxy_id, info)
            if fetched.get("ok"):
                retry = _probe_extract_record(info)
                retry["extracted"] = fetched.get("added", 0)
                if retry.get("ok"):
                    return retry
                result = retry
            else:
                result["error"] = (f"{result.get('error')}；补节点失败："
                                   f"{str(fetched.get('error'))[:120]}")
        return result
    if mode == "dynamic":
        tpl = info.get("session_template") or ""
        base = info.get("username") or ""
        sess = session or _new_session_id()
        result = probe_exit_ip(_url_from_record(info, render_session_username(tpl, base, sess)))
        result["session"] = sess
        if result.get("ok"):
            return result
        first = str(result.get("error", ""))
        fresh = _new_session_id()
        retry = probe_exit_ip(_url_from_record(info, render_session_username(tpl, base, fresh)))
        retry["session"] = fresh
        retry["retried"] = True
        if not retry.get("ok"):
            retry["error"] = (f"session {sess} 失败：{first[:160]}；"
                              f"换 session {fresh} 重试仍失败："
                              f"{str(retry.get('error', ''))[:160]}")
        return retry
    return probe_exit_ip(_url_from_record(info))


def probe_account_exit_ip(account: str) -> dict:
    """探测某个号当前真实出口 IP，并记录到 proxy_sessions。"""
    url = proxy_url_for(account)
    result = probe_exit_ip(url)
    record_probe_result(
        account,
        result.get("ip", ""),
        bool(result.get("ok")),
        result.get("error", "") or result.get("isp", ""),
    )
    return result


# ===== 提取型（mode=extract）：调提取接口拿一批可用节点 =====

# 提取接口返回的常见形态：
#   host:port:user:pass      <- ipdeep 等（user 里已编码 session/country）
#   host:port@user:pass
#   user:pass@host:port
#   host:port                <- 无鉴权
_SESSION_TTL_RE = re.compile(r"sessiontime[_-](\d+)", re.I)


def parse_extract_lines(text: str) -> list[dict]:
    """把提取接口的返回文本解析成节点列表。"""
    nodes: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        # 有些接口用逗号/分号分隔多条
        for chunk in re.split(r"[;,\s]+", line):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            node = _parse_node_token(chunk)
            if node:
                nodes.append(node)
    return nodes


def _parse_node_token(token: str) -> dict | None:
    user = password = ""
    hostpart = token
    if "@" in token:
        left, right = token.split("@", 1)
        # user:pass@host:port  还是  host:port@user:pass
        if left.count(":") >= 1 and right.count(":") == 1 and _is_host_port(right):
            user, password = left.split(":", 1)
            hostpart = right
        else:
            hostpart, cred = left, right
            if ":" in cred:
                user, password = cred.split(":", 1)
            else:
                user = cred
    parts = hostpart.split(":")
    if len(parts) < 2:
        return None
    host, port = parts[0].strip(), parts[1].strip()
    if not host or not port.isdigit():
        return None
    rest = parts[2:]
    if not user and rest:
        user = rest[0].strip()
        if len(rest) > 1:
            password = rest[1].strip()
    if not host:
        return None
    ttl = _default_node_ttl()
    m = _SESSION_TTL_RE.search(user)
    if m:
        ttl = int(m.group(1)) * 60
    return {
        "host": host,
        "port": int(port),
        "username": user,
        "password": password,
        "exit_hint": user,
        "ttl": ttl,
    }


def _is_host_port(text: str) -> bool:
    parts = text.split(":")
    return len(parts) == 2 and parts[1].strip().isdigit()


def iprocket_disabled() -> bool:
    """IPRocket 总开关是否处于停用状态（DOLA_IPROCKET_ENABLED=0）。"""
    return os.environ.get("DOLA_IPROCKET_ENABLED", "1").strip().lower() in (
        "0", "false", "no", "off")


def _is_iprocket_url(url: str) -> bool:
    """只认 IPRocket 自己的接口域名 —— 总开关只停 IPRocket，不影响别的代理源。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return "iprocket" in (url or "").lower()
    return host == "iprocket.io" or host.endswith(".iprocket.io")


def fetch_extract_nodes(proxy_id: str, info: dict | None = None) -> dict:
    """调用提取接口，把新节点写进 proxy_nodes（旧节点保留，过期自然淘汰）。

    注意：GenerateLink 类接口通常**按调用次数计费/配额**，所以只在
    需要补节点、且距上次提取超过 extract_interval 时才调用。

    DOLA_IPROCKET_ENABLED=0 时，**指向 IPRocket 的提取链接**直接拒绝（不碰网络）；
    其它代理源的提取链接不受影响（见 three_segment_exit.py 顶部说明）。
    """
    import requests

    info = info or get_proxy(proxy_id)
    if not info:
        return {"ok": False, "added": 0, "error": "代理不存在"}
    url = (info.get("extract_url") or "").strip()
    if not url:
        return {"ok": False, "added": 0, "error": "该代理未配置提取链接"}
    # IPRocket 停用：只拦 IPRocket 的接口（按次计费，失败重试会烧额度）
    if iprocket_disabled() and _is_iprocket_url(url):
        return {"ok": False, "added": 0,
                "error": "IPRocket 已被 DOLA_IPROCKET_ENABLED=0 停用"}
    try:
        resp = requests.get(url, timeout=_extract_timeout())
        resp.raise_for_status()
        text = resp.text
        nodes = parse_extract_lines(text)
        if not nodes:
            _record_extract_result(proxy_id, 0, f"提取结果为空: {text[:120]}")
            return {"ok": False, "added": 0, "error": f"没解析出节点: {text[:120]}"}
        added = _insert_nodes(proxy_id, nodes, info.get("protocol") or "http")
        _record_extract_result(proxy_id, added, "")
        return {"ok": True, "added": added, "total_lines": len(nodes)}
    except Exception as exc:  # noqa: BLE001 - 失败原因要回报给面板
        _record_extract_result(proxy_id, 0, str(exc)[:200])
        return {"ok": False, "added": 0, "error": str(exc)[:200]}


def _record_extract_result(proxy_id: str, count: int, error: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE proxies SET extract_last_at=?, extract_last_count=?, "
            "extract_last_error=? WHERE id=?",
            (time.time(), count, (error or "")[:200], proxy_id),
        )
        conn.commit()
    finally:
        conn.close()


def add_nodes_from_text(proxy_id: str, text: str, ttl: int | None = None) -> dict:
    """手工粘贴节点：一行一个 host:port:user:pass（也认 user:pass@host:port 等形态）。

    ttl=None 时：行里带 sessiontime-N 的按 N 分钟算有效期；
    其余（手工粘贴的固定凭据）默认**不过期**，由操作员自己清理/替换。
    """
    info = get_proxy(proxy_id)
    if not info:
        return {"ok": False, "added": 0, "parsed": 0, "error": "代理不存在"}
    nodes = parse_extract_lines(text)
    if not nodes:
        return {"ok": False, "added": 0, "parsed": 0,
                "error": "没解析出节点，格式应为 host:port:user:pass（一行一个）"}
    for n in nodes:
        if ttl is not None:
            n["ttl"] = int(ttl)
        elif not _SESSION_TTL_RE.search(n.get("username") or ""):
            n["ttl"] = 0
    added = _insert_nodes(proxy_id, nodes, info.get("protocol") or "http")
    return {"ok": True, "added": added, "parsed": len(nodes)}


def _expiry_of(now: float, ttl) -> float:
    """ttl=None 用默认有效期；ttl<=0 表示不过期（0 在 _pick_node 里等价于永不过期）。"""
    ttl = _default_node_ttl() if ttl is None else int(ttl)
    return 0 if ttl <= 0 else now + ttl


def _insert_nodes(proxy_id: str, nodes: list[dict], protocol: str) -> int:
    """写入节点，按 (proxy_id, host, port, username) 去重。"""
    conn = _connect()
    now = time.time()
    added = 0
    try:
        for n in nodes:
            exists = conn.execute(
                "SELECT id FROM proxy_nodes WHERE proxy_id=? AND host=? AND port=? "
                "AND username=?",
                (proxy_id, n["host"], n["port"], n["username"]),
            ).fetchone()
            if exists:
                # 厂商会把同一批凭据再发一次。过期的节点要顺带续期，
                # 否则它会一直躺在表里且永远挑不中（dead 标记保持不动）。
                conn.execute(
                    "UPDATE proxy_nodes SET expires_at=? WHERE id=? AND dead=0",
                    (_expiry_of(time.time(), n.get("ttl")), exists["id"]),
                )
                continue
            conn.execute(
                """INSERT INTO proxy_nodes
                   (proxy_id, protocol, host, port, username, password, exit_hint,
                    created_at, expires_at, dead, fail_count)
                   VALUES (?,?,?,?,?,?,?,?,?,0,0)""",
                (proxy_id, protocol, n["host"], n["port"], n["username"],
                 n["password"], n.get("exit_hint", ""), now,
                 _expiry_of(now, n.get("ttl"))),
            )
            added += 1
        conn.commit()
    finally:
        conn.close()
    return added


def _node_row_to_dict(row) -> dict:
    return dict(row) if row else {}


def list_nodes(proxy_id: str | None = None, alive_only: bool = False) -> list[dict]:
    conn = _connect()
    try:
        sql = "SELECT * FROM proxy_nodes"
        args: list = []
        where = []
        if proxy_id:
            where.append("proxy_id=?")
            args.append(proxy_id)
        if alive_only:
            where.append("dead=0 AND (expires_at=0 OR expires_at>?)")
            args.append(time.time())
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY last_ok_at DESC, created_at DESC"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _url_from_node(node: dict) -> str:
    auth = ""
    if node.get("username"):
        auth = quote(node["username"], safe="")
        if node.get("password"):
            auth += ":" + quote(node["password"], safe="")
        auth += "@"
    return f"{node.get('protocol') or 'http'}://{auth}{node['host']}:{node['port']}"


def _claimed_node_ids(conn: sqlite3.Connection, except_account: str) -> set:
    rows = conn.execute(
        "SELECT node_id FROM proxy_sessions WHERE node_id>0 AND account<>?",
        (except_account,),
    ).fetchall()
    return {r["node_id"] for r in rows}


def _pick_node(proxy_id: str, account: str, exclude_node: int = 0) -> dict | None:
    """挑一个没用过、没过期、没标记死的节点；优先最近成功过的。"""
    now = time.time()
    conn = _connect()
    try:
        claimed = _claimed_node_ids(conn, account)
        rows = conn.execute(
            "SELECT * FROM proxy_nodes WHERE proxy_id=? AND dead=0 "
            "AND (expires_at=0 OR expires_at>?) ORDER BY last_ok_at DESC, created_at ASC",
            (proxy_id, now),
        ).fetchall()
        for row in rows:
            if row["id"] == exclude_node or row["id"] in claimed:
                continue
            return _node_row_to_dict(row)
        return None
    finally:
        conn.close()


def _node_by_id(node_id: int) -> dict | None:
    if not node_id:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM proxy_nodes WHERE id=?", (node_id,)
        ).fetchone()
        return _node_row_to_dict(row)
    finally:
        conn.close()


def _bind_node(account: str, proxy_id: str, node: dict) -> None:
    """把账号钉到某个节点（session 字段存节点标识，便于面板显示）。"""
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO proxy_sessions
               (account, proxy_id, session, rotated_at, last_exit_ip,
                last_probe_at, last_probe_ok, last_probe_detail, node_id)
               VALUES (?,?,?,?,'',0,0,'',?)
               ON CONFLICT(account) DO UPDATE SET
                 proxy_id=excluded.proxy_id,
                 session=excluded.session,
                 rotated_at=excluded.rotated_at,
                 last_exit_ip='',
                 last_probe_at=0,
                 last_probe_ok=0,
                 last_probe_detail='',
                 node_id=excluded.node_id""",
            (account, proxy_id, node.get("exit_hint") or node.get("username") or "",
             time.time(), node["id"]),
        )
        conn.commit()
    finally:
        conn.close()


def _bound_node(account: str) -> dict | None:
    """只读：账号当前绑定的节点（不管死活/过期）。

    与 _ensure_node 的区别：不挑节点、不调提取接口、不探活 ——
    供 egress_key_for(ensure=False) 的面板热路径使用。
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT node_id FROM proxy_sessions WHERE account=?", (account,),
        ).fetchone()
    finally:
        conn.close()
    return _node_by_id(row["node_id"]) if row and row["node_id"] else None


def _ensure_node(account: str, info: dict, rotate: bool = False) -> dict | None:
    """给账号拿到一个可用节点；池子空了就调提取接口补，仍不行返回 None。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT node_id, rotated_at FROM proxy_sessions WHERE account=?",
            (account,),
        ).fetchone()
    finally:
        conn.close()
    cur_node = _node_by_id(row["node_id"]) if row and row["node_id"] else None
    expired = bool(cur_node) and cur_node.get("expires_at") and \
        cur_node["expires_at"] <= time.time()
    if cur_node and not rotate and not expired and not cur_node.get("dead"):
        return cur_node

    node = _pick_usable_node(info["id"], account,
                             exclude_node=(cur_node or {}).get("id", 0), info=info)
    if node is None:
        return None
    _bind_node(account, info["id"], node)
    return node


def _should_extract(info: dict) -> bool:
    """是否允许再调一次提取接口（按 extract_interval 限流，保护配额）。

    没配提取链接的（节点靠手工粘贴的池子）永远不去调。
    """
    if not (info.get("extract_url") or "").strip():
        return False
    interval = int(info.get("extract_interval") or 0)
    last = float(info.get("extract_last_at") or 0)
    if interval <= 0:
        return True
    return (time.time() - last) >= interval


def mark_node_dead(node_id: int, reason: str = "") -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE proxy_nodes SET dead=1, fail_count=fail_count+1 WHERE id=?",
            (node_id,),
        )
        conn.commit()
    finally:
        conn.close()


# 探活目标：CONNECT 通它 = 该节点真的能用（dola 出片/接口都走这个域）。
_PROBE_TARGET = ("www.dola.com", 443)


def is_tls_cert_error(reason: str) -> bool:
    """识别 TLS 被拦截/替换的信号（出口被中间人劫持的典型表现）。

    2026-10-10 实测：IPRocket 少数住宅出口 IP 上跑着 TLS 拦截设备，对 www.dola.com
    会给出两种表现（同一个坏节点两种都出现过）：
      a) 自建 CA 签的伪造证书 → SSLCertVerificationError / certificate verify failed
         （抓到 issuer=CN=K7 Web Proxy,O=K7 Web Proxy,C=CA）；
      b) 直接回明文 HTTP 应答 → SSL: WRONG_VERSION_NUMBER。
    这类节点 CONNECT 是通的、只有 TLS 层会炸，所以必须单独识别出来判死换节点。
    """
    low = (reason or "").lower()
    return any(k in low for k in (
        "sslcertverificationerror",
        "certificate_verify_failed",
        "unable to get local issuer certificate",
        "self-signed certificate",
        "self signed certificate",
        "wrong_version_number",
        "wrong version number",
        "eof occurred in violation of protocol",
        "sslv3 alert handshake failure",
        "tlsv1 alert",
    ))



def _recv_exact(sock, n: int) -> bytes:
    """从 socket 精确读满 n 字节（不足则返回已读到的部分）。

    SOCKS5 应答是变长的，必须按 ATYP 精确读完，否则残留字节会污染后续 TLS 握手。
    """
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except Exception:  # noqa: BLE001
            break
        if not chunk:
            break
        buf += chunk
    return buf


def _probe_node(node: dict, timeout: int = 8) -> tuple[bool, str]:
    """SOCKS5 CONNECT + TLS 验签探测：网关握手（含账密）+ 连探活目标 + 校验证书。

    背景：IPRocket 提取接口会下发实际拒连的网关入口（如 199.190.45.172，
    实测 8/8 节点全部拒连），节点不探活就会把号绑死在坏节点上，
    表现为 chat/completion SOCKS Max retries exceeded / 上游 300 秒无回调。

    2026-10-10 追加 TLS 验签：**CONNECT 通 ≠ 链路可信**。少数住宅出口 IP 上跑着
    TLS 拦截设备，CONNECT 正常放行但会返回伪造证书，只测 CONNECT 会让它通过探活，
    号绑定后所有 HTTPS 全部 CERTIFICATE_VERIFY_FAILED（面板表现：验证「网络软失败」、
    出片静默失败）。这里用默认信任库做一次真实握手，验不过的一律判死。
    """
    try:
        s = socket.create_connection((node["host"], int(node["port"])), timeout=timeout)
        s.settimeout(timeout)
        user = (node.get("username") or "").encode()
        pwd = (node.get("password") or "").encode()
        if user:
            s.sendall(b"\x05\x01\x02")
            resp = s.recv(2)
            if len(resp) < 2 or resp[1] != 0x02:
                s.close()
                return False, "网关不接受账密认证"
            s.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
            resp = s.recv(2)
            if len(resp) < 2 or resp[1] != 0x00:
                s.close()
                return False, "账密认证被拒"
        else:
            s.sendall(b"\x05\x01\x00")
            resp = s.recv(2)
            if len(resp) < 2 or resp[1] != 0x00:
                s.close()
                return False, "网关不接受无认证"
        host = _PROBE_TARGET[0].encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host +
                  struct.pack(">H", _PROBE_TARGET[1]))
        # 必须把 CONNECT 应答**读完整**：VER REP RSV ATYP + BND.ADDR + BND.PORT。
        # 旧实现只 recv(4) 就完事，剩下 6 字节（IPv4 3+域名的地址与端口）留在缓冲区里；
        # 后面一旦接着做 TLS 握手，这 6 个字节会被当成服务端的 ServerHello 读掉，
        # 于是每个节点都报 "SSL: WRONG_VERSION_NUMBER"（假阳性）。
        head = _recv_exact(s, 4)
        rep = head[1] if len(head) >= 2 else 0xFF
        if rep != 0x00:
            s.close()
            return False, "CONNECT 被拒(0x%02x)" % rep
        atyp = head[3]
        if atyp == 0x01:
            _recv_exact(s, 4 + 2)
        elif atyp == 0x04:
            _recv_exact(s, 16 + 2)
        elif atyp == 0x03:
            ln = _recv_exact(s, 1)
            _recv_exact(s, (ln[0] if ln else 0) + 2)
        # TLS 验签：证书链必须能在本地信任库里走通（这里绝不关校验）。
        try:
            tls_ctx = ssl.create_default_context()
            with tls_ctx.wrap_socket(s, server_hostname=_PROBE_TARGET[0]) as tls:
                tls.getpeercert()
            s.close()
        except ssl.SSLCertVerificationError as exc:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
            return False, "TLS 证书校验失败(疑似中间人劫持): %s" % str(exc)[:90]
        except Exception as exc:  # noqa: BLE001
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
            return False, "TLS 握手失败: %s" % str(exc)[:90]
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:120]


def _pick_usable_node(proxy_id: str, account: str, exclude_node: int = 0,
                      info: dict | None = None) -> dict | None:
    """挑节点 + 探活：没验证过的新节点先探测，坏节点当场标死并换下一个。

    已验证过（last_ok_at>0）的节点跳过探测直接用；池子空了按需调提取接口。
    """
    exclude = exclude_node
    for _ in range(5):
        node = _pick_node(proxy_id, account, exclude_node=exclude)
        if node is None and info is not None and _should_extract(info):
            fetch_extract_nodes(proxy_id, info)
            node = _pick_node(proxy_id, account, exclude_node=exclude)
        if node is None:
            return None
        if node.get("last_ok_at"):
            return node
        ok, detail = _probe_node(node)
        if ok:
            mark_node_ok(node["id"])
            node["last_ok_at"] = time.time()
            return node
        mark_node_dead(node["id"], "探活失败: " + detail)
        exclude = node["id"]
    return None


def mark_node_ok(node_id: int, exit_ip: str = "") -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE proxy_nodes SET last_ok_at=?, last_exit_ip=?, fail_count=0 WHERE id=?",
            (time.time(), exit_ip or "", node_id),
        )
        conn.commit()
    finally:
        conn.close()


def prune_nodes(proxy_id: str | None = None) -> int:
    """清掉过期和连续失败的节点，避免池子无限膨胀。"""
    conn = _connect()
    try:
        now = time.time()
        # 注意括号：AND proxy_id=? 必须作用在整个 OR 组上。
        # 少了这层括号时 AND 的优先级更高，条件会变成
        #   dead=1 OR expired OR (fail_count>=3 AND proxy_id=?)
        # 于是 prune 一个代理会把别的代理的节点池一起清掉。
        sql = ("DELETE FROM proxy_nodes WHERE (dead=1 "
               "OR (expires_at>0 AND expires_at<=?) OR fail_count>=3)")
        args: list = [now]
        if proxy_id:
            sql += " AND proxy_id=?"
            args.append(proxy_id)
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
