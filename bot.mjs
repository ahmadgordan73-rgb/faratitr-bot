#!/usr/bin/env node
// ============================================================
//  فراتیتر بات — ربات خودکار خبری
//  Fetch foreign news (EN/HE/AR) -> AI translate/edit to Persian
//  -> post to Telegram channel in the exact Faratitr format
//  Engine: z-ai sandbox SDK (default) | Gemini REST (for GH Actions)
//  Node >= 18 required (built-in fetch). No external deps.
// ============================================================

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ---------- load config ----------
const CFG = JSON.parse(fs.readFileSync(path.join(__dirname, 'config.json'), 'utf8'));
// env overrides (used by GitHub Actions secrets)
if (process.env.BOT_TOKEN)  CFG.BOT_TOKEN  = process.env.BOT_TOKEN;
if (process.env.CHANNEL_ID) CFG.CHANNEL_ID = process.env.CHANNEL_ID;
const ENGINE = process.env.ENGINE || CFG.ENGINE || 'sandbox';
const HEADER_TEXT = CFG.HEADER_TEXT;   // سرصفحه ثابت
const FOOTER_TAG  = CFG.FOOTER_TAG;    // تگ پایانی

// ---------- logging ----------
function ts() { return new Date().toISOString().replace('T', ' ').slice(0, 19); }
function log(...args) { console.log(`[${ts()}]`, ...args); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ---------- state (anti-duplicate archive + hourly post log) ----------
const ARCHIVE_PATH = path.join(__dirname, CFG.ARCHIVE_FILE || 'archive.json');
let archive = { keys: {}, posts: [], titles: {} };
function loadArchive() {
  try {
    const d = JSON.parse(fs.readFileSync(ARCHIVE_PATH, 'utf8'));
    archive = { keys: d.keys || {}, posts: d.posts || [], titles: d.titles || {} };
  } catch { archive = { keys: {}, posts: [], titles: {} }; }
}
function pruneArchive() {
  const cutoff = Date.now() - 3 * 24 * 3600 * 1000; // keep 3 days
  for (const k of Object.keys(archive.keys)) if (archive.keys[k] < cutoff) delete archive.keys[k];
  const hourAgo = Date.now() - 3600 * 1000;
  archive.posts = archive.posts.filter(t => t > hourAgo);
  const halfDay = Date.now() - 12 * 3600 * 1000; // تیترهای فارسی پست‌شده — ۱۲ ساعت کافی است
  for (const k of Object.keys(archive.titles || {})) if (archive.titles[k] < halfDay) delete archive.titles[k];
}
function saveArchive() {
  try {
    const tmp = ARCHIVE_PATH + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify({ keys: archive.keys, posts: archive.posts, titles: archive.titles }, null, 1));
    fs.renameSync(tmp, ARCHIVE_PATH);
  } catch (e) { log('⚠️ archive save failed:', e.message); }
}
function normTitle(t) {
  return (t || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim().slice(0, 60);
}
function normLink(u) {
  try {
    const x = new URL(u);
    x.search = ''; x.hash = '';
    return (x.host.replace(/^www\./, '') + x.pathname).toLowerCase().replace(/\/+$/, '');
  } catch { return (u || '').slice(0, 120).toLowerCase(); }
}
// ددوپ هوشمند: خبرِ هم‌رویداد از دو منبع/زبان مختلف → تیترهای فارسیِ مشابه → رد
function titleTokens(t) {
  return new Set((t || '').split(' ').filter(w => w.length >= 3));
}
function isNearDupTitle(title) {
  const toks = titleTokens(normTitle(title));
  if (toks.size === 0) return false;
  for (const [prev] of Object.entries(archive.titles || {})) {
    const p = titleTokens(prev);
    let common = 0;
    for (const w of toks) if (p.has(w)) common++;
    if (common >= 3 && common / Math.min(toks.size, p.size) >= 0.7) return true;
  }
  return false;
}

// ---------- HTTP ----------
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36';
async function fetchText(url, timeoutMs = 20000) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: ac.signal, redirect: 'follow',
      headers: { 'User-Agent': UA, 'Accept': 'application/rss+xml, application/xml, text/xml, */*' },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.text();
  } finally { clearTimeout(t); }
}

