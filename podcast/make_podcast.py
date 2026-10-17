#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
پادکست تصویری فراتیتر — تولید با Gemini TTS (برای اجرا روی GitHub Actions)
نسخه ۲:
  - کشف خودکار مدل‌های TTS از ListModels
  - probe با جمله خبری (نه سلام! که مدل جوابش را می‌دهد)
  - اگر مدل خواست «جواب» بدهد به‌جای «خواندن» → تلاش با پیشوند «این متن را دقیقا بخوان:»
  - نرخ نمونه از mimeType خوانده می‌شود و هر chunk با ffmpeg به 24kHz یکنواخت می‌رسد
"""
import os, sys, json, re, base64, wave, time, subprocess, urllib.request, urllib.error, urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
CARDS = os.path.join(BASE, 'cards')
SEG_JSON = os.path.join(BASE, 'segments.json')
OUT = os.path.join(BASE, 'out')
os.makedirs(OUT, exist_ok=True)

SR = 24000
API = 'https://generativelanguage.googleapis.com/v1beta'
# اولویت: مدل‌های نسل ۳ (سهمیه بهتر) → نسل ۲.۵
PREFERRED = ['gemini-3.8-flash-tts', 'gemini-3.1-flash-tts-preview',
             'gemini-2.5-flash-preview-tts', 'gemini-3.8-flash-lite-tts',
             'gemini-2.5-pro-preview-tts']
PROBE_TEXT = 'این یک پیام آزمایشی برای بررسی کیفیت صدای پادکست است.'
READER_PREFIX = 'این متن را دقیقا بخوان:\n'
CHUNK_SLEEP = 3.5          # احترام به سهمیه RPM
MAX_WORDS = 22             # جمله‌های بلندتر = درخواست کمتر

# مدل جاری و لیست failover (در runtime پر می‌شود)
MODEL_POOL = []
MODEL_IDX = 0


def log(*a):
    print(*a, flush=True)


def http_req(url, payload=None, key=None, tries=5):
    body = json.dumps(payload).encode() if payload is not None else None
    last = ''
    backoff = [15, 30, 45, 60, 60]
    for a in range(tries):
        req = urllib.request.Request(url, data=body, headers={
            'Content-Type': 'application/json', 'x-goog-api-key': key})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors='replace')[:400]
            last = f'HTTP {e.code}: {detail}'
            if e.code in (429, 500, 503) and a < tries - 1:
                time.sleep(backoff[a])
                continue
            raise RuntimeError(last)
        except Exception as e:
            last = str(e)
            if a < tries - 1:
                time.sleep(backoff[a])
    raise RuntimeError(f'failed after retries: {last}')


# ---------------- کشف مدل ----------------
def discover_tts_models(key):
    try:
        d = http_req(f'{API}/models?pageSize=1000', key=key)
        names = [m['name'].split('/')[-1] for m in d.get('models', [])
                 if 'tts' in m['name'].lower()
                 and 'generateContent' in m.get('supportedGenerationMethods', [])]
        log('TTS models seen:', names)
        return names
    except Exception as e:
        log('ListModels failed:', str(e)[:150])
        return []


# ---------------- TTS ----------------
def tts_once(text, voice, key, model):
    payload = {
        'contents': [{'parts': [{'text': text}]}],
        'generationConfig': {
            'responseModalities': ['AUDIO'],
            'speechConfig': {'voiceConfig': {
                'prebuiltVoiceConfig': {'voiceName': voice}}}
        }
    }
    return http_req(f'{API}/models/{model}:generateContent', payload, key=key)


def tts_pcm(text, voice, style, key, model):
    """خروجی: (pcm_bytes, rate) — با تلاش مجدد اگر مدل بخواهد جواب بدهد"""
    if os.environ.get('FAKE_TTS'):
        return b'\x00\x00' * int(SR * min(4.0, 0.14 * len(text.split()))), SR
    full = style + text
    last_err = ''
    for attempt, prefix in enumerate(('', READER_PREFIX)):
        try:
            d = tts_once(prefix + full, voice, key, model)
        except RuntimeError as e:
            last_err = str(e)
            if 'tried to generate text' in last_err and attempt == 0:
                continue
            raise
        try:
            part = d['candidates'][0]['content']['parts'][0]['inlineData']
            mime = part.get('mimeType', 'audio/L16;rate=24000')
            m = re.search(r'rate=(\d+)', mime)
            rate = int(m.group(1)) if m else SR
            return base64.b64decode(part['data']), rate
        except Exception:
            raise RuntimeError('bad TTS response: ' + json.dumps(d)[:300])
    raise RuntimeError(last_err or 'TTS failed')


def tts_failover(text, voice, style, key):
    """با مدل جاری تلاش؛ اگر سهمیه/خطا پایدار بود → مدل بعدی در استخر"""
    global MODEL_IDX
    last_err = ''
    for mi in range(MODEL_IDX, len(MODEL_POOL)):
        m = MODEL_POOL[mi]
        try:
            pcm, rate = tts_pcm(text, voice, style, key, m)
            MODEL_IDX = mi
            return pcm, rate
        except RuntimeError as e:
            last_err = str(e)
            log(f'   [failover] {m} -> {last_err[:90]}')
            if '429' in last_err or 'quota' in last_err.lower():
                continue
            raise
    raise RuntimeError(f'همه مدل‌ها جواب ندادند: {last_err[:150]}')


def probe_model(key, voice):
    env_m = os.environ.get('TTS_MODEL')
    discovered = discover_tts_models(key)
    ordered = [m for m in PREFERRED if m in discovered or env_m == m]
    ordered += [m for m in discovered if m not in ordered]
    if env_m and env_m not in ordered:
        ordered.insert(0, env_m)
    global MODEL_POOL, MODEL_IDX
    MODEL_POOL = ordered or PREFERRED
    MODEL_IDX = 0
    for m in MODEL_POOL:
        try:
            pcm, rate = tts_pcm(PROBE_TEXT, voice, '', key, m)
            log(f'TTS model OK: {m} ({rate}Hz, {len(pcm)} bytes)')
            MODEL_IDX = MODEL_POOL.index(m)
            return m
        except RuntimeError as e:
            log(f'  model {m} -> {str(e)[:110]}')
    raise RuntimeError('هیچ مدل TTS کار نکرد')


# ---------------- شکستن متن ----------------
def split_chunks(text):
    parts = re.split(r'(?<=[.؟!])\s+', text.strip())
    out = []
    for p in parts:
        if len(p.split()) <= MAX_WORDS:
            out.append(p)
            continue
        toks = [t for t in re.split(r'(،|؛)', p) if t and t.strip()]
        pieces, cur = [], ''
        for t in toks:
            if t in '،؛':
                cur += t
                pieces.append(cur.strip())
                cur = ''
            else:
                cur += (' ' if cur else '') + t
        if cur.strip():
            pieces.append(cur.strip())
        merged = []
        for pc in pieces:
            if merged and len((merged[-1] + ' ' + pc).split()) <= MAX_WORDS:
                merged[-1] += ' ' + pc
            else:
                merged.append(pc)
        out.extend(merged)
    return [o.strip() for o in out if o.strip()]


# ---------------- صدای هر بخش ----------------
def raw_to_wav(pcm, rate, path):
    """PCM خام → WAV یکنواخت 24kHz mono (اگر لازم، resample با ffmpeg)"""
    if rate == SR:
        with wave.open(path, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm)
        return
    raw = path + '.raw'
    open(raw, 'wb').write(pcm)
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 's16le', '-ar', str(rate),
                    '-ac', '1', '-i', raw, '-ar', str(SR), '-ac', '1', path],
                   check=True)
    os.remove(raw)


def build_seg_audio(cfg, seg, key, model):
    out = os.path.join(OUT, f"{seg['id']}.wav")
    if os.path.exists(out) and os.path.getsize(out) > 20000:
        log(f"   {seg['id']}: cached")
        return out
    voice = cfg.get('voice', 'Kore')
    style = cfg.get('style', '')
    pause = b'\x00\x00' * int(SR * cfg.get('chunk_pause', 0.35))
    chunks = split_chunks(seg['text'])
    log(f"   {seg['id']}: {len(chunks)} chunks")
    tmp_files = []
    for i, ch in enumerate(chunks):
        pcm, rate = tts_failover(ch, voice, style, key)
        t = os.path.join(OUT, f"{seg['id']}_c{i}.wav")
        raw_to_wav(pcm, rate, t)
        tmp_files.append(t)
        if i < len(chunks) - 1:
            time.sleep(CHUNK_SLEEP)
    # اتصال با wave
    with wave.open(out, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        for i, t in enumerate(tmp_files):
            with wave.open(t, 'rb') as r:
                w.writeframes(r.readframes(r.getnframes()))
            if i < len(tmp_files) - 1:
                w.writeframes(pause)
    for t in tmp_files:
        os.remove(t)
    return out


# ---------------- رندر ویدیو ----------------
def ffprobe_dur(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', path], capture_output=True, text=True)
    return float(r.stdout.strip())


def build_segment(seg, audio, i, n, gap):
    card = os.path.join(CARDS, f"{seg['id']}.png")
    out = os.path.join(OUT, f'seg_{i:02d}.mp4')
    if os.path.exists(out) and os.path.getsize(out) > 100_000:
        log(f"   seg {seg['id']}: cached")
        return out
    dur = ffprobe_dur(audio)
    total = dur + gap
    vf = '[0:v]format=yuv420p'
    af = '[1:a]aresample=44100,apad=pad_dur=0.6'
    if i == 0:
        vf += ',fade=t=in:st=0:d=0.7'
        af += ',afade=t=in:st=0:d=0.5'
    if i == n - 1:
        vf += f',fade=t=out:st={max(0, total-1.0):.2f}:d=1.0'
        af += f',afade=t=out:st={max(0, total-1.2):.2f}:d=1.2'
    vf += '[v]'
    af += '[a]'
    cmd = ['ffmpeg', '-y', '-v', 'error', '-loop', '1', '-framerate', '25',
           '-t', f'{total:.2f}', '-i', card, '-i', audio,
           '-filter_complex', vf + ';' + af,
           '-map', '[v]', '-map', '[a]', '-t', f'{total:.2f}', '-r', '25',
           '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
           '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k',
           '-ar', '44100', '-ac', '2', out]
    subprocess.run(cmd, check=True)
    log(f"   seg {seg['id']}: {total:.2f}s")
    return out


# ---------------- تلگرام ----------------
def tg_send(cfg, vid, mp3):
    token = os.environ['BOT_TOKEN']
    channel = os.environ.get('CHANNEL_ID', '@FARATIE')
    q = '?chat_id=' + urllib.parse.quote(channel)

    def run(files, extra, method, endpoint):
        cmd = ['curl', '-sS', '--max-time', '600']
        for f in files:
            cmd += ['-F', f]
        for k, v in extra:
            cmd += ['-F', f'{k}={v}']
        cmd += [f'https://api.telegram.org/bot{token}/{endpoint}{q}']
        r = subprocess.run(cmd, capture_output=True, text=True)
        try:
            d = json.loads(r.stdout)
            ok = d.get('ok')
            msg = (d.get('result') or {}).get('message_id')
            desc = str(d.get('description', ''))[:150]
            log(f'{method}: ok={ok} msg={msg} {desc}')
            return ok
        except Exception:
            log(f'{method} RAW: {r.stdout[:200]} {r.stderr[:200]}')
            return False

    cap_v = cfg.get('caption_video', 'پادکست تصویری فراتیتر\n\n@Faratitrenews')
    okv = run([f'video=@{vid};type=video/mp4', 'supports_streaming=true'],
              [('caption', cap_v)], 'sendVideo', 'sendVideo')
    if not okv:
        run([f'document=@{vid};type=video/mp4'],
            [('caption', cap_v)], 'sendVideo(doc)', 'sendDocument')
    time.sleep(3)
    run([f'audio=@{mp3};type=audio/mpeg'],
        [('title', cfg.get('audio_title', 'پادکست فراتیتر')),
         ('performer', 'Faratitr'),
         ('caption', cfg.get('caption_audio', 'نسخه صوتی — فراتیتر\n\n@Faratitrenews'))],
        'sendAudio', 'sendAudio')


def main():
    cfg = json.load(open(SEG_JSON, encoding='utf-8'))
    segs = cfg['segments']
    key = os.environ.get('GEMINI_API_KEY', '')
    fake = bool(os.environ.get('FAKE_TTS'))
    if not key and not fake:
        sys.exit('GEMINI_API_KEY تنظیم نشده است')

    model = 'fake' if fake else probe_model(key, cfg.get('voice', 'Kore'))
    if fake:
        MODEL_POOL.append('fake')

    log('1) TTS ...')
    audios = [build_seg_audio(cfg, s, key, model) for s in segs]

    log('2) render segments ...')
    gap = cfg.get('gap', 0.55)
    vids = [build_segment(s, a, i, len(segs), gap)
            for i, (s, a) in enumerate(zip(segs, audios))]

    log('3) concat ...')
    lst = os.path.join(OUT, 'list.txt')
    with open(lst, 'w') as fh:
        for v in vids:
            fh.write(f"file '{v}'\n")
    video_out = os.path.join(OUT, cfg.get('out_name', 'faratitr-podcast.mp4'))
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0',
                    '-i', lst, '-c', 'copy', '-movflags', '+faststart', video_out],
                   check=True)
    mp3_out = os.path.join(OUT, cfg.get('out_name', 'faratitr-podcast.mp4')
                           .replace('.mp4', '.mp3'))
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', video_out, '-vn',
                    '-c:a', 'libmp3lame', '-b:a', '192k', mp3_out], check=True)
    log(f'video: {video_out} ({os.path.getsize(video_out)//1024} KB, {ffprobe_dur(video_out):.0f}s)')
    log(f'audio: {mp3_out} ({os.path.getsize(mp3_out)//1024} KB)')

    if os.environ.get('SKIP_SEND'):
        log('SKIP_SEND=1 → ارسال انجام نشد')
        return
    log('4) send to telegram ...')
    tg_send(cfg, video_out, mp3_out)
    log('DONE')


if __name__ == '__main__':
    main()
