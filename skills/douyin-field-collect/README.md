# douyin-field-collect

免登录的抖音「实地数据」采集 skill：给定关键词找同题视频；给定视频，取它的详情、平台字幕（没有就本地转写）、评论和回复、按来源区分的推荐搜索词、开头截帧、BGM 元数据与本地音频分析（BPM / 拍点 / 切镜卡点 / 粗略风格），输出带字段状态的 JSON 和 Markdown。

- 用法、思路和限制见 [SKILL.md](SKILL.md)
- 依赖见 `requirements.txt`（另需 ffmpeg 和 curl；BGM 音频分析需要 librosa）
- `--no-bgm-audio`：跳过 BGM 音频分析，只保留音乐元数据；`--scene-thr 0.3`：切镜检测阈值

## 目录

```
SKILL.md            skill 说明（思路 / 命令 / 限制）
requirements.txt
scripts/
  dy_search.py      关键词 → 同题视频列表（so.douyin.com + 神马/360）
  dy_collect.py     单条视频采集 → collect.json / comments.json / report.md
  media.py          多源下载、截帧拼图、faster-whisper 转写
  bgm.py            BGM 元数据归档、BPM/拍点/强拍、切镜与拍点对齐、规则风格分桶
  dy_common.py      公共请求、ID 解析、状态常量
```

## 输出示例（collect.json 结构）

```json
{
  "detail": {"status": "available", "likes": 0, "is_subtitled": 0, "platform_download": {"allowed": false, "reason": "aigc_forbidden"}},
  "subtitle": {"status": "available", "source": "local_asr", "needs_review": true,
               "platform_caption": {"status": "missing", "reason": "详情无字幕字段（is_subtitled=0）"}, "segments": []},
  "comments": {"status": "partial", "collected_unique": 100, "platform_total": 1152, "has_more": true, "stop_reason": "limit_reached"},
  "replies": {"status": "partial", "parents_expanded": 5, "collected_unique": 32},
  "recommendations": {
    "comment_top_rec": {"status": "available", "hint_text": "大家都在搜：", "words": []},
    "detail_suggest_words": {"status": "available", "scenes": {"feed_bottom_rec": {}, "detail_inbox_rex": {}}},
    "page_suggest_words": {"status": "available", "groups": []},
    "related_videos": {"status": "missing"},
    "search_sug": {"status": "available", "words": []},
    "search_page_related": {"status": "available", "words": []}
  },
  "media": {"status": "available", "source": "dlpanda", "tried": [{"source": "iesdouyin_share", "reason": "分享页无 item_list"}]},
  "bgm": {
    "status": "available",
    "meta": {"title": {"status": "available", "value": "moon"}, "author": {"status": "available", "value": "FLYSBR"},
             "music_id": {"status": "available", "value": "7478210321670408201"},
             "is_original_sound": {"status": "available", "value": false},
             "license_type": {"status": "available", "value": "licensed_library（抖音曲库/版权音乐）", "raw_flags": {"is_pgc": true}},
             "music_page_url": {"status": "available", "value": "https://www.douyin.com/music/7478210321670408201"},
             "play_url": {"status": "observed_empty", "value": null}, "duration_s": {"status": "available", "value": 144}},
    "audio_analysis": {"status": "available", "bpm": 123.0, "beats": [], "downbeats": [],
                       "shot_cuts": {"status": "available", "times": []},
                       "cut_beat_alignment": {"cuts": 77, "cuts_on_beat": 17, "share_on_beat": 0.221, "chance_share": 0.41, "share_on_downbeat": 0.039},
                       "hook": {"window_s": 5.0, "beats": [], "cuts": [2.5, 3.467], "cuts_on_beat": [2.5, 3.467]}},
    "style": {"status": "available", "label": "欢快节奏", "runner_up": "轻科技电子", "heuristic": true, "scores": {}, "features": {}}
  }
}
```

## 声明

本工具只访问未登录状态下公开可见的页面和接口，不保存任何账号凭据。请遵守平台规则，控制采集频率，仅用于研究和内容分析。