// ---------- RSS / Atom parsing ----------
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', mdash: '—', ndash: '–', hellip: '…', rsquo: '\u2019', lsquo: '\u2018', ldquo: '\u201c', rdquo: '\u201d', laquo: '«', raquo: '»' };
function decodeEntities(s) {
  return s
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(parseInt(d, 10)))
    .replace(/&([a-z]+);/gi, (_, n) => ENTITIES[n.toLowerCase()] ?? `&${n};`);
}
function cleanText(s) {
  if (!s) return '';
  s = s.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1');
  s = s.replace(/<[^>]+>/g, ' ');
  s = decodeEntities(s);
  return s.replace(/\s+/g, ' ').trim();
}
function getField(itemXml, tags) {
  for (const tag of tags) {
    const re = new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)<\\/${tag}>`, 'i');
    const m = itemXml.match(re);
    if (m && m[1] && m[1].trim()) return m[1].trim();
  }
  return '';
}
function stripGoogleSuffix(title) {
  const parts = title.split(' - ');
  if (parts.length > 1 && parts[parts.length - 1].length <= 40) parts.pop();
  return parts.join(' - ').trim();
}
function parseFeed(xml, source) {
  const blocks = [...xml.matchAll(/<(item|entry)(?:\s[^>]*)?>([\s\S]*?)<\/\1>/g)];
  const items = [];
  for (const b of blocks) {
    const x = b[2];
    let title = cleanText(getField(x, ['title']));
    let link = cleanText(getField(x, ['link']));
    if (!link) { const lm = x.match(/<link[^>]*href="([^"]+)"/i); if (lm) link = lm[1].trim(); }
    const dateRaw = getField(x, ['pubDate', 'published', 'updated', 'dc:date']);
    let desc = cleanText(getField(x, ['description', 'summary', 'content:encoded', 'content']));
    if (!title || !link) continue;
    if (source.googleNews) title = stripGoogleSuffix(title);
    let dateMs = dateRaw ? Date.parse(dateRaw) : NaN;
    if (Number.isNaN(dateMs)) dateMs = null;
    if (desc.length > 3000) desc = desc.slice(0, 3000);
    const guid = cleanText(getField(x, ['guid', 'id'])) || link;
    items.push({ source: source.name, title, link, dateMs, desc, guid });
  }
  return items;
}

// ---------- filtering ----------
// کلمات کلیدی با حدِ ابتدای کلمه (\b) — تا «war» در «award» یا «toward» اشتباه گرفته نشود
function kwMatch(kw, hay) {
  const e = kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp('\\b' + e, 'i').test(hay);
}
function passesFilter(item) {
  const now = Date.now();
  if (!item.dateMs) return false; // 1-hour rule needs a date
  if (now - item.dateMs > CFG.WINDOW_SECONDS * 1000) return false;
  if (item.dateMs - now > 5 * 60 * 1000) return false; // future clock-skew
  // فیلتر فقط روی تیتر — توضیحات فید حاوی لینک/اسم منبع است و فیلتر را فریب می‌دهد
  const hay = (item.title || '').toLowerCase();
  if (CFG.KEYWORDS_INCLUDE?.length && !CFG.KEYWORDS_INCLUDE.some(k => kwMatch(k, hay))) return false;
  if (CFG.KEYWORDS_EXCLUDE?.length && CFG.KEYWORDS_EXCLUDE.some(k => kwMatch(k, hay))) return false;
  return true;
}

async function fetchAllFeeds() {
  const results = await Promise.allSettled(CFG.SOURCES.map(async (src) => {
    const xml = await fetchText(src.url, 22000);
    return { src, items: parseFeed(xml, src) };
  }));
  const all = [];
  for (const r of results) {
    if (r.status === 'fulfilled') {
      log(`  · ${r.value.src.name}: ${r.value.items.length} items`);
      all.push(...r.value.items);
    } else {
      log(`  · FEED FAILED: ${r.reason?.message || r.reason}`);
    }
  }
  return all;
}

// ---------- AI translation engine ----------
const SYSTEM_PROMPT = fs.readFileSync(path.join(__dirname, 'prompt.txt'), 'utf8');

async function loadSandboxSDK() {
  const candidates = [
    'z-ai-web-dev-sdk',
    'file:///usr/lib/node_modules/z-ai-web-dev-sdk/dist/index.js',
    'file:///home/z/.bun/install/cache/z-ai-web-dev-sdk@0.0.18@@@1/dist/index.js',
  ];
  for (const c of candidates) {
    try { const m = await import(c); if (m?.default?.create) return m.default; } catch { /* next */ }
  }
  throw new Error('z-ai-web-dev-sdk not found in this environment');
}
let _zai = null;
async function sandboxTranslate(inputText) {
  if (!_zai) _zai = await loadSandboxSDK();
  const zai = await _zai.create();
  const completion = await zai.chat.completions.create({
    messages: [
      { role: 'assistant', content: SYSTEM_PROMPT },
      { role: 'user', content: inputText },
    ],
    thinking: { type: 'disabled' },
  });
  return completion.choices?.[0]?.message?.content || '';
}
// زنجیره مدل‌ها: سهمیه رایگان 3.8-flash فقط 20/روز است → مدل‌های پرسهمیه اول
// (2.5-flash-lite تا ~1000/روز). روی 429 خودکار به بعدی سوییچ می‌شود.
const GEMINI_CHAIN = (process.env.GEMINI_CHAIN || CFG.GEMINI_CHAIN ||
  'gemini-2.5-flash-lite,gemini-3.8-flash-lite,gemini-2.5-flash,gemini-3.8-flash')
  .split(',').map(s => s.trim()).filter(Boolean);
// اگر GEMINI_MODEL صریح ست شده، اول همان امتحان می‌شود (سازگاری با قبلی)
const GEMINI_MODEL_ENV = process.env.GEMINI_MODEL || CFG.GEMINI_MODEL;
const CHAIN = GEMINI_MODEL_ENV ? [GEMINI_MODEL_ENV, ...GEMINI_CHAIN.filter(m => m !== GEMINI_MODEL_ENV)] : GEMINI_CHAIN;
let chainIdx = 0;   // چسبناک: مدلی که جواب داد در همین ران نگه داشته می‌شود

async function geminiCall(inputText, model) {
  const key = process.env.GEMINI_API_KEY || CFG.GEMINI_API_KEY;
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), 60000);
  try {
    const res = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`,
      {
        method: 'POST', signal: ac.signal,
        headers: { 'Content-Type': 'application/json', 'x-goog-api-key': key },
        body: JSON.stringify({
          systemInstruction: { parts: [{ text: SYSTEM_PROMPT }] },
          contents: [{ parts: [{ text: inputText }] }],
          generationConfig: { temperature: 0.3 },
          // اخبار جنگ/سیاست نباید فیلتر شوند — آستانه‌ها آزاد
          safetySettings: [
            { category: 'HARM_CATEGORY_HARASSMENT', threshold: 'BLOCK_NONE' },
            { category: 'HARM_CATEGORY_HATE_SPEECH', threshold: 'BLOCK_NONE' },
            { category: 'HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold: 'BLOCK_NONE' },
            { category: 'HARM_CATEGORY_DANGEROUS_CONTENT', threshold: 'BLOCK_NONE' },
          ],
        }),
      }
    );
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const e = new Error(`Gemini HTTP ${res.status}: ${data?.error?.message || ''}`);
      e.status = res.status;
      throw e;
    }
    const text = data?.candidates?.[0]?.content?.parts?.map(p => p.text || '').join('');
    if (!text) {
      const fr = data?.candidates?.[0]?.finishReason;
      throw new Error('Gemini empty response' + (fr ? ` (${fr})` : ''));
    }
    return text;
  } finally { clearTimeout(t); }
}

