"""media.py — 抖音视频下载（多源回退）、截帧拼图、本地 ASR。全部免登录、不读浏览器 cookie。
下载顺序：iesdouyin 公开分享页 → dlpanda.com 网页表单 → snapany.com（无头浏览器正常操作网页）→ yt-dlp。
任一源 403/失败就换下一个；全部失败如实返回 status=error 与每个源的失败原因。
"""
import html, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from dy_common import UA_M, UA_PC, log

FONT_CANDIDATES = ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
                   "/System/Library/Fonts/PingFang.ttc", "C:/Windows/Fonts/msyh.ttc"]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _curl(args, timeout=40):
    r = run(["curl", "-sS", "-m", str(timeout), "-A", UA_PC] + args)
    if r.returncode:
        raise RuntimeError(f"curl rc={r.returncode}")
    return r.stdout


def _download(url, path, headers):
    cmd = ["curl", "-sSL", "--fail", "-m", "900", "-C", "-", "-o", path, url]
    for k, v in headers.items():
        cmd[1:1] = ["-H", f"{k}: {v}"]
    for _ in range(3):
        r = run(cmd)
        if r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 10000:
            return
    if os.path.exists(path):
        os.remove(path)
    m = re.search(r"error: (\d{3})", r.stderr)
    raise RuntimeError(f"下载失败 HTTP {m.group(1) if m else '?'}")


def src_share_page(vid):
    req = urllib.request.Request(f"https://www.iesdouyin.com/share/video/{vid}/", headers={"User-Agent": UA_M, "Referer": "https://www.douyin.com/"})
    h = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    mm = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", h, re.S)
    if not mm:
        raise RuntimeError("分享页无作品数据（海外 IP 常见）")
    for val in json.loads(mm.group(1)).get("loaderData", {}).values():
        if isinstance(val, dict) and (val.get("videoInfoRes") or {}).get("item_list"):
            it = val["videoInfoRes"]["item_list"][0]
            return it["video"]["play_addr"]["url_list"][0].replace("playwm", "play"), {"User-Agent": UA_M, "Referer": "https://www.douyin.com/"}
    raise RuntimeError("分享页无 item_list")


def src_dlpanda(vid):
    url = f"https://www.douyin.com/video/{vid}"
    fd, jar = tempfile.mkstemp(suffix=".jar"); os.close(fd)
    try:   # 站点自己的会话 cookie 只在本次请求内使用，随后删除
        page = _curl(["-c", jar, "-b", jar, "https://dlpanda.com/zh-CN"])
        t = re.search(r'name="_token" value="([^"]+)"', page); t0 = re.search(r'name="t0ken"[^>]*value="([^"]+)"', page)
        if not t:
            raise RuntimeError("表单 token 未找到（页面改版？）")
        res = _curl(["-c", jar, "-b", jar, "-e", "https://dlpanda.com/zh-CN", "-X", "POST", "https://dlpanda.com/zh-CN",
                     "--data-urlencode", f"_token={t.group(1)}", "--data-urlencode", f"url={url}",
                     "--data-urlencode", f"t0ken={t0.group(1) if t0 else ''}"], 60)
    finally:
        os.remove(jar)
    m = re.search(r'["\'](?:https?:)?(//[^"\'<\s]*(?:zjcdn\.com|douyinvod\.com|amemv\.com|bytecdn\.cn)[^"\'<\s]*)', res)
    if not m:
        raise RuntimeError("结果页无视频直链")
    return "https:" + html.unescape(m.group(1)), {"User-Agent": UA_PC}


def src_snapany(vid):
    from playwright.sync_api import sync_playwright
    from dy_common import launch_sync
    vre = re.compile(r"(douyinvod\.com|zjcdn\.com|amemv\.com|bytecdn)", re.I)
    with sync_playwright() as p:
        b = launch_sync(p)
        pg = b.new_context(locale="zh-CN", user_agent=UA_PC).new_page()
        pg.goto("https://snapany.com/zh", timeout=45000, wait_until="domcontentloaded")
        inp = pg.locator("input:not([type=hidden]):visible, textarea:visible").first
        inp.wait_for(timeout=20000); inp.fill(f"https://www.douyin.com/video/{vid}"); inp.press("Enter")
        links = []
        for _ in range(40):
            pg.wait_for_timeout(1000)
            links = [h for h in pg.eval_on_selector_all("a[href]", "els=>els.map(e=>e.href)") if vre.search(h)]
            if links:
                break
        b.close()
    if not links:
        raise RuntimeError("40 秒内页面未出现下载链接")
    return links[0], {"User-Agent": UA_PC}


def src_ytdlp(vid, od):
    r = run(["yt-dlp", "--no-playlist", "-f", "b[height<=720]/b", "-o", f"{od}/video.%(ext)s", f"https://www.douyin.com/video/{vid}"], timeout=600)
    if r.returncode:
        raise RuntimeError("yt-dlp: " + ((r.stderr.strip().splitlines() or ["?"])[-1])[:120])
    for f in os.listdir(od):
        if f.startswith("video."):
            return os.path.join(od, f)
    raise RuntimeError("yt-dlp 无输出文件")


