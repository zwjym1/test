---
name: douyin-field-collect
description: 需要免登录采集抖音同题视频的实地数据（详情、平台字幕或本地转写、评论与回复、按来源区分的推荐搜索词、开头截帧、BGM 元数据与本地卡点/风格分析）并输出带字段状态的 JSON/MD 时使用。
---

# 抖音实地数据采集（免登录）

## 思路

1. **找同题视频**：`dy_search.py` 用关键词搜索。so.douyin.com 是主力来源，用无头浏览器像手机一样正常浏览，只读取页面自己发出的请求。神马（m.sm.cn）和 360 用来补充。结果去重后按点赞排序。建议换 5–10 个说法分别搜，从中挑出角度不重复的 8–10 条。
2. **取详情和字幕**：`dy_collect.py video` 先调 so.douyin.com 的 `aweme/detail`。字幕优先用抖音自带的，按以下字段顺序查找：
   `interaction_stickers[].auto_video_caption_info.auto_captions[].url` → `video.cla_info.caption_infos[].url` → `video.subtitle_infos` / `subtitle_infos`。
   取到时标 `source=platform_caption`，这是原文。取不到且加了 `--media --asr` 时，才走本地 faster-whisper 转写，标 `source=local_asr, needs_review=true`，需要人工复核。
3. **评论分页**：用 `comment/list` 拿一级评论，`comment/list/reply` 展开高赞评论的回复。每项都会记录已采唯一数、平台 total、has_more、stop_reason（no_more / limit_reached / empty_page / cursor_stuck / blocked / api_error）。
4. **推荐词按来源分开记录，不混在一起**：

   | 键 | 来源 | 含义 |
   |---|---|---|
   | `comment_top_rec` | 详情 `suggest_words` 中 scene=comment_top_rec | 评论区顶部的「大家都在搜」 |
   | `detail_suggest_words` | 详情 `suggest_words` 的其余 scene（feed_bottom_rec=视频下方「相关搜索」、detail_inbox_rex、image_detail_related_search 等） | 按 scene 分开存放 |
   | `page_suggest_words` | `--browse`：游客打开视频页后，页面自己请求的 suggest_words 接口 | 看后搜/页面级推荐词；可能混入其他视频的词 |
   | `related_videos` | `--browse`：页面自己请求的 `/aweme/related/` | 相关推荐视频（是视频，不是词） |
   | `search_sug` | `--kw`：www.douyin.com `search/sug` | 搜索框联想词 |
   | `search_page_related` | `--kw --search-rel`：so.douyin.com 结果页 | 「大家还在搜/相关搜索」 |
5. **截帧和转写**：加 `--media` 后按以下顺序下载，某个源报 403 或失败就换下一个：iesdouyin 公开分享页 → dlpanda.com → snapany.com → yt-dlp。下载后截开头 0–5 秒的帧（每 0.5 秒一帧，生成 `hook_sheet.jpg`）和全片 16 张均匀帧（`contact_sheet.jpg`）。视频默认用完就删。
6. **BGM**：每条视频都会写一个 `bgm` 段，分三块：
   - `bgm.meta`：从详情 `music` 字段归档，**每项单独带 status**：`title` 曲名、`author` 作者、`music_id` 抖音音乐 ID、`is_original_sound` 是否原声、`license_type`（`original_sound` 原声 / `licensed_library` 抖音曲库版权音乐，可商用时会注明 / `unknown`；原始标记 `is_pgc`、`is_commerce_music`、`pgc_music_type` 存在 `raw_flags` 里）、`music_page_url`（按 ID 拼出的 `https://www.douyin.com/music/<id>`，没有逐条打开验证）、`play_url` 试听直链、`duration_s`。游客态的 `play_url.url_list` 通常为空，这时标 `observed_empty`。
   - `bgm.audio_analysis`（需要 `--media`）：用 ffmpeg 从下载的视频抽出 22.05kHz 单声道音轨，交给 librosa 估算 `bpm`、`beats` 拍点和 `downbeats` 强拍。强拍是按 4/4 拍假设、取起音最强的相位，属于启发式。切镜用 ffmpeg 场景检测（`select=gt(scene,0.3)`，可用 `--scene-thr` 调整），0.2 秒内连续触发的只算一次，结果放在 `shot_cuts.times`。`cut_beat_alignment` 给出切镜数、±0.1 秒内踩拍的次数和占比 `share_on_beat`、踩强拍占比、切镜到最近拍点的偏移中位数，以及随机基线 `chance_share`（= 2×0.1 秒 ÷ 拍长）。**`share_on_beat` 要明显高于 `chance_share`，才能说明这条视频在卡点。** `hook` 列出开头 `--hook-sec` 秒（默认 5 秒）内的拍点、强拍、切镜和踩拍切镜的时间戳。
   - `bgm.style`：**粗略启发式**（`heuristic=true`），按规则给特征打分，不是模型识别。用到的特征有速度、RMS 均值和动态范围、频谱质心、起音密度、谐波比、人声频段占比，以及口播覆盖率（来自字幕或转写的时间轴；whisper 的幻觉句如「字幕by…」、字密度过低的歌词不计入）。分桶为 轻科技电子 / 抑扬叙事 / 欢快节奏 / 悬疑紧张 / 人声为主 / 安静铺底，输出 `label`、`runner_up`、各桶 `scores` 和 `features`。混有口播时偏差较大，只作参考。
   - 退化处理：没下到视频时标 `missing`，视频无音轨或近乎静音时标 `observed_empty`，缺 librosa 时标 `error`，加了 `--no-bgm-audio` 时标 `not_requested`。不管哪种情况，`bgm.meta` 都照常输出。