async function geminiTranslate(inputText) {
  let lastErr = null;
  for (let i = 0; i < CHAIN.length; i++) {
    const idx = (chainIdx + i) % CHAIN.length;
    const model = CHAIN[idx];
    try {
      const out = await geminiCall(inputText, model);
      chainIdx = idx;   // این مدل کار کرد → بقیه ران همان
      return out;
    } catch (e) {
      lastErr = e;
      const st = e.status || 0;
      if ([429, 404, 500, 503].includes(st)) {
        log(`  · ${model}: HTTP ${st} → fallback${st === 429 ? ' (quota)' : ''}`);
        continue;       // سهمیه/دردسترس‌نبودن → مدل بعدی
      }
      throw e;          // خطای دیگر (شبکه/ Abort): رفتار قبلی
    }
  }
  throw lastErr;
}
async function aiTranslate(inputText) {
  if (ENGINE === 'gemini') return geminiTranslate(inputText);
  return sandboxTranslate(inputText);
}

function extractJson(text) {
  if (!text) throw new Error('empty AI response');
  let s = text.replace(/```(?:json)?/gi, '').trim();
  const a = s.indexOf('{'), b = s.lastIndexOf('}');
  if (a === -1 || b === -1 || b <= a) throw new Error('no JSON in AI response');
  return JSON.parse(s.slice(a, b + 1));
}
// مدل گاهی کلیدها را اشتباه می‌نویسد (leide/leade) — انعطاف‌پذیر می‌گیریم
function pickField(obj, keys, minLen = 0) {
  const lower = {};
  for (const [k, v] of Object.entries(obj)) if (typeof v === 'string') lower[k.toLowerCase().trim()] = v;
  for (const k of keys) if (lower[k] && lower[k].trim().length >= minLen) return lower[k].trim();
  return '';
}
function truncateSentence(s, max) {
  if (s.length <= max) return s;
  const cut = s.slice(0, max);
  const m = cut.match(/^[\s\S]*[.؟!»]\s/);
  return (m ? m[0] : cut.replace(/\s+\S*$/, '')).trim();
}
function sanitizeAIPart(s) {
  return (s || '')
    .replace(/[*_`#]+/g, '')            // markdown leftovers
    .replace(/^\s*🔰\s*/, '')           // bot adds its own emoji
    .replace(/\s+@Faratitrenews\s*$/i, '')
    .trim();
}
// طبق قانون تحریریه: هیچ کلمه انگلیسی در متن فارسی (به جز مخفف‌های تمام‌حروف‌بزرگ مثل IDF)
function checkPersian(title, lede) {
  const words = (title + ' ' + lede).match(/[A-Za-z]+/g) || [];
  const bad = words.filter(w => !(w.length <= 2 || (w === w.toUpperCase() && w.length <= 6)));
  if (bad.length) throw new Error(`english words in output: ${bad.join(', ')}`);
}
async function translateItem(item) {
  let input = `SOURCE: ${item.source}\nTITLE: ${item.title}\nCONTENT: ${item.desc || item.title}`;
  let lastErr = null;
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      const out = extractJson(await aiTranslate(input));
      const title = sanitizeAIPart(pickField(out, ['title', 'تیتر', 'titr', 'headline'], 8));
      let lede = sanitizeAIPart(pickField(out, ['lede', 'leide', 'leade', 'lead', 'lid', 'لید'], 40));
      if (!lede) { // آخرین چاره: طولانی‌ترین مقدار متنی
        const vals = Object.values(out).filter(v => typeof v === 'string' && v.length > 50).sort((x, y) => y.length - x.length);
        if (vals[0]) lede = sanitizeAIPart(vals[0]);
      }
      if (title.length < 10 || title.length > 160) throw new Error(`bad title len ${title.length}`);
      lede = truncateSentence(lede, 700);
      if (lede.length < 50) throw new Error(`bad lede len ${lede.length}`);
      checkPersian(title, lede);
      // ددوپ تقریبی فارسی: خبر هم‌رویداد با تیترهای متفاوت
      const fz = 'f:' + normTitle(title).slice(0, 30);
      if (archive.keys[fz]) throw new Error('duplicate (near-identical Persian title) — skipped');
      if (isNearDupTitle(title)) throw new Error('duplicate (similar Persian title) — skipped');
      return { title, lede, fz };
    } catch (e) {
      lastErr = e;
      const msg = String(e.message || '');
      if (msg.includes('1301') || msg.includes('contentFilter')) {
        throw new Error('blocked by AI content filter (sensitive topic) — skipped permanently');
      }
      log(`  · translate attempt ${attempt} failed: ${msg}`);
      input += '\n\nهشدار: خروجی قبلی نامعتبر بود (کلمه انگلیسی، متن خالی یا قالب اشتباه). این بار فقط JSON معتبر با متن کاملاً فارسی بده.';
      await sleep(1200);
    }
  }
  throw lastErr;
}

