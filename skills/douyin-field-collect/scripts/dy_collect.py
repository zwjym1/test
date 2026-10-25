#!/usr/bin/env python3
"""dy_collect.py — 单条抖音视频的“实地数据”采集（免登录），输出带字段状态的 collect.json + report.md
用法：
  python dy_collect.py video <链接|作品ID> [-o 输出根目录] [-n 200] [--replies] [--reply-n 20]
                       [--kw 关键词] [--browse] [--media] [--asr] [--asr-model small] [--hook-sec 5] [--no-bgm-audio]
  python dy_collect.py sug <关键词> [--rel]      # 只取搜索框联想词（--rel 额外取 so.douyin 结果页“大家还在搜”）
字段与状态：
  subtitle          先取抖音自带字幕（source=platform_caption，原文）；拿不到且开了 --media --asr 才走本地 ASR（source=local_asr，需复核）
  comments/replies  已采唯一数、平台 total、has_more、stop_reason
  recommendations   按来源分开：comment_top_rec / detail_suggest_words(按 scene) / page_suggest_words(--browse) /
                    related_videos(--browse) / search_sug(--kw) / search_page_related(--kw 且 sug --rel)
  media             --media 时下载视频并截帧（开头 hook + 全片均匀帧）
  bgm               音乐元数据（标题/作者/音乐ID/原声或曲库/音乐页/试听链接，逐项 status）；--media 时另做本地音频分析
                    （BPM、拍点、强拍、切镜与拍点对齐、粗略风格桶），--no-bgm-audio 跳过音频分析
  每项 status ∈ available/partial/observed_empty/missing/blocked_login/blocked_verification/error/not_requested
"""
import argparse, json, os, re, sys, time, urllib.parse as up
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dy_common import BASE, UA_PC, UA_M, S, T, now, log, get_json, vid_of, Blocked, launch_sync

API = "https://so.douyin.com/aweme/v1/web"


def st(status, **kw):
    return {"status": status, **kw}


# ---------- 详情 ----------
def detail(vid):
    try:
        j = get_json(f"{API}/aweme/detail/", {**BASE, "aweme_id": vid})
    except Blocked as e:
        return None, st(e.status, reason=e.detail)
    a = j.get("aweme_detail")
    if not a:
        return None, st("missing", reason=f"aweme_detail 为空（删除/私密/地区限制）status_code={j.get('status_code')}")
    return a, st("available")


def summarize(a, vid):
    s, au, mu, vv = a.get("statistics") or {}, a.get("author") or {}, a.get("music") or {}, a.get("video") or {}
    di = ((a.get("video_control") or {}).get("download_info") or {})
    return {"id": vid, "url": f"https://www.douyin.com/video/{vid}", "desc": a.get("desc", ""), "published": T(a.get("create_time")) + " (UTC+8)",
            "duration_s": round((a.get("duration") or vv.get("duration") or 0) / 1000, 1),
            "likes": s.get("digg_count"), "comments": s.get("comment_count"), "collects": s.get("collect_count"), "shares": s.get("share_count"),
            "hashtags": [t["hashtag_name"] for t in a.get("text_extra") or [] if t.get("hashtag_name")],
            "bgm": {"title": mu.get("title"), "author": mu.get("author")}, "author": au.get("nickname"),
            "is_image_post": bool(a.get("images")), "is_subtitled": a.get("is_subtitled"),
            "platform_download": {"allowed": not di.get("fail_info"), "reason": (di.get("fail_info") or {}).get("reason")}}