7. **输出**：每条视频生成一个 `<out>/douyin_<id>/` 目录，包含 `collect.json`（所有字段及状态）、`comments.json`、`report.md`（可读摘要），加 `--media` 时还有截帧拼图。

### 字段状态（status）

`available` 完整 · `partial` 部分（到了上限或中途被拦） · `observed_empty` 请求成功但平台返回为空 · `missing` 没有这个字段，或页面没发出这个请求 · `blocked_login` 被登录墙挡住 · `blocked_verification` 碰到验证码、403 或 429 · `error` 程序或网络错误 · `not_requested` 没开这一项。
写结论时要把「没取到」和「平台真的没有」分开说：`available` = 有（ok）；`observed_empty` = 真没有；`missing` / `blocked_*` / `error` = 没取到。

## 命令

```bash
pip install -r requirements.txt && python -m playwright install chromium   # 一次性安装
cd scripts
# 1) 找同题视频
python dy_search.py "XX怎么做" -n 20 --src so,sm --json search.json
python dy_search.py "XX实测" --urls --days 30 > urls.txt
# 2) 单条采集：只调接口，最省资源
python dy_collect.py video <链接或ID> -o ./out -n 200
# 全量：回复、联想词、看后搜、截帧、无字幕时本地转写
python dy_collect.py video <链接或ID> -o ./out -n 200 --replies --reply-n 20 \
    --kw "同题关键词" --search-rel --browse --media --asr --asr-model small
# 要截帧，但不做 BGM 音频分析（BGM 元数据照常记录）
python dy_collect.py video <链接或ID> -o ./out --media --no-bgm-audio
# 3) 只取搜索联想词
python dy_collect.py sug "关键词" --rel
```

批量采集时用 `while read u; do python dy_collect.py video "$u" -o ./out; done < urls.txt`，每条之间留出间隔。

## 限制

- **全程免登录**：不带任何账号 cookie，不做签名逆向，也不换身份重试。遇到验证码或 403/429 就如实记录状态并停止该项。
- **搜索页有登录墙**：www.douyin.com 的搜索页游客打开会弹登录，所以只用 so.douyin.com、神马和 360，偶尔会漏掉最新发布的视频。
- **平台字幕不一定有**：很多视频的 `is_subtitled=0`，详情里也没有字幕字段，这时只能走本地转写。转写结果必须复核；歌曲或强 BGM 时 VAD 可能判为无人声，脚本会自动关掉 VAD 再跑一次。
- **下载可能被拦**：带 AI 生成标识或作者禁止下载的作品，官方下载会被拒（`detail.platform_download`）。这时靠解析站，403 就换源；全部失败则 `media.status=error`，并在 `tried` 中逐源列出原因。
- **游客态拿不到部分数据**：`--browse` 下 `/aweme/related/` 常常不会发出请求（`missing`）；页面级 suggest_words 可能混入别的视频的词，见 `attribution` 字段。
- **BGM 分析是本地脚本**：BGM 元数据直接取自详情，不额外发请求；音频分析在本机跑，不调大模型，也不联网。13 秒的视频约 3 秒，231 秒的约 20 秒（CPU）。BPM 可能估成真实值的一半或两倍；口播很重的视频，拍点和风格都会受人声干扰。
- **用量要克制**：同一 IP 短时间大量请求容易触发验证。默认每页间隔约 0.8 秒，批量时请自行再拉长间隔。
