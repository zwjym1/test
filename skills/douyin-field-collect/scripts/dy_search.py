#!/usr/bin/env python3
"""dy_search.py — 免登录：关键词 → 去重后的同题抖音视频列表（按点赞降序）
来源（公开、未登录可见页面；不破解签名、不用 cookie）：
  so   抖音官方 H5 搜索 so.douyin.com（无头 Chrome 模拟手机正常浏览，只读页面自己发出的请求的响应）【主力】
  sm   神马搜索 m.sm.cn（"关键词 抖音"，时效性好，只有链接+标题）
  360  360 搜索 www.so.com（偏旧内容，补充）
用法：
  python dy_search.py "deepseek 做视频"                         # 默认 so+sm
  python dy_search.py "XX原理" -n 10 --src so,sm,360 --json out.json --days 30
  python dy_search.py "XX实测" --urls                           # 只输出链接，可接 dy_collect.py
"""
import argparse, asyncio, datetime as dt, html, json, re, sys, time, urllib.parse as up
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
      "Accept-Language": "zh-CN,zh;q=0.9"}
PAT = re.compile(r'douyin\.com(?:/|%2F|%252F)(?:share/)?(?:video|note)(?:/|%2F|%252F)(\d{19})', re.I)


def id_time(i):
    try:
        return int(i) >> 32
    except (TypeError, ValueError):
        return 0


def walk(o, out):
    if isinstance(o, dict):
        aid = o.get("aweme_id")
        if isinstance(aid, str) and re.fullmatch(r"\d{19}", aid) and ("desc" in o or "author" in o):
            a = o.get("author") or {}; s = o.get("statistics") or {}; v = o.get("video") or {}
            cur = out.setdefault(aid, {"id": aid})
            cur.setdefault("desc", (o.get("desc") or "").replace("\n", " ")[:80])
            if not cur.get("author"):
                cur["author"] = a.get("nickname", "")
            if s.get("digg_count") is not None:
                cur["likes"] = max(cur.get("likes", 0), s.get("digg_count") or 0)
            if s.get("comment_count") is not None:
                cur["comments"] = s.get("comment_count")
            if o.get("create_time"):
                cur["ts"] = o["create_time"]
            if v.get("duration"):
                cur["dur_s"] = round(v["duration"] / 1000)
        for x in o.values():
            walk(x, out)
    elif isinstance(o, list):
        for x in o:
            walk(x, out)
    elif isinstance(o, str) and len(o) > 50 and o[:1] in "[{":
        try:
            walk(json.loads(o), out)
        except ValueError:
            pass


async def src_so(kw, pages):
    from playwright.async_api import async_playwright
    found, state = {}, "available"
    async with async_playwright() as p:
        try:
            b = await p.chromium.launch(channel="chrome", headless=True)   # 系统已装 Chrome
        except Exception:
            b = await p.chromium.launch(headless=True)                     # 或 playwright install chromium
        ctx = await b.new_context(**p.devices["iPhone 13"], locale="zh-CN")
        pg = await ctx.new_page(); tasks = []

        async def grab(r):
            try:
                walk(json.loads(await r.text()), found)
            except Exception:
                pass
        pg.on("response", lambda r: tasks.append(asyncio.ensure_future(grab(r)))
              if "/search/" in r.url and r.request.resource_type in ("xhr", "fetch") else None)
        await pg.goto("https://so.douyin.com/s?keyword=" + up.quote(kw), wait_until="domcontentloaded", timeout=45000)
        await pg.wait_for_timeout(5000)
        for _ in range(pages):
            btn = pg.get_by_text("更多搜索结果")
            if await btn.count() == 0:
                break
            try:
                await btn.first.scroll_into_view_if_needed(); await btn.first.click(); await pg.wait_for_timeout(2500)
            except Exception:
                break
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        title = await pg.title()
        await b.close()
    if "验证" in title:
        state = "blocked_verification"
    for v in found.values():
        v["src"] = "so"
    return found, state


def src_html(kw, name):
    found = {}
    for p in (1, 2, 3):
        url = (f"https://m.sm.cn/s?q={up.quote(kw + ' 抖音')}&page={p}" if name == "sm"
               else f"https://www.so.com/s?q={up.quote(kw + ' 抖音')}&pn={p}")
        try:
            t = requests.get(url, headers=UA, timeout=20).text
        except requests.RequestException as e:
            print(f"[{name}] {type(e).__name__}", file=sys.stderr); continue
        for m in PAT.finditer(t):
            i = m.group(1)
            if i in found:
                continue
            raw = html.unescape(t[max(0, m.start() - 4000):m.end() + 1500])
            k = m.start() - max(0, m.start() - 4000)
            tt = list(re.finditer(r'"(?:article_title|title)"\s*:\s*"([^"]{4,120})"', raw))
            desc = min(tt, key=lambda x: abs(x.start() - k)).group(1) if tt else ""
            if not desc:
                mm = re.search(r">\s*([^<>]{6,})<", raw[k:k + 1500]); desc = mm.group(1).strip() if mm else ""
            found[i] = {"id": i, "desc": re.sub(r"<[^>]+>", "", desc)[:60], "src": name}
        time.sleep(1)
    return found, ("available" if found else "observed_empty")


def search(kw, srcs=("so", "sm"), pages=8):
    allv, states = {}, {}
    for s in srcs:
        try:
            got, st = asyncio.run(src_so(kw, pages)) if s == "so" else src_html(kw, s)
        except Exception as e:
            got, st = {}, "error"; print(f"[{s}] 失败：{type(e).__name__}", file=sys.stderr)
        states[s] = {"status": st if got or st != "available" else "observed_empty", "count": len(got)}
        for i, v in got.items():
            if i in allv:
                allv[i]["src"] += "+" + s
                for k, val in v.items():
                    allv[i].setdefault(k, val)
            else:
                allv[i] = v
    rows = list(allv.values())
    for r in rows:
        r["ts"] = r.get("ts") or id_time(r["id"])
        r["date"] = dt.datetime.fromtimestamp(r["ts"], dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
        r["url"] = f"https://www.douyin.com/video/{r['id']}"
    rows.sort(key=lambda r: (-(r.get("likes") or 0), -r["ts"]))
    return rows, states


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kw"); ap.add_argument("-n", type=int, default=30, help="最多输出条数")
    ap.add_argument("--src", default="so,sm", help="so,sm,360 任意组合")
    ap.add_argument("--pages", type=int, default=8, help="so 源点击“更多搜索结果”的次数")
    ap.add_argument("--days", type=int, help="只要近 N 天发布"); ap.add_argument("--json"); ap.add_argument("--urls", action="store_true")
    a = ap.parse_args()
    rows, states = search(a.kw, a.src.split(","), a.pages)
    if a.days:
        rows = [r for r in rows if r["ts"] >= time.time() - a.days * 86400]
    rows = rows[:a.n]
    print("[sources]", json.dumps(states, ensure_ascii=False), file=sys.stderr)
    if a.json:
        json.dump({"keyword": a.kw, "sources": states, "videos": rows}, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if a.urls:
        for r in rows:
            print(r["url"])
        return
    for r in rows:
        lk = r.get("likes"); lk = f"{lk/10000:.1f}w" if lk and lk >= 10000 else (lk if lk is not None else "-")
        print(f"{r['date']}  赞{lk!s:>6}  [{r['src']}] {r['url']}  {r.get('author','')}｜{r['desc'][:40]}")


if __name__ == "__main__":
    main()