# ---------- 字幕：平台自带优先 ----------
def subtitle_source(a):
    """字段路径参考：interaction_stickers[].auto_video_caption_info.auto_captions[].url、video.cla_info.caption_infos[].url、subtitle_infos"""
    v = a.get("video") or {}
    for i, sk in enumerate(a.get("interaction_stickers") or []):
        for k, c in enumerate(((sk or {}).get("auto_video_caption_info") or {}).get("auto_captions") or []):
            u = c.get("url") or {}
            urls = (u.get("url_list") or []) if isinstance(u, dict) else [u]
            if urls and urls[0]:
                return urls[0], f"interaction_stickers[{i}].auto_video_caption_info.auto_captions[{k}]", c.get("language")
    for i, c in enumerate((v.get("cla_info") or {}).get("caption_infos") or []):
        u = c.get("url") or (c.get("url_list") or [None])[0]
        if u:
            return u, f"video.cla_info.caption_infos[{i}]", c.get("lang") or c.get("language_code_name")
    for owner, pre in ((v, "video.subtitle_infos"), (a, "subtitle_infos")):
        for i, c in enumerate(owner.get("subtitle_infos") or []):
            u = c.get("url") or (c.get("url_list") or [None])[0]
            if u:
                return u, f"{pre}[{i}]", c.get("language_code_name")
    return None, None, None


def parse_segments(text):
    try:
        d = json.loads(text)
    except ValueError:
        d = None
    segs = []
    if d is not None:
        items = d if isinstance(d, list) else (d.get("utterances") or d.get("segments") or d.get("body") or [])
        for it in items:
            ms = "start_time" in it
            s0 = float(it.get("start_time", it.get("start", it.get("from", 0)))) / (1000 if ms else 1)
            s1 = float(it.get("end_time", it.get("end", it.get("to", s0)))) / (1000 if ms else 1)
            t = str(it.get("text", it.get("content", ""))).strip()
            if t:
                segs.append({"start": round(s0, 2), "end": round(s1, 2), "text": t})
        return segs
    def sec(x):
        p = x.replace(",", ".").split(":")
        return sum(float(v) * 60 ** i for i, v in enumerate(reversed(p)))
    for m in re.finditer(r"((?:\d{2}:)?\d{2}:\d{2}[,.]\d{3})\s*-->\s*((?:\d{2}:)?\d{2}:\d{2}[,.]\d{3})[^\n]*\n(.*?)(?=\n\s*\n|\Z)", text, re.S):
        segs.append({"start": round(sec(m[1]), 2), "end": round(sec(m[2]), 2), "text": m[3].strip()})
    return segs


def platform_subtitle(a):
    url, path, lang = subtitle_source(a)
    if not url:
        return st("missing", source="platform_caption", reason=f"详情无字幕字段（is_subtitled={a.get('is_subtitled')}）")
    try:
        r = S.get(url, headers={"User-Agent": UA_M, "Referer": "https://www.douyin.com/"}, timeout=20)
        if r.status_code in (403, 429):
            return st("blocked_verification", source="platform_caption", field_path=path, reason=f"HTTP {r.status_code}")
        segs = parse_segments(r.content.decode("utf-8-sig", "replace"))
    except Exception as e:
        return st("error", source="platform_caption", field_path=path, reason=type(e).__name__)
    return st("available" if segs else "observed_empty", source="platform_caption", field_path=path, language=lang,
              needs_review=False, segments=segs, text="\n".join(s["text"] for s in segs))


# ---------- 评论 ----------
def _c(c, parent=""):
    u = c.get("user") or {}
    return {"cid": c.get("cid"), "parent": parent, "user": u.get("nickname"), "like": c.get("digg_count", 0), "text": c.get("text", ""),
            "time": T(c.get("create_time")), "ip": c.get("ip_label", ""), "reply_total": c.get("reply_comment_total", 0)}


