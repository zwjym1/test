"""公共工具：免登录请求、作品 ID 解析、字段状态常量。不带任何账号 cookie，不做签名逆向。"""
import datetime as dt, re, sys, time
import requests

TZ = dt.timezone(dt.timedelta(hours=8))
UA_M = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
BASE = {"aid": "581610", "channel": "channel_pc_web", "platform": "PC", "device_platform": "webapp"}
# 每个字段的状态取值（与输出 JSON 的 status 一致）
STATUSES = ("available", "partial", "observed_empty", "missing", "blocked_login",
            "blocked_verification", "error", "not_requested")
S = requests.Session()


def T(sec):
    return dt.datetime.fromtimestamp(int(sec), TZ).strftime("%Y-%m-%d %H:%M") if sec else ""


def now():
    return dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S (UTC+8)")


def log(*a):
    print("[douyin-field-collect]", *a, file=sys.stderr, flush=True)


class Blocked(Exception):
    """接口返回非 JSON（验证码页 / 登录墙）。status 字段为 blocked_verification 或 blocked_login。"""
    def __init__(self, status, detail=""):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


def get_json(url, params, ref="https://so.douyin.com/", ua=UA_M, tries=2):
    """GET 公开接口。返回 dict；连续拿到非 JSON 时抛 Blocked（不换身份、不硬重试）。"""
    last = ""
    for k in range(tries):
        try:
            r = S.get(url, params=params, headers={"User-Agent": ua, "Referer": ref}, timeout=20)
            t = r.text.strip()
            if t.startswith("{"):
                return r.json()
            low = t[:3000].lower()
            if r.status_code in (401,) or "login" in low or "登录" in t[:3000]:
                last = f"blocked_login|HTTP {r.status_code}"
            elif r.status_code in (403, 429) or "verify" in low or "验证" in t[:3000] or not t:
                last = f"blocked_verification|HTTP {r.status_code} {len(t)}B"
            else:
                last = f"error|HTTP {r.status_code} 非JSON {len(t)}B"
        except requests.RequestException as e:
            last = f"error|{type(e).__name__}"
        time.sleep(1.5 * (k + 1))
    st, _, d = last.partition("|")
    raise Blocked(st, d)


def vid_of(s):
    s = s.strip()
    m = re.search(r"https?://\S+", s)
    u = m.group(0) if m else s
    m = re.search(r"(?:video/|modal_id=|note/|aweme_id=)(\d{15,21})", u) or re.fullmatch(r"(\d{15,21})", u)
    if m:
        return m.group(1)
    if "v.douyin.com" in u or "iesdouyin.com" in u:
        r = S.get(u, headers={"User-Agent": UA_M}, allow_redirects=False, timeout=15)
        m = re.search(r"/(?:video|note)/(\d{15,21})", r.headers.get("location", ""))
        if m:
            return m.group(1)
    raise SystemExit(f"未识别到作品ID：{s}")


def launch_sync(p):
    """优先用系统 Chrome；没有就用 `playwright install chromium` 装的 Chromium。"""
    try:
        return p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        return p.chromium.launch(headless=True)
