"""bgm.py — BGM 元数据归档 + 本地音频分析（纯脚本，不调大模型）。
1) music_meta(aweme)        从详情 music 字段归档：标题、作者、音乐 ID、原声/版权库标记、音乐页链接、试听直链
2) analyze(video, od, ...)  ffmpeg 抽音轨 → librosa 估 BPM / 拍点 / 强拍（启发式）→ ffmpeg 场景检测切镜 → 切镜与拍点对齐统计
3) style(features)          按本地特征（速度、能量 RMS、频谱质心、起音密度、人声占比估计）粗分风格，仅为粗略启发式
每个字段带 status，取值与全局一致：available（有）/ observed_empty（请求成功、平台真没有）/
missing（字段不存在或没取到）/ error / not_requested。
"""
import json, os, re, subprocess

SR = 22050
HALLU = re.compile(r"字幕\s*by|字幕制作|字幕由|请不吝点赞|订阅|谢谢观看|明镜与点点|Amara", re.I)
HEURISTIC_NOTE = "粗略启发式：基于本地音频特征的规则打分，不是模型识别，混了口播时偏差较大，只作参考"


def _f(status, value=None, path=None, **kw):
    d = {"status": status, "value": value}
    if path:
        d["field_path"] = path
    d.update(kw)
    return d


# ---------- 1) 元数据 ----------
def music_meta(a):
    """返回 (status, fields)。fields 每项 {status, value, field_path}。"""
    m = (a or {}).get("music")
    if not isinstance(m, dict) or not m:
        miss = _f("missing", reason="详情无 music 字段" if a else "详情不可用")
        return "missing", {k: dict(miss) for k in ("title", "author", "music_id", "is_original_sound", "license_type", "music_page_url", "play_url", "duration_s")}
    def txt(k):
        v = m.get(k)
        if v is None:
            return _f("missing", path=f"music.{k}")
        return _f("available" if str(v).strip() else "observed_empty", v, f"music.{k}")
    out = {"title": txt("title"), "author": txt("author")}
    if not out["author"]["value"] and m.get("owner_nickname"):
        out["author"] = _f("available", m["owner_nickname"], "music.owner_nickname")
    mid = m.get("id_str") or m.get("mid") or (str(m["id"]) if m.get("id") else None)
    out["music_id"] = _f("available", mid, "music.id_str") if mid else _f("missing", path="music.id_str")
    ios = m.get("is_original_sound")
    out["is_original_sound"] = _f("available", bool(ios), "music.is_original_sound") if ios is not None else _f("missing", path="music.is_original_sound")
    # 版权/曲库标记：is_pgc=曲库（版权）音乐；is_commerce_music=商用曲库；pgc_music_type 原样留档
    flags = {k: m.get(k) for k in ("is_original", "is_original_sound", "is_pgc", "is_commerce_music", "pgc_music_type", "is_matched_metadata") if k in m}
    if ios:
        lt = "original_sound（作者/他人上传的原声）"
    elif m.get("is_pgc") or m.get("is_commerce_music"):
        lt = "licensed_library（抖音曲库/版权音乐）" + ("，可商用" if m.get("is_commerce_music") else "")
    elif flags:
        lt = "unknown（非原声，但未标曲库）"
    else:
        lt = None
    out["license_type"] = _f("available", lt, "music.is_original_sound/is_pgc/is_commerce_music", raw_flags=flags) if lt else _f("missing", path="music.is_pgc")
    out["music_page_url"] = (_f("available", f"https://www.douyin.com/music/{mid}", "由 music.id_str 拼出", note="公开音乐页链接（拼接，未逐条打开验证）")
                             if mid else _f("missing", reason="无音乐 ID"))
    pu = m.get("play_url")
    urls = (pu.get("url_list") or []) if isinstance(pu, dict) else ([pu] if isinstance(pu, str) and pu else [])
    if urls:
        out["play_url"] = _f("available", urls[0], "music.play_url.url_list[0]", note="CDN 直链，可能过期")
    elif pu is not None:
        out["play_url"] = _f("observed_empty", None, "music.play_url.url_list", reason="字段存在但 url_list 为空（游客态常见，或原声未单独开放）")
    else:
        out["play_url"] = _f("missing", path="music.play_url")
    dur = m.get("duration") or m.get("shoot_duration")
    out["duration_s"] = _f("available", dur, "music.duration") if dur else _f("missing", path="music.duration")
    for k in ("album", "user_count"):
        if m.get(k) not in (None, ""):
            out[k] = _f("available", m[k], f"music.{k}")
    core = [out[k]["status"] for k in ("title", "author", "music_id")]
    return ("available" if all(s == "available" for s in core) else "partial"), out