// ---------- Faratitr post assembly ----------
function escHtml(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
function buildPost(item, tr) {
  const source = escHtml(item.source);
  return [
    HEADER_TEXT,
    '',
    `🔰 ${escHtml(tr.title)}`,
    '',
    `📰 <a href="${escHtml(item.link)}">${source}</a>`,
    '',
    `🔶 ${escHtml(tr.lede)}`,
    '',
    FOOTER_TAG,
  ].join('\n');
}

// ---------- Telegram ----------
async function tgApi(method, payload) {
  const url = `https://api.telegram.org/bot${CFG.BOT_TOKEN}/${method}`;
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), 30000);
  try {
    const res = await fetch(url, {
      method: 'POST', signal: ac.signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 429) {
      const wait = (data?.parameters?.retry_after || 5) + 1;
      const err = new Error(`TG 429, retry after ${wait}s`);
      err.retryAfter = wait; throw err;
    }
    if (!data.ok) throw new Error(`TG ${res.status}: ${data.description || 'unknown'}`);
    return data.result;
  } finally { clearTimeout(t); }
}
async function sendPost(text) {
  try {
    const r = await tgApi('sendMessage', {
      chat_id: CFG.CHANNEL_ID, text, parse_mode: 'HTML',
      link_preview_options: { is_disabled: true },
    });
    return r.message_id;
  } catch (e) {
    if (e.retryAfter) {
      log(`  · rate-limited, waiting ${e.retryAfter}s ...`);
      await sleep(e.retryAfter * 1000);
      const r = await tgApi('sendMessage', {
        chat_id: CFG.CHANNEL_ID, text, parse_mode: 'HTML',
        link_preview_options: { is_disabled: true },
      });
      return r.message_id;
    }
    throw e;
  }
}