def _page_loop(url, params, limit, parent=""):
    """通用分页：返回 (items, state)。stop_reason: no_more / limit_reached / empty_page / cursor_stuck / blocked / api_error"""
    items, seen, cur, total, has_more, pages, reason, status = [], set(), 0, None, None, 0, "", None
    while len(items) < limit:
        try:
            j = get_json(url, {**params, "cursor": cur, "count": 20})
        except Blocked as e:
            status, reason = e.status, "blocked"; break
        if j.get("status_code") not in (0, None):
            status, reason = "error", "api_error"; break
        pages += 1; total = j.get("total", total); has_more = bool(j.get("has_more"))
        got = j.get("comments") or []
        for c in got:
            if c.get("cid") in seen:
                continue
            seen.add(c.get("cid")); items.append(_c(c, parent))
        if not has_more:
            reason = "no_more"; break
        if not got:
            reason = "empty_page"; break
        nc = j.get("cursor")
        if nc in (None, cur):
            reason = "cursor_stuck"; break
        cur = nc; time.sleep(0.8)
    else:
        reason = "limit_reached"
    items = items[:limit]
    if status is None:
        if not items:
            status = "observed_empty" if (total in (0, None) and reason == "no_more") else ("partial" if pages else "missing")
        else:
            status = "available" if reason == "no_more" else "partial"
    if status in ("blocked_login", "blocked_verification", "error") and items:
        status = "partial"
    return items, {"status": status, "collected_unique": len(items), "platform_total": total, "has_more": has_more,
                   "pages": pages, "stop_reason": reason}


def comments(vid, n=200, replies=False, reply_n=20, reply_parents=10):
    tops, cst = _page_loop(f"{API}/comment/list/", {**BASE, "aweme_id": vid}, n)
    cst["limit"] = n
    rst = st("not_requested")
    if replies:
        allr, per = [], []
        parents = sorted([c for c in tops if c["reply_total"]], key=lambda c: -c["like"])[:reply_parents]
        for c in parents:
            rs, s1 = _page_loop(f"{API}/comment/list/reply/", {**BASE, "item_id": vid, "comment_id": c["cid"]}, reply_n, c["cid"])
            allr += rs; per.append({"parent": c["cid"], **s1}); time.sleep(0.6)
        sts = {p["status"] for p in per}
        status = ("observed_empty" if not parents else "available" if sts <= {"available", "observed_empty"} else
                  "partial" if allr else next(iter(sts - {"available"}), "error"))
        rst = st(status, parents_expanded=len(parents), collected_unique=len(allr),
                 platform_total=sum(c["reply_total"] for c in parents), per_parent_limit=reply_n, per_parent=per)
        tops = tops + allr
    return tops, cst, rst


# ---------- 推荐词（按来源分开，不混） ----------
def detail_recs(a):
    """aweme_detail.suggest_words 按 scene 拆：comment_top_rec 单列，其余放 detail_suggest_words。"""
    sw = a.get("suggest_words")
    top = st("missing", reason="详情无 suggest_words 字段")
    scenes = {}
    if isinstance(sw, dict):
        top = st("missing", reason="suggest_words 中无 comment_top_rec 场景")
        for g in sw.get("suggest_words") or []:
            words = list(dict.fromkeys(w.get("word") for w in g.get("words") or [] if w.get("word")))
            ent = st("available" if words else "observed_empty", hint_text=g.get("hint_text") or "", words=words)
            if g.get("scene") == "comment_top_rec":
                top = {**ent, "field_path": "aweme_detail.suggest_words[scene=comment_top_rec]", "meaning": "评论区顶部“大家都在搜”"}
            else:
                scenes[g.get("scene") or "unknown"] = ent
    dsw = st("available" if any(v["words"] for v in scenes.values()) else ("observed_empty" if scenes else "missing"),
             field_path="aweme_detail.suggest_words[scene!=comment_top_rec]", scenes=scenes)
    return top, dsw