def download_video(vid, od):
    """返回 (path|None, media_state)。"""
    tried = []
    for name, fn in [("iesdouyin_share", src_share_page), ("dlpanda", src_dlpanda), ("snapany", src_snapany), ("yt-dlp", None)]:
        t0 = time.time()
        try:
            if fn is None:
                path = src_ytdlp(vid, od)
            else:
                url, hdr = fn(vid); path = os.path.join(od, "video.mp4"); _download(url, path, hdr)
            return path, {"status": "available", "source": name, "seconds": round(time.time() - t0, 1), "tried": tried}
        except Exception as e:
            msg = str(e)[:160]
            # 只记原因，不记带签名的直链
            tried.append({"source": name, "reason": re.sub(r"https?://\S+", "<url>", msg)})
            log(f"下载源 {name} 失败：{tried[-1]['reason']}，换下一个")
    return None, {"status": "error", "reason": "all_sources_failed", "tried": tried}


def probe(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", path])
    j = json.loads(r.stdout)
    vs = [s for s in j["streams"] if s["codec_type"] == "video"]
    return float(j["format"]["duration"]), ((vs[0]["width"], vs[0]["height"]) if vs else (0, 0)), any(s["codec_type"] == "audio" for s in j["streams"])


def fmt(t):
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def tile(imgs, labels, out, cols=4):
    from PIL import Image, ImageDraw, ImageFont
    ims = [Image.open(i).convert("RGB") for i in imgs]
    w, h = max(i.width for i in ims), max(i.height for i in ims)
    sheet = Image.new("RGB", (cols * w, ((len(ims) + cols - 1) // cols) * h), "black"); d = ImageDraw.Draw(sheet)
    f = None
    for fp in FONT_CANDIDATES:
        try:
            f = ImageFont.truetype(fp, max(14, w // 14)); break
        except OSError:
            pass
    f = f or ImageFont.load_default()
    for k, (im, lb) in enumerate(zip(ims, labels)):
        x, y = (k % cols) * w, (k // cols) * h; sheet.paste(im, (x, y))
        bb = d.textbbox((x + 4, y + 4), lb, font=f); d.rectangle(bb, fill="black"); d.text((x + 4, y + 4), lb, fill="yellow", font=f)
    sheet.save(out, quality=85)


def frames(video, od, hook_sec=5.0, n=16):
    """开头 0–hook_sec 秒每 0.5 秒一帧（hook_sheet.jpg）+ 全片均匀 n 帧（contact_sheet.jpg）。"""
    dur, (w, h), has_a = probe(video)
    fd = os.path.join(od, "frames"); os.makedirs(fd, exist_ok=True)
    width = 300 if h > w else 400
    def grab(t, p):
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", p])
        return os.path.exists(p)
    hk = [(p, fmt(t)) for i, t in enumerate([x * 0.5 for x in range(int(hook_sec * 2) + 1)] or [0])
          if t < dur and grab(max(t, 0.05), p := os.path.join(fd, f"h{i:02d}_{t:.1f}.jpg"))]
    if hk:
        tile([a for a, _ in hk], [b for _, b in hk], os.path.join(od, "hook_sheet.jpg"), cols=6 if h > w else 4)
    un = [(p, fmt(t)) for i, t in enumerate([(i + 0.5) * dur / n for i in range(n)])
          if grab(t, p := os.path.join(fd, f"u{i:02d}_{t:07.2f}.jpg"))]
    if un:
        tile([a for a, _ in un], [b for _, b in un], os.path.join(od, "contact_sheet.jpg"), cols=6 if h > w else 4)
    return {"status": "available" if hk else "error", "duration_s": round(dur, 2), "resolution": f"{w}x{h}", "has_audio": has_a,
            "hook_frames": len(hk), "uniform_frames": len(un),
            "files": [f for f in ("hook_sheet.jpg", "contact_sheet.jpg") if os.path.exists(os.path.join(od, f))]}


def asr(video, od, model="small"):
    """faster-whisper 本地识别（CPU，int8）。结果是机器转写，需人工复核。
    音频用 ffmpeg 解码成 16k 单声道数组再喂给模型，不依赖 PyAV 版本。"""
    import numpy as np
    from faster_whisper import WhisperModel
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", video, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"], capture_output=True)
    audio = np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0
    m = WhisperModel(model, device="cpu", compute_type="int8")
    kw = dict(language="zh", beam_size=1, initial_prompt="以下是普通话的句子，使用简体中文和标点。")
    segs, info = m.transcribe(audio, vad_filter=True, **kw)
    segs, note = list(segs), "vad"
    if not segs:   # VAD 会把歌唱/强 BGM 当成非语音，整段丢掉；关掉 VAD 再跑一次
        segs, info = m.transcribe(audio, vad_filter=False, **kw)
        segs, note = list(segs), "vad_empty_retried_without_vad（可能是歌曲/BGM 人声）"
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs if s.text.strip()]
    return segs, f"{info.language}; {note}"