# ---------- 2) 音频分析 ----------
def _audio(video):
    import numpy as np
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", video, "-vn", "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"], capture_output=True)
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def shot_cuts(video, thr=0.3):
    """ffmpeg 场景检测（缩到 160 宽再算，省资源）。返回切镜时间点（秒）。"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", video, "-an", "-vf", f"scale=160:-2,select='gt(scene,{thr})',metadata=print",
                        "-f", "null", "-"], capture_output=True, text=True)
    ts, out = sorted({round(float(x), 3) for x in re.findall(r"pts_time:([\d.]+)", r.stderr + r.stdout)}), []
    for t in ts:   # 0.2 秒内连续触发（转场/闪白）只算一次
        if not out or t - out[-1] >= 0.2:
            out.append(t)
    return out


def _downbeats(beats_t, onset_env, beat_frames):
    """强拍（启发式）：按 4/4 假设，选出 4 个相位里起音强度总和最大的一组。"""
    import numpy as np
    if len(beat_frames) < 8:
        return [], None
    st = onset_env[np.clip(beat_frames, 0, len(onset_env) - 1)]
    ph = int(np.argmax([st[p::4].sum() for p in range(4)]))
    return [round(float(t), 3) for t in beats_t[ph::4]], ph


def analyze(video, hook_sec=5.0, scene_thr=0.3, tol=0.1, speech_segments=None):
    """返回 bgm.audio_analysis 与 style 两块。无音轨/静音/依赖缺失时给出明确状态。"""
    try:
        import numpy as np, librosa
    except ImportError as e:
        err = {"status": "error", "reason": f"缺依赖 {e.name}（pip install librosa）"}
        return err, dict(err)
    if not video or not os.path.exists(video):
        s = {"status": "missing", "reason": "没有本地视频（下载失败或未加 --media）"}
        return s, dict(s)
    y = _audio(video)
    dur = len(y) / SR
    if dur < 1.0:
        s = {"status": "observed_empty", "reason": "视频无音轨"}
        return s, dict(s)
    rms = librosa.feature.rms(y=y)[0]
    rms_db = 20 * np.log10(np.maximum(rms, 1e-6))
    if float(np.percentile(rms_db, 95)) < -55:
        s = {"status": "observed_empty", "reason": "音轨近乎静音", "duration_s": round(dur, 2)}
        return s, dict(s)
    oenv = librosa.onset.onset_strength(y=y, sr=SR)
    tempo, bf = librosa.beat.beat_track(onset_envelope=oenv, sr=SR, units="frames")
    tempo = float(np.atleast_1d(tempo)[0])
    beats = librosa.frames_to_time(bf, sr=SR)
    downs, phase = _downbeats(beats, oenv, bf)
    onsets = librosa.onset.onset_detect(onset_envelope=oenv, sr=SR, units="time")
    # 切镜
    try:
        cuts = shot_cuts(video, scene_thr); cut_st = "available" if cuts else "observed_empty"
    except Exception as e:
        cuts, cut_st = [], "error"
    def near(t, arr):
        if len(arr) == 0:
            return None
        i = int(np.argmin(np.abs(arr - t))); return float(arr[i] - t)
    dists = [near(c, beats) for c in cuts]
    hits = [c for c, d in zip(cuts, dists) if d is not None and abs(d) <= tol]
    dhits = [c for c in cuts if downs and abs(near(c, np.array(downs))) <= tol]
    period = 60.0 / tempo if tempo > 0 else None
    chance = round(min(1.0, 2 * tol / period), 3) if period else None
    align = {"status": "available" if cuts and len(beats) else ("observed_empty" if cut_st == "observed_empty" else "missing"),
             "tolerance_s": tol, "cuts": len(cuts), "cuts_on_beat": len(hits),
             "share_on_beat": round(len(hits) / len(cuts), 3) if cuts else None,
             "share_on_downbeat": round(len(dhits) / len(cuts), 3) if cuts and downs else None,
             "chance_share": chance, "note": "chance_share=随机切镜也会落在 ±tol 内的比例（2·tol/拍长）；share_on_beat 明显高于它才说明在卡点",
             "median_abs_offset_s": round(float(np.median(np.abs([d for d in dists if d is not None]))), 3) if cuts and len(beats) else None}
    hook = {"window_s": hook_sec, "beats": [round(float(t), 3) for t in beats if t <= hook_sec],
            "downbeats": [t for t in downs if t <= hook_sec], "cuts": [c for c in cuts if c <= hook_sec],
            "cuts_on_beat": [c for c in hits if c <= hook_sec]}
    aa = {"status": "available", "method": "ffmpeg 抽音轨 22.05kHz 单声道 → librosa beat_track / onset；ffmpeg scene 检测切镜",
          "duration_s": round(dur, 2), "bpm": round(tempo, 1), "beats_count": int(len(beats)),
          "beats": [round(float(t), 3) for t in beats], "downbeats": downs,
          "downbeat_note": "强拍按 4/4 假设取起音最强的相位，启发式" if downs else "拍点太少，未估强拍",
          "shot_cuts": {"status": cut_st, "method": f"ffmpeg select=gt(scene,{scene_thr})", "times": cuts},
          "cut_beat_alignment": align, "hook": hook}
    # 3) 风格特征
    feats = features(y, rms_db, oenv, onsets, tempo, dur, speech_segments)
    return aa, style(feats)


def features(y, rms_db, oenv, onsets, tempo, dur, speech_segments=None):
    import numpy as np, librosa
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
    cent = librosa.feature.spectral_centroid(S=S, sr=SR)[0]
    H, P = librosa.decompose.hpss(S)
    freqs = librosa.fft_frequencies(sr=SR, n_fft=2048)
    band = (freqs >= 250) & (freqs <= 4000)
    he, pe = float((H ** 2).sum()), float((P ** 2).sum())
    # 人声占比估计：谐波成分里落在 250–4000Hz 的能量占比 × 谐波占总能量比；有字幕时再看口播覆盖率
    vocal_band = float((H[band] ** 2).sum()) / max(he, 1e-9)
    harm_ratio = he / max(he + pe, 1e-9)
    cover, cover_note = None, "没有字幕/转写可用"
    if speech_segments:
        segs = [s for s in speech_segments if not HALLU.search(s.get("text", ""))]
        secs = sum(max(0, s["end"] - s["start"]) for s in segs)
        chars = sum(len(re.sub(r"\W", "", s["text"])) for s in segs)
        if not segs:
            cover, cover_note = 0.0, "转写只有 whisper 常见幻觉句（如「字幕by…」），按无口播计"
        elif secs > 0 and chars / secs < 1.5:
            cover_note = f"转写字密度过低（{chars / secs:.2f} 字/秒，疑似歌词/幻觉），不计入"
        else:
            cover, cover_note = round(min(1.0, secs / max(dur, 1e-6)), 3), "来自字幕/转写时间轴"
    loud = rms_db[rms_db > np.percentile(rms_db, 10)]
    return {"tempo_bpm": round(float(tempo), 1), "rms_db_mean": round(float(np.mean(loud)), 1),
            "rms_db_range_p10_p90": round(float(np.percentile(rms_db, 90) - np.percentile(rms_db, 10)), 1),
            "spectral_centroid_hz": round(float(np.median(cent)), 0), "onset_density_per_s": round(len(onsets) / dur, 2),
            "harmonic_ratio": round(harm_ratio, 3), "vocal_band_ratio": round(vocal_band, 3),
            "speech_coverage": cover, "speech_coverage_note": cover_note}


def style(f):
    """规则打分 → 最高分桶。返回分数与理由，便于人工判断。"""
    t, e, rng, c, od = f["tempo_bpm"], f["rms_db_mean"], f["rms_db_range_p10_p90"], f["spectral_centroid_hz"], f["onset_density_per_s"]
    vb, hr, cov = f["vocal_band_ratio"], f["harmonic_ratio"], f.get("speech_coverage")
    sc, why = {}, {}
    def add(k, v, r):
        sc[k] = sc.get(k, 0) + v
        if v > 0:
            why.setdefault(k, []).append(r)
    add("人声为主", 2.0 * (cov is not None and cov > 0.6) + 1.0 * (vb > 0.45) + 0.5 * (hr > 0.7), f"口播覆盖 {cov}、人声频段占比 {vb}")
    add("安静铺底", 2.0 * (e < -30) + 1.0 * (od < 1.5) + 0.5 * (rng < 12), f"均值 {e}dB、起音 {od}/s")
    add("欢快节奏", 1.5 * (t >= 112) + 1.0 * (od >= 3) + 1.0 * (e > -22) + 0.5 * (1500 <= c <= 3200), f"{t}BPM、起音 {od}/s、{e}dB")
    add("轻科技电子", 1.5 * (c > 2600) + 1.0 * (95 <= t <= 135) + 1.0 * (hr < 0.6) + 0.5 * (rng < 14), f"质心 {c}Hz、谐波比 {hr}")
    add("悬疑紧张", 1.5 * (c < 1400) + 1.0 * (t < 95 or t > 140) + 1.0 * (rng > 18) + 0.5 * (od < 2.5), f"质心 {c}Hz、动态 {rng}dB")
    add("抑扬叙事", 1.5 * (rng > 15) + 1.0 * (70 <= t <= 110) + 1.0 * (hr > 0.6) + 0.5 * (1200 <= c <= 2600), f"动态 {rng}dB、{t}BPM")
    rank = sorted(sc.items(), key=lambda kv: -kv[1])
    top = rank[0][0] if rank[0][1] > 0 else None
    return {"status": "available" if top else "observed_empty", "label": top, "heuristic": True, "note": HEURISTIC_NOTE,
            "runner_up": rank[1][0] if len(rank) > 1 and rank[1][1] > 0 else None,
            "scores": {k: round(v, 2) for k, v in rank}, "reasons": {k: why.get(k, []) for k, _ in rank[:2]}, "features": f}
