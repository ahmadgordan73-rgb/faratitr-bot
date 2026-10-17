#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
پادکست تصویری فراتیتر — تولید با Gemini TTS (برای اجرا روی GitHub Actions)
مراحل:
  1) متن هر بخش به جمله‌ها شکسته می‌شود؛ هر جمله با Gemini TTS خوانده می‌شود
  2) قطعات با مکث به هم می‌چسبند (ماژول wave — بدون هیچ کتابخانه اضافه)
  3) کارت PNG + صدا → MP4 → ویدیوی نهایی → ارسال ویدیو و صوت به کانال تلگرام

متغیرهای محیطی (از Secrets گیت‌هاب):
  GEMINI_API_KEY  کلید Gemini
  BOT_TOKEN       توکن ربات
  CHANNEL_ID      مثلا @FARATIE
اختیاری:
  VOICE=Kore            نام صدا (Zephyr/Puck/Charon/Kore/Fenrir/Leda/Orus/Aoede)
  TTS_MODEL=...         نام دقیق مدل TTS (اگر خالی، خودکار امتحان می‌کند)
  SKIP_SEND=1           ارسال به تلگرام انجام نشود (تست)
  FAKE_TTS=1            به‌جای Gemini سکوت بسازد (تست خط لوله بدون کلید)
"""
import os, sys, json, re, base64, wave, time, subprocess, urllib.request, urllib.error, urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
CARDS = os.path.join(BASE, 'cards')
SEG_JSON = os.path.join(BASE, 'segments.json')
OUT = os.path.join(BASE, 'out')
os.makedirs(OUT, exist_ok=True)

SR = 24000                       # نرخ نمونه‌گیری خروجی Gemini TTS
API = 'https://generativelanguage.googleapis.com/v1beta/models'
MODEL_CANDS = ['gemini-2.5-flash-preview-tts',
               'gemini-2.5-pro-preview-tts',
               'gemini-2.5-flash-tts']


def log(*a):
    print(*a, flush=True)


# ---------------- Gemini TTS ----------------
def http_json(url, payload, key, tries=4):
    body = json.dumps(payload).encode()
    last = ''
    for a in range(tries):
        req = urllib.request.Request(url, data=body, headers={
            'Content-Type': 'application/json', 'x-goog-api-key': key})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors='replace')[:300]
            last = f'HTTP {e.code}: {detail}'
            if e.code in (429, 500, 503):
                time.sleep(6 * (a + 1))
                continue
            raise RuntimeError(last)
        except Exception as e:
            last = str(e)
            time.sleep(6 * (a + 1))
    raise RuntimeError(f'TTS failed after retries: {last}')


def tts_pcm(text, voice, style, key, model):
    """یک جمله → بایت‌های PCM 16bit mono 24kHz"""
    if os.environ.get('FAKE_TTS'):
        return b'\x00\x00' * int(SR * min(4.0, 0.14 * len(text.split())))
    payload = {
        'contents': [{'parts': [{'text': style + text}]}],
        'generationConfig': {
            'responseModalities': ['AUDIO'],
            'speechConfig': {'voiceConfig': {
                'prebuiltVoiceConfig': {'voiceName': voice}}}
        }
    }
    d = http_json(f'{API}/{model}:generateContent', payload, key)
    try:
        part = d['candidates'][0]['content']['parts'][0]['inlineData']
        mime = part.get('mimeType', 'audio/L16;rate=24000')
        if 'rate=24000' not in mime:
            log(f'   [warn] mimeType: {mime}')
        return base64.b64decode(part['data'])
    except Exception:
        raise RuntimeError('bad TTS response: ' + json.dumps(d)[:300])


def probe_model(key, voice):
    """اولین مدل TTS که با این کلید جواب بدهد"""
    env_m = os.environ.get('TTS_MODEL')
    cands = [env_m] if env_m else MODEL_CANDS
    for m in cands:
        if not m:
            continue
        try:
            tts_pcm('سلام', voice, '', key, m)
            log(f'TTS model: {m}')
            return m
        except RuntimeError as e:
            log(f'  model {m} -> {str(e)[:120]}')
    raise RuntimeError('هیچ مدل TTS با این کلید در دسترس نبود')


# ---------------- شکستن متن به جمله‌ها ----------------
def split_chunks(text):
    parts = re.split(r'(?<=[.؟!])\s+', text.strip())
    out = []
    for p in parts:
        if len(p.split()) <= 16:
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
            if merged and len((merged[-1] + ' ' + pc).split()) <= 16:
                merged[-1] += ' ' + pc
            else:
                merged.append(pc)
        out.extend(merged)
    return [o.strip() for o in out if o.strip()]


# ---------------- ساخت صدای هر بخش ----------------
def write_wav(path, pcm):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)


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
    pcm = b''
    for i, ch in enumerate(chunks):
        p = tts_pcm(ch, voice, style, key, model)
        pcm += p + (pause if i < len(chunks) - 1 else b'')
        time.sleep(0.8)                      # احترام به سهمیه
    write_wav(out, pcm)
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


# ---------------- ارسال به تلگرام ----------------
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
    if not okv:  # fallback: به‌صورت فایل
        run([f'document=@{vid};type=video/mp4'],
            [('caption', cap_v)], 'sendVideo(doc)', 'sendDocument')
    time.sleep(3)
    run([f'audio=@{mp3};type=audio/mpeg'],
        [('title', cfg.get('audio_title', 'پادکست فراتیتر')),
         ('performer', 'Faratitr'),
         ('caption', cfg.get('caption_audio', 'نسخه صوتی — فراتیتر\n\n@Faratitrenews'))],
        'sendAudio', 'sendAudio')


# ---------------- main ----------------
def main():
    cfg = json.load(open(SEG_JSON, encoding='utf-8'))
    segs = cfg['segments']
    key = os.environ.get('GEMINI_API_KEY', '')
    fake = bool(os.environ.get('FAKE_TTS'))
    if not key and not fake:
        sys.exit('GEMINI_API_KEY تنظیم نشده است')

    model = 'fake' if fake else probe_model(key, cfg.get('voice', 'Kore'))

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