// ---------- main cycle ----------
async function runCycle() {
  log('════ Cycle start ════');
  loadArchive(); pruneArchive();
  const raw = await fetchAllFeeds();

  const seen = new Set();
  let items = [];
  for (const it of raw) {
    if (!passesFilter(it)) continue;
    const k1 = normLink(it.link), k2 = normTitle(it.title);
    if (archive.keys[k1] || archive.keys['t:' + k2] || seen.has(k1) || seen.has('t:' + k2)) continue;
    seen.add(k1); seen.add('t:' + k2);
    items.push(it);
  }
  items.sort((a, b) => (b.dateMs || 0) - (a.dateMs || 0));
  const batch = items.slice(0, CFG.MAX_POSTS_PER_CYCLE);
  log(`Fetched ${raw.length} | fresh(in-window, unposted) ${items.length} | posting ${batch.length}`);

  let posted = 0;
  for (const item of batch) {
    if (archive.posts.length >= (CFG.MAX_POSTS_PER_HOUR || 8)) {
      log('⏸ سقف پست در ساعت پر شد — بقیه خبرها به چرخه‌های بعدی موکول شد');
      break;
    }
    try {
      const tr = await translateItem(item);
      const text = buildPost(item, tr);
      const mid = await sendPost(text);
      const k1 = normLink(item.link), k2 = normTitle(item.title);
      archive.keys[k1] = Date.now(); archive.keys['t:' + k2] = Date.now();
      if (tr.fz) archive.keys[tr.fz] = Date.now();
      const nt = normTitle(tr.title);
      if (nt) archive.titles[nt] = Date.now();
      archive.posts.push(Date.now());
      saveArchive();
      posted++;
      log(`✅ posted msg#${mid} [${item.source}] ${tr.title}`);
    } catch (e) {
      // اگر تکرار/فیلتر بود، لینک را آرشیو کن تا دوباره ترجمه نشود
      const msg = String(e.message || '');
      if (msg.includes('duplicate') || msg.includes('content filter')) {
        archive.keys[normLink(item.link)] = Date.now();
        saveArchive();
      }
      log(`⚠️ skipped [${item.source}]: ${e.message}`);
    }
    await sleep(1600); // gentle pacing
  }
  log(`════ Cycle done: ${posted} posted ════`);
  return posted;
}

// ---------- entry ----------
const arg = process.argv[2] || '';

if (arg === '--fetch-test') {
  loadArchive();
  fetchAllFeeds().then(items => {
    const now = Date.now();
    const inWin = items.filter(passesFilter);
    log(`TOTAL ${items.length} items | in 1h-window ${inWin.length}`);
    inWin.slice(0, 8).forEach(i =>
      log(`  [${Math.round((now - i.dateMs) / 60000)}m ago] [${i.source}] ${i.title.slice(0, 90)}`));
    process.exit(0);
  }).catch(e => { console.error(e); process.exit(1); });
} else if (arg === '--once') {
  runCycle().then(() => process.exit(0)).catch(e => { console.error(e); process.exit(1); });
} else {
  log('🚀 Faratitr bot starting — engine:', CFG.ENGINE, '| channel:', CFG.CHANNEL_ID, '| window:', CFG.WINDOW_SECONDS + 's', '| poll:', CFG.POLL_INTERVAL_SECONDS + 's');
  const loop = async () => {
    try { await runCycle(); }
    catch (e) { log('❌ cycle error:', e.message); }
  };
  loop(); // first cycle immediately
  setInterval(loop, CFG.POLL_INTERVAL_SECONDS * 1000);
  process.on('SIGTERM', () => { log('SIGTERM — exiting'); saveArchive(); process.exit(0); });
  process.on('SIGINT', () => { log('SIGINT — exiting'); saveArchive(); process.exit(0); });
}