def browse(vid, wait_ms=9000):
    """无头浏览器以游客身份正常打开视频弹窗页，只读页面自己发出的请求：/aweme/related/（相关推荐视频）与 suggest_words 接口（看后搜/页面级推荐词）。"""
    from playwright.sync_api import sync_playwright
    got = {"related": [], "suggest": []}
    with sync_playwright() as p:
        b = launch_sync(p)
        pg = b.new_context(viewport={"width": 1366, "height": 900}, user_agent=UA_PC, locale="zh-CN").new_page()
        def on(r):
            if r.request.resource_type not in ("xhr", "fetch"):
                return
            key = "related" if "/aweme/related/" in r.url else ("suggest" if "suggest_word" in r.url else None)
            if key:
                try:
                    got[key].append((r.status, r.json()))
                except Exception:
                    got[key].append((r.status, None))
        pg.on("response", on)
        pg.goto(f"https://www.douyin.com/jingxuan?modal_id={vid}", wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(wait_ms)
        title = pg.title()
        b.close()
    gate = "blocked_verification" if "验证" in title else None
    def judge(lst, n_items):
        if n_items:
            return "available"
        if gate:
            return gate
        if any(s in (401, 403) for s, _ in lst):
            return "blocked_login"
        return "observed_empty" if lst else "missing"   # 页面没发出该请求（游客态常见）
    rel = []
    for _, j in got["related"]:
        for x in (j or {}).get("aweme_list") or []:
            s = x.get("statistics") or {}
            rel.append({"url": f"https://www.douyin.com/video/{x['aweme_id']}", "desc": (x.get("desc") or "")[:60],
                        "author": (x.get("author") or {}).get("nickname"), "likes": s.get("digg_count"), "published": T(x.get("create_time"))})
    rel = list({r["url"]: r for r in rel}.values())
    groups = []
    for _, j in got["suggest"]:
        for g in (j or {}).get("data") or []:
            if isinstance(g, dict):
                words = list(dict.fromkeys(w.get("word") if isinstance(w, dict) else w for w in g.get("words") or []))
                groups.append({"scene": g.get("scene"), "type": g.get("type"), "hint_text": g.get("hint_text"), "words": [w for w in words if w]})
    nw = sum(len(g["words"]) for g in groups)
    return (st(judge(got["suggest"], nw), source="www.douyin.com 页面自发 suggest_words 接口（看后搜/页面级）",
               attribution="page_level：页面级接口，可能混入弹窗外其他视频的词，未逐条校验归属", responses=len(got["suggest"]), groups=groups),
            st(judge(got["related"], len(rel)), source="www.douyin.com 页面自发 /aweme/related/", responses=len(got["related"]), items=rel))


def search_sug(kw):
    try:
        j = get_json("https://www.douyin.com/aweme/v1/web/search/sug/", {**BASE, "aid": "6383", "keyword": kw}, ref="https://www.douyin.com/", ua=UA_PC)
    except Blocked as e:
        return st(e.status, keyword=kw, reason=e.detail)
    words = [x.get("content") for x in j.get("sug_list") or [] if x.get("content")]
    return st("available" if words else "observed_empty", keyword=kw, source="www.douyin.com search/sug（搜索框联想）", words=words)


def search_page_related(kw):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = launch_sync(p); ctx = b.new_context(**p.devices["iPhone 13"], locale="zh-CN"); pg = ctx.new_page()
        pg.goto("https://so.douyin.com/s?keyword=" + up.quote(kw), wait_until="domcontentloaded", timeout=45000); pg.wait_for_timeout(6000)
        title, txt = pg.title(), pg.inner_text("body"); b.close()
    if "验证" in title:
        return st("blocked_verification", keyword=kw)
    lines, rel = [l.strip() for l in txt.split("\n")], []
    for i, l in enumerate(lines):
        if l in ("大家还在搜", "相关搜索", "猜你想搜"):
            for w in lines[i + 1:i + 16]:
                if not w:
                    continue
                if w in ("更多搜索结果", "抖音搜索", "收藏", "分享") or re.fullmatch(r"[\d.w万:：\-年月日天前 ]+", w) or len(w) > 30:
                    break
                rel.append(w)
    rel = list(dict.fromkeys(rel))
    return st("available" if rel else "observed_empty", keyword=kw, source="so.douyin.com 结果页“大家还在搜/相关搜索”", words=rel)


# ---------- 主流程 ----------
def collect(a_):
    vid = vid_of(a_.target)
    od = os.path.join(a_.out, f"douyin_{vid}"); os.makedirs(od, exist_ok=True)
    res = {"platform": "douyin", "video_id": vid, "collected_at": now(), "login": "none（游客/公开接口）"}
    raw, dst = detail(vid)
    res["detail"] = {**dst, **(summarize(raw, vid) if raw else {})}
    log("详情", dst["status"])
    # 字幕
    sub = platform_subtitle(raw) if raw else st("missing", source="platform_caption", reason="详情不可用")
    log("平台字幕", sub["status"])
    # 评论
    if a_.n > 0:
        cm, cst, rst = comments(vid, a_.n, a_.replies, a_.reply_n)
        json.dump(cm, open(os.path.join(od, "comments.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        cst["file"] = "comments.json"
    else:
        cm, cst, rst = [], st("not_requested"), st("not_requested")
    res["comments"], res["replies"] = cst, rst
    log("评论", cst["status"], cst.get("collected_unique"), "/", cst.get("platform_total"))
    # 推荐词
    top, dsw = detail_recs(raw) if raw else (st("missing", reason="详情不可用"), st("missing", reason="详情不可用"))
    recs = {"comment_top_rec": top, "detail_suggest_words": dsw,
            "page_suggest_words": st("not_requested", hint="加 --browse"), "related_videos": st("not_requested", hint="加 --browse"),
            "search_sug": st("not_requested", hint="加 --kw 关键词"), "search_page_related": st("not_requested", hint="加 --kw 与 --search-rel")}
    if a_.browse:
        try:
            recs["page_suggest_words"], recs["related_videos"] = browse(vid)
        except Exception as e:
            recs["page_suggest_words"] = recs["related_videos"] = st("error", reason=type(e).__name__)
    if a_.kw:
        recs["search_sug"] = search_sug(a_.kw)
        if a_.search_rel:
            try:
                recs["search_page_related"] = search_page_related(a_.kw)
            except Exception as e:
                recs["search_page_related"] = st("error", reason=type(e).__name__)
    res["recommendations"] = recs
    # 媒体 / 截帧 / ASR
    res["media"] = st("not_requested", hint="加 --media")
    import bgm
    ms, meta = bgm.music_meta(raw)
    aa_hint = "已加 --no-bgm-audio" if a_.no_bgm_audio else "加 --media"
    res["bgm"] = {"status": ms, "meta": meta, "audio_analysis": st("not_requested", hint=aa_hint),
                  "style": st("not_requested", hint=aa_hint, note=bgm.HEURISTIC_NOTE)}
    log("BGM 元数据", ms)
    if a_.media:
        import media
        path, mst = media.download_video(vid, od)
        if path:
            try:
                mst["frames"] = media.frames(path, od, a_.hook_sec)
            except Exception as e:
                mst["frames"] = st("error", reason=type(e).__name__)
            if sub["status"] != "available" and a_.asr:
                if not mst["frames"].get("has_audio", True):
                    sub = st("observed_empty", source="local_asr", reason="视频无音轨", platform_caption=sub)
                else:
                    t0 = time.time()
                    try:
                        segs, lang = media.asr(path, od, a_.asr_model)
                        sub = st("available" if segs else "observed_empty", source="local_asr", needs_review=True, model=f"faster-whisper {a_.asr_model}",
                                 language=lang, seconds=round(time.time() - t0, 1), platform_caption=sub,
                                 segments=segs, text="\n".join(s["text"] for s in segs))
                    except Exception as e:
                        sub = st("error", source="local_asr", reason=type(e).__name__, platform_caption=sub)
            if not a_.no_bgm_audio:
                t0 = time.time()
                try:
                    aa, sty = bgm.analyze(path, a_.hook_sec, a_.scene_thr, speech_segments=sub.get("segments") if sub["status"] == "available" else None)
                    aa["seconds"] = round(time.time() - t0, 1)
                except Exception as e:
                    aa = sty = st("error", reason=f"{type(e).__name__}: {str(e)[:120]}")
                res["bgm"]["audio_analysis"], res["bgm"]["style"] = aa, sty
                log("BGM 音频分析", aa["status"], aa.get("bpm"), "BPM；风格", sty.get("label"))
            if not a_.keep_video:
                os.remove(path); mst["video_file"] = "已删除（加 --keep-video 保留）"
            else:
                mst["video_file"] = os.path.basename(path)
        elif not a_.no_bgm_audio:
            res["bgm"]["audio_analysis"] = st("missing", reason="视频没下载到，无法分析音频")
            res["bgm"]["style"] = st("missing", reason="视频没下载到", note=bgm.HEURISTIC_NOTE)
        res["media"] = mst
    res["subtitle"] = sub
    json.dump(res, open(os.path.join(od, "collect.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(os.path.join(od, "report.md"), "w", encoding="utf-8").write(report(res, cm))
    print(json.dumps({"out": od, "detail": dst["status"], "subtitle": [sub["status"], sub.get("source")], "comments": cst.get("status"),
                      "recommendations": {k: v["status"] for k, v in recs.items()}, "media": res["media"]["status"],
                      "bgm": {"meta": res["bgm"]["status"], "audio": res["bgm"]["audio_analysis"]["status"], "bpm": res["bgm"]["audio_analysis"].get("bpm"),
                              "style": res["bgm"]["style"].get("label")}}, ensure_ascii=False))


def fmt_t(s):
    return f"{int(s // 60):02d}:{s % 60:04.1f}"


def report(r, cm):
    d = r["detail"]; L = [f"# 抖音实地数据 {r['video_id']}", "", f"- 采集时间：{r['collected_at']}（免登录）", f"- 详情：{d['status']}"]
    if d["status"] == "available":
        L += [f"- 链接：{d['url']}", f"- 作者：{d['author']}  发布：{d['published']}  时长：{d['duration_s']}s",
              f"- 赞 {d['likes']} / 评 {d['comments']} / 藏 {d['collects']} / 转 {d['shares']}", f"- 文案：{d['desc']}",
              f"- 话题：{' '.join('#' + h for h in d['hashtags'])}"]
    s = r["subtitle"]
    L += ["", "## 口播/字幕", f"- status：{s['status']}  source：{s.get('source')}" + ("（机器转写，需复核）" if s.get("needs_review") else "")]
    if s.get("platform_caption"):
        L.append(f"- 平台字幕：{s['platform_caption']['status']}（{s['platform_caption'].get('reason', '')}）")
    elif s.get("reason"):
        L.append(f"- 说明：{s['reason']}")
    L += [f"[{fmt_t(x['start'])}] {x['text']}" for x in s.get("segments") or []]
    c = r["comments"]
    L += ["", "## 评论", f"- status：{c['status']}  已采唯一：{c.get('collected_unique')}  平台 total：{c.get('platform_total')}  has_more：{c.get('has_more')}  stop_reason：{c.get('stop_reason')}",
          f"- 回复：{r['replies']['status']}" + (f"（展开 {r['replies'].get('parents_expanded')} 条一级，采 {r['replies'].get('collected_unique')} 条）" if r['replies'].get('parents_expanded') is not None else "")]
    L += [f"- ({x['like']}赞{(' · ' + x['ip']) if x.get('ip') else ''}) {x['text'][:120]}" for x in sorted([x for x in cm if not x["parent"]], key=lambda x: -(x["like"] or 0))[:15]]
    L += ["", "## 推荐词（按来源）"]
    for k, v in r["recommendations"].items():
        if k == "detail_suggest_words":
            L.append(f"- {k}：{v['status']}")
            L += [f"  - scene={sc}（{e.get('hint_text') or ''}）：{'、'.join(e['words']) or '空'}" for sc, e in v.get("scenes", {}).items()]
        elif k == "page_suggest_words":
            L.append(f"- {k}：{v['status']}")
            L += [f"  - scene={g.get('scene')}：{'、'.join(g['words']) or '空'}" for g in v.get("groups") or []]
        elif k == "related_videos":
            L.append(f"- {k}：{v['status']}（{len(v.get('items') or [])} 条视频）")
        else:
            L.append(f"- {k}：{v['status']}" + (f"：{'、'.join(v.get('words') or [])}" if v.get("words") else (f"（{v.get('reason') or v.get('hint') or ''}）")))
    b = r.get("bgm") or {}
    if b:
        L += ["", "## BGM", f"- 元数据：{b['status']}"]
        names = {"title": "曲名", "author": "作者", "music_id": "音乐 ID", "is_original_sound": "原声", "license_type": "类型",
                 "music_page_url": "音乐页", "play_url": "试听直链", "duration_s": "时长(s)"}
        for k, lb in names.items():
            f = (b.get("meta") or {}).get(k) or {}
            L.append(f"  - {lb}：{f.get('value') if f.get('value') not in (None, '') else '—'}（{f.get('status')}）")
        aa = b.get("audio_analysis") or {}
        L.append(f"- 音频分析：{aa.get('status')}" + (f"（{aa.get('reason') or aa.get('hint')}）" if aa.get("reason") or aa.get("hint") else ""))
        if aa.get("status") == "available":
            al, hk = aa["cut_beat_alignment"], aa["hook"]
            L += [f"  - BPM {aa['bpm']}，拍点 {aa['beats_count']} 个，强拍 {len(aa['downbeats'])} 个（启发式）",
                  f"  - 切镜 {al['cuts']} 次，±{al['tolerance_s']}s 内踩拍 {al['cuts_on_beat']} 次（{al['share_on_beat']}，随机基线 {al['chance_share']}；踩强拍 {al['share_on_downbeat']}）",
                  f"  - 开头 {hk['window_s']}s：拍点 {hk['beats']}；切镜 {hk['cuts']}；踩拍切镜 {hk['cuts_on_beat']}"]
        sy = b.get("style") or {}
        if sy.get("status") == "available":
            L.append(f"- 风格（{sy['note']}）：{sy['label']}，次选 {sy.get('runner_up')}；打分 {sy['scores']}")
    m = r["media"]
    L += ["", "## 媒体", f"- status：{m['status']}" + (f"  source：{m.get('source')}" if m.get("source") else "")]
    if m.get("tried"):
        L += [f"  - 失败源 {t['source']}：{t['reason']}" for t in m["tried"]]
    if isinstance(m.get("frames"), dict) and m["frames"].get("files"):
        L.append(f"- 截帧：{', '.join(m['frames']['files'])}（开头 {m['frames']['hook_frames']} 帧，全片 {m['frames']['uniform_frames']} 帧）")
    return "\n".join(str(x) for x in L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    v = sp.add_parser("video", help="采集单条视频")
    v.add_argument("target"); v.add_argument("-o", "--out", default="./dy_out", help="输出根目录（默认 ./dy_out）")
    v.add_argument("-n", type=int, default=200, help="一级评论上限（0=不取）")
    v.add_argument("--replies", action="store_true", help="展开高赞评论的二级回复"); v.add_argument("--reply-n", type=int, default=20)
    v.add_argument("--kw", help="同题关键词：取搜索框联想词 search_sug"); v.add_argument("--search-rel", action="store_true", help="另取 so.douyin 结果页“大家还在搜”（无头浏览器）")
    v.add_argument("--browse", action="store_true", help="无头浏览器打开视频页，取看后搜/页面级推荐词与相关推荐视频")
    v.add_argument("--media", action="store_true", help="下载视频并截帧"); v.add_argument("--asr", action="store_true", help="无平台字幕时本地 ASR（需 --media）")
    v.add_argument("--asr-model", default="small"); v.add_argument("--hook-sec", type=float, default=5.0); v.add_argument("--keep-video", action="store_true")
    v.add_argument("--no-bgm-audio", action="store_true", help="跳过 BGM 本地音频分析（BPM/拍点/切镜对齐/风格），只留音乐元数据")
    v.add_argument("--scene-thr", type=float, default=0.3, help="切镜检测阈值（ffmpeg scene，默认 0.3）")
    g = sp.add_parser("sug", help="只取搜索联想词"); g.add_argument("kw"); g.add_argument("--rel", action="store_true")
    a = ap.parse_args()
    if a.cmd == "video":
        collect(a)
    else:
        r = {"search_sug": search_sug(a.kw)}
        if a.rel:
            r["search_page_related"] = search_page_related(a.kw)
        print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
