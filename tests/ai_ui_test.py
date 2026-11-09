"""Browser tests (Playwright + Chromium) of the unified application: pages and /api on ONE origin served by lyravox_ai.devserver (the same handler
code that runs on Vercel).

REAL: browser, UI, audio decoding in the browser, chunking, the HTTP requests, retries/resume/cancel, project library, editor integration, the
motion engine's timing, and the genuine 'speech engine not installed' state of this sandbox.
MOCK: the speech recogniser — a successful transcription uses the test-only placeholder engine ("TEST LINE n" text built from the audio's silence
structure), because no Whisper model is available here. These tests prove plumbing and UI, not recognition quality.

Run from the repository root:  python3 build.py && python3 tests/ai_ui_test.py"""
import json, os, re, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = '/tmp/lv_ai_ui'; os.makedirs(TMP, exist_ok=True)
results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print(('  PASS ' if cond else '  FAIL ') + name + (f'  [{detail}]' if detail and not cond else ''))


def ff(args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y'] + args, check=True)


def tone_bursts(path, seconds, on, off, lead_silence=0):
    expr = f'sin(2*PI*330*t)*0.6*gt(t\\,{lead_silence})*lt(mod(t\\,{on + off})\\,{on})'
    ff(['-f', 'lavfi', '-i', f'aevalsrc={expr}:d={seconds}:s=16000', '-ac', '1', path])
    return path


def spawn(port, *args, env=None):
    e = dict(os.environ, LYRAVOX_ALLOW_FAKE='1', **(env or {}))
    p = subprocess.Popen([sys.executable, '-m', 'lyravox_ai.devserver', '--port', str(port), *args], cwd=ROOT, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try: urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=1); return p
        except Exception: time.sleep(0.1)
    raise RuntimeError('server did not start on ' + str(port))


def main():
    burst = tone_bursts(f'{TMP}/burst.wav', 12, 2.0, 1.0)
    burst_long = tone_bursts(f'{TMP}/burst_long.wav', 40, 2.0, 2.0)
    quiet_lead = tone_bursts(f'{TMP}/quiet_lead.wav', 70, 2.0, 2.0, lead_silence=36)       # first 36 s silent
    tone_bursts(f'{TMP}/long25.wav', 25, 2.0, 1.0)
    for fmt, enc in (('mp3', ['-c:a', 'libmp3lame', '-b:a', '64k']), ('ogg', ['-c:a', 'libvorbis']), ('m4a', ['-c:a', 'aac', '-b:a', '64k'])):
        ff(['-i', burst, *enc, f'{TMP}/burst.{fmt}'])
    with open(f'{TMP}/notaudio.mp3', 'wb') as f: f.write(os.urandom(50000))
    procs = []
    try:
        procs.append(spawn(8800, '--fake', '--fake-delay', '0.4'))                                   # main
        procs.append(spawn(8803, '--fake', '--fake-fail-first', '3'))                                # first 3 chunk requests fail
        procs.append(spawn(8804))                                                                    # REAL engine: not installed in this sandbox
        procs.append(spawn(8805, '--fake', env={'VERCEL': '1'}))                                     # behaves like the Vercel runtime (no Demucs)
        procs.append(spawn(8806, '--fake', env={'MAX_DURATION_S': '20'}))                            # tiny duration limit
        with sync_playwright() as pw:
            run(pw, burst, burst_long, quiet_lead)
    finally:
        for p in procs: p.terminate()
    failed = [r for r in results if not r[1]]
    print(f'\n{len(results) - len(failed)}/{len(results)} checks passed')
    sys.exit(1 if failed else 0)


FETCH_LOG = """()=>{window.__f=[];if(window.__wrapped)return;window.__wrapped=true;const o=window.fetch;window.fetch=function(u,init){const url=String(u&&u.url||u);
  if(url.includes('/api/transcribe')){const b=init&&init.body;window.__f.push({url:url,size:b&&b.size!=null?b.size:-1,type:init&&init.headers&&init.headers['Content-Type'],t:Date.now()});}
  return o.apply(this,arguments);};}"""


def new_page(browser, port=8800, lang='en', mobile=False):
    kw = dict(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True) if mobile else dict(viewport={'width': 1280, 'height': 860})
    ctx = browser.new_context(accept_downloads=True, **kw)
    ctx.add_init_script(f"localStorage.setItem('lyravox.tourDone','1');localStorage.setItem('lyravox.lang','{lang}');")
    pg = ctx.new_page(); pg.errors = []; pg.base = f'http://localhost:{port}'
    pg.on('pageerror', lambda e: pg.errors.append(str(e)))
    pg.on('console', lambda m: pg.errors.append(m.text) if m.type == 'error' and 'ERR_NAME_NOT_RESOLVED' not in m.text and 'Failed to load resource' not in m.text and 'net::ERR' not in m.text else None)
    return ctx, pg


def studio_with_song(browser, song, **kw):
    ctx, pg = new_page(browser, **kw)
    pg.goto(pg.base + '/studio/#audio', wait_until='domcontentloaded'); pg.wait_for_timeout(500)
    pg.set_input_files('#afile', song); pg.wait_for_timeout(800)
    return ctx, pg


def wait_result(pg, timeout=60000):
    pg.wait_for_selector('.lvai-table tbody tr', timeout=timeout)


def open_ai(pg):
    pg.click('#alist [data-act=ai]'); pg.wait_for_selector('.lvai-pill', timeout=10000)


def run(pw, burst, burst_long, quiet_lead):
    browser = pw.chromium.launch(args=['--no-sandbox', '--autoplay-policy=no-user-gesture-required'])

    print('\n[1] Same-origin AI dialog: no configuration, real chunked processing')
    ctx, pg = studio_with_song(browser, burst_long)
    pg.evaluate(FETCH_LOG)
    open_ai(pg)
    pg.wait_for_selector('.lvai-pill.ok', timeout=10000)
    txt = pg.inner_text('.lvai-box')
    check('never shows "No AI server is configured"; no server-address field exists', 'No AI server' not in txt and pg.locator('#lvaiApi').count() == 0 and 'server address' not in txt.lower())
    check('dialog is ready with nothing configured (health answered on the same origin)', 'AI server ready' in pg.inner_text('.lvai-pill') or 'ready' in pg.inner_text('.lvai-pill').lower(), pg.inner_text('.lvai-pill'))
    opts = pg.eval_on_selector_all('#lvaiLang option', 'els=>els.map(e=>e.value)')
    check('language selector: auto + bn + en + hi', opts == ['auto', 'bn', 'en', 'hi'], opts)
    check('file info: name, size, real duration', 'burst_long.wav' in pg.inner_text('.lvai-info') and '0:40' in pg.inner_text('.lvai-info'), pg.inner_text('.lvai-info'))
    check('vocal separation is shown but disabled, with the server-supplied reason', pg.is_disabled('#lvaiVocal') and 'Not available' in pg.inner_text('.lvai-chk') and 'demucs' in pg.inner_text('.lvai-chk').lower(), pg.inner_text('.lvai-chk'))
    pg.select_option('#lvaiLang', 'auto')
    stages = []; widths = []
    pg.click('#lvaiGo'); t0 = time.time()
    while time.time() - t0 < 60 and pg.locator('.lvai-table').count() == 0:
        try:
            stages.append(pg.inner_text('.lvai-stage', timeout=300)); widths.append(pg.evaluate("parseFloat(document.querySelector('.lvai-bar i').style.width)||0"))
        except Exception: pass
        pg.wait_for_timeout(120)
    uniq = []; [uniq.append(x) for x in stages if x not in uniq]
    check('real progress: decoding, then "part 1 of 2", "part 2 of 2", then building lines', any('Decoding' in x for x in uniq) and any('part 1 of 2' in x for x in uniq) and any('part 2 of 2' in x for x in uniq), uniq)
    check('progress bar only moves forward', widths == sorted(widths) and max(widths) > 40, widths[:12])
    wait_result(pg)
    f = pg.evaluate('window.__f')
    check('the 40 s song was sent as 2 same-origin requests (not one big upload)', len(f) == 2 and all(x['url'].startswith('/api/transcribe?') for x in f), [x['url'][:70] for x in f])
    check('every piece is a small audio/wav body, far under the 4.5 MB request limit', all(0 < x['size'] < 1_500_000 and x['type'] == 'audio/wav' for x in f), f)
    offs = [float(re.search(r'offset=([\d.]+)', x['url']).group(1)) for x in f]
    check('pieces are cut at a quiet moment near 30 s and carry their offset', offs[0] == 0 and 26 <= offs[1] <= 34, offs)
    rows = pg.locator('.lvai-table tbody tr[data-i]')
    check('result: all 10 sound stretches across both pieces, in timeline order', rows.count() == 10, rows.count())
    starts = pg.eval_on_selector_all('input.tm[data-key=start]', 'els=>els.map(e=>e.value)'); sec = [int(x.split(':')[0]) * 60 + float(x.split(':')[1]) for x in starts]
    check('line start times follow the real tone positions (every 4 s) across the chunk boundary', sec == sorted(sec) and all(abs(s - 4 * k) < 0.3 for k, s in enumerate(sec)), starts)
    check('the placeholder engine is labelled, never passed off as real lyrics', 'TEST ENGINE' in pg.inner_text('.lvai-notes'))
    check('auto-detect result shown (detected language chip)', 'Detected language' in pg.inner_text('.lvai-meta'))
    check('no page errors', not pg.errors, pg.errors[:3]); ctx.close()

    print('\n[2] Quiet parts are not sent')
    ctx, pg = studio_with_song(browser, quiet_lead)
    pg.evaluate(FETCH_LOG); open_ai(pg); pg.wait_for_selector('.lvai-pill.ok'); pg.click('#lvaiGo'); wait_result(pg, 90000)
    f = pg.evaluate('window.__f'); pieces = 3
    check('a silent piece is skipped locally (fewer requests than pieces)', 1 <= len(f) < pieces, len(f))
    check('lines start after the 36 s of silence', pg.evaluate("[...document.querySelectorAll('input.tm[data-key=start]')].every(e=>{const [m,s]=e.value.split(':');return +m*60+ +s>=35.5})"))
    ctx.close()

    print('\n[3] Lyrics editor operations')
    ctx, pg = studio_with_song(browser, burst)
    open_ai(pg); pg.wait_for_selector('.lvai-pill.ok'); pg.select_option('#lvaiLang', 'bn'); pg.click('#lvaiGo'); wait_result(pg)
    check('line text comes from the engine response', pg.input_value('tr[data-i="0"] input.tx') == 'TEST LINE 1')
    pg.fill('tr[data-i="0"] input.tx', 'আমার সোনার বাংলা'); pg.dispatch_event('tr[data-i="0"] input.tx', 'input')
    pg.fill('tr[data-i="1"] input.tm[data-key=start]', '0:03.50'); pg.dispatch_event('tr[data-i="1"] input.tm[data-key=start]', 'change')
    check('timestamp can be edited', pg.input_value('tr[data-i="1"] input.tm[data-key=start]') == '0:03.50')
    pg.click('tr[data-i="0"] .lvai-ib:has-text("⤵")')
    check('merge joins two lines', pg.locator('tbody tr[data-i]').count() == 3 and 'আমার সোনার বাংলা TEST LINE 2' in pg.input_value('tr[data-i="0"] input.tx'))
    pg.evaluate("(()=>{const i=document.querySelector('tr[data-i=\"0\"] input.tx');i.focus();i.setSelectionRange(16,16);i.dispatchEvent(new Event('keyup'))})()")
    pg.click('tr[data-i="0"] .lvai-ib:has-text("✂")')
    check('split cuts at the cursor without breaking Bangla clusters', pg.locator('tbody tr[data-i]').count() == 4 and pg.input_value('tr[data-i="0"] input.tx') == 'আমার সোনার বাংলা')
    pg.click('tr[data-i="3"] .lvai-ib:has-text("+")'); check('add line below', pg.locator('tbody tr[data-i]').count() == 5)
    pg.click('tr[data-i="4"] .lvai-ib.del'); check('delete line', pg.locator('tbody tr[data-i]').count() == 4)
    pg.click('#lvaiPlayAll'); pg.wait_for_timeout(1500); active = pg.locator('.lvai-table tr.on').count(); pg.click('#lvaiPlayAll')
    check('playback highlights the current line', active == 1, active)
    pg.fill('tr[data-i="3"] input.tx', 'a/b | c *d* !'); pg.dispatch_event('tr[data-i="3"] input.tx', 'input')

    print('\n[4] Send all to the editor (sanitised, timestamps kept, song attached)')
    pg.click('#lvaiSendAll'); pg.wait_for_url('**/editor/', timeout=20000); pg.wait_for_timeout(4500)
    lyr = pg.input_value('#lyrics')
    check('editor received timestamped lyrics', '[00:00.00]আমার সোনার বাংলা' in lyr, lyr[:100])
    check('special characters escaped', 'a b c d' in lyr and '*' not in lyr and '/' not in lyr and '|' not in lyr, lyr)
    check('song loaded in the editor', pg.evaluate("J.ui.audio&&J.ui.audio.name") == 'burst.wav')
    tags = [int(m[0]) * 60 + float(m[1]) for m in re.findall(r'^\[(\d+):(\d+\.\d+)\]', lyr, re.M)]
    firsts = {}
    for li, st in pg.evaluate("J.ui.plan.cuts.filter(c=>c.line>=0).map(c=>[c.line,+c.start.toFixed(2)])"): firsts.setdefault(li, st)
    got = [firsts[k] for k in sorted(firsts)]
    check('motion-engine cut starts equal the AI timestamps (within 0.02 s)', len(got) == len(tags) and all(abs(a - b) < 0.02 for a, b in zip(got, tags)), (tags, got))
    check('Bangla conjuncts survive into the engine', pg.evaluate("J.ui.plan.cuts.some(c=>J.unpackText(c.text).includes('বাংলা'))"))
    check('no page errors in the editor', not pg.errors, pg.errors[:3]); ctx.close()

    print('\n[5] Cancel stops sending; Regenerate sends only the selected section')
    ctx, pg = studio_with_song(browser, burst_long)
    pg.evaluate(FETCH_LOG); open_ai(pg); pg.wait_for_selector('.lvai-pill.ok'); pg.click('#lvaiGo')
    pg.wait_for_function("window.__f.length>=1", timeout=20000); pg.click('#lvaiCancel')
    pg.wait_for_selector('#lvaiGo', timeout=10000); n_at_cancel = len(pg.evaluate('window.__f')); pg.wait_for_timeout(4000)
    check('cancel returns to the setup view', pg.locator('#lvaiGo').count() == 1)
    check('nothing is sent after cancel (no 2nd piece started)', len(pg.evaluate('window.__f')) == n_at_cancel == 1, (n_at_cancel, len(pg.evaluate('window.__f'))))
    pg.click('#lvaiGo'); wait_result(pg, 90000)
    pg.evaluate("document.querySelectorAll('.lvai-box audio').forEach(a=>a.pause())"); pg.evaluate(FETCH_LOG)
    n0 = pg.locator('tbody tr[data-i]').count()
    pg.check('tr[data-i="2"] input[type=checkbox]'); pg.check('tr[data-i="3"] input[type=checkbox]')
    pg.fill('tr[data-i="0"] input.tx', 'KEEP ME'); pg.dispatch_event('tr[data-i="0"] input.tx', 'input')
    pg.fill('tr[data-i="2"] input.tx', 'OLD TEXT'); pg.dispatch_event('tr[data-i="2"] input.tx', 'input')
    pg.click('#lvaiRegen'); pg.wait_for_function("/regenerated/i.test(document.querySelector('.lvai-stage.small').textContent)", timeout=60000)
    f = pg.evaluate('window.__f'); off = float(re.search(r'offset=([\d.]+)', f[0]['url']).group(1))
    check('regenerate sent ONE small request starting at the selection (offset ~7.7 s)', len(f) == 1 and 7 < off < 8.5 and f[0]['size'] < 600_000, (len(f), off, f and f[0]['size']))
    check('lines outside the selection untouched; selected lines replaced', pg.input_value('tr[data-i="0"] input.tx') == 'KEEP ME' and 'OLD TEXT' not in ''.join(pg.eval_on_selector_all('input.tx', 'els=>els.map(e=>e.value)')) and pg.locator('tbody tr[data-i]').count() == n0)
    ctx.close()

    print('\n[6] Errors and recovery')
    ctx, pg = studio_with_song(browser, burst)
    pg.route('**/api/health', lambda r: r.abort())
    open_ai(pg); pg.wait_for_selector('.lvai-pill.bad', timeout=15000)
    check('service unreachable: clear message, retry button, Generate disabled', 'not reachable' in pg.inner_text('.lvai-pill') and pg.locator('#lvaiRetry').count() == 1 and pg.is_disabled('#lvaiGo'))
    pg.unroute('**/api/health'); pg.click('#lvaiRetry'); pg.wait_for_selector('.lvai-pill.ok', timeout=10000)
    check('retry button recovers once the service answers', not pg.is_disabled('#lvaiGo')); ctx.close()

    ctx, pg = studio_with_song(browser, burst_long, port=8803)
    pg.evaluate(FETCH_LOG); open_ai(pg); pg.wait_for_selector('.lvai-pill.ok'); pg.click('#lvaiGo')
    pg.wait_for_selector('#lvaiRetry', timeout=60000)
    msg = pg.inner_text('.lvai-err')
    check('engine failure (after automatic retries) is shown with the server message', 'simulated engine failure' in msg, msg)
    n_fail = len(pg.evaluate('window.__f'))
    check('3 automatic attempts were made before giving up', n_fail == 3, n_fail)
    pg.click('#lvaiRetry'); wait_result(pg, 90000)
    f = pg.evaluate('window.__f')
    check('"Retry" continues the same job and succeeds (4th request ok, 2 pieces done)', len(f) == 5 and pg.locator('tbody tr[data-i]').count() == 10, (len(f), pg.locator('tbody tr[data-i]').count()))
    ctx.close()

    ctx, pg = studio_with_song(browser, burst, port=8804)
    open_ai(pg); pg.wait_for_selector('.lvai-pill.bad', timeout=10000)
    check('REAL engine missing (this sandbox): honest message, Generate disabled, nothing faked', 'not available' in pg.inner_text('.lvai-pill') and 'not installed' in pg.inner_text('.lvai-err') and pg.is_disabled('#lvaiGo'), pg.inner_text('.lvai-err'))
    ctx.close()

    ctx, pg = studio_with_song(browser, burst, port=8805)
    open_ai(pg); pg.wait_for_selector('.lvai-pill.ok')
    check('Vercel runtime: vocal separation disabled with the Vercel explanation', pg.is_disabled('#lvaiVocal') and 'Vercel' in pg.inner_text('.lvai-chk'), pg.inner_text('.lvai-chk'))
    pg.click('#lvaiGo'); wait_result(pg)
    check('no claim that vocals were separated', 'Demucs ✓' not in pg.inner_text('.lvai-meta')); ctx.close()

    ctx, pg = studio_with_song(browser, f'{TMP}/burst_long.wav', port=8806)
    open_ai(pg); pg.wait_for_selector('.lvai-pill.ok'); pg.click('#lvaiGo'); pg.wait_for_selector('.lvai-err:not([hidden])', timeout=20000)
    check('song longer than the site limit is refused before anything is sent', 'longer than the limit' in pg.inner_text('.lvai-err'), pg.inner_text('.lvai-err')); ctx.close()

    ctx, pg = studio_with_song(browser, f'{TMP}/notaudio.mp3')
    open_ai(pg); pg.wait_for_selector('.lvai-pill.ok'); pg.click('#lvaiGo'); pg.wait_for_selector('.lvai-err:not([hidden])', timeout=20000)
    check('undecodable file: clear message', 'could not decode' in pg.inner_text('.lvai-err'), pg.inner_text('.lvai-err')); ctx.close()

    print('\n[7] Editor: button, generate, send all (undoable), single line')
    ctx, pg = new_page(browser)
    pg.goto(pg.base + '/editor/', wait_until='domcontentloaded'); pg.wait_for_timeout(3500)
    pg.click('#btnAIGen'); pg.wait_for_timeout(300)
    check('no song loaded -> no dialog', pg.locator('.lvai-overlay').count() == 0)
    pg.set_input_files('#audioFile', burst); pg.wait_for_timeout(4000); sample = pg.input_value('#lyrics')
    pg.click('#btnAIGen'); pg.wait_for_selector('.lvai-pill.ok', timeout=10000); pg.click('#lvaiGo'); wait_result(pg)
    pg.once('dialog', lambda d: d.accept()); pg.click('#lvaiSendAll'); pg.wait_for_timeout(1200)
    lyr = pg.input_value('#lyrics')
    check('editor lyrics replaced with the AI lines (timestamped)', lyr.startswith('[00:00.00]TEST LINE 1') and lyr != sample, lyr[:80])
    pg.keyboard.press('Control+z'); pg.wait_for_timeout(500)
    check('replacement is undoable (Ctrl+Z)', pg.input_value('#lyrics') == sample)
    pg.click('#btnAIGen'); pg.wait_for_selector('.lvai-pill.ok'); pg.click('#lvaiGo'); wait_result(pg)
    nb = pg.evaluate("J.ui.plan.lines.length"); pg.click('tr[data-i="1"] .lvai-ib.send'); pg.wait_for_timeout(900)
    check('single line imported with its timestamp and the editor re-planned', re.match(r'^\[\d\d:\d\d\.\d\d\]TEST LINE 2$', pg.input_value('#lyrics').splitlines()[-1]) is not None and pg.evaluate("J.ui.plan.lines.length") == nb + 1)
    pg.keyboard.press('Escape'); pg.wait_for_timeout(300); check('Esc closes the dialog', pg.locator('.lvai-overlay').count() == 0)
    time.sleep(3); check('project autosaved to the library with the AI lyrics', pg.evaluate("LVStore.projects.list().then(l=>l.some(r=>r.project.lyrics.includes('TEST LINE 2')))"))
    check('no page errors', not pg.errors, pg.errors[:3]); ctx.close()

    print('\n[8] Mobile (390 px) + Bangla UI')
    ctx, pg = studio_with_song(browser, burst, lang='bn', mobile=True)
    open_ai(pg); pg.wait_for_selector('.lvai-pill.ok', timeout=10000)
    check('Bangla UI in the dialog', 'AI দিয়ে লিরিক্স বানান' in pg.inner_text('.lvai-box header') and 'AI সার্ভার প্রস্তুত' in pg.inner_text('.lvai-pill'), pg.inner_text('.lvai-pill'))
    pg.click('#lvaiGo'); wait_result(pg)
    ow = pg.evaluate("[document.querySelector('.lvai-box').scrollWidth, document.querySelector('.lvai-box').clientWidth, document.documentElement.scrollWidth, innerWidth]")
    check('no horizontal overflow on a phone', ow[0] <= ow[1] + 1 and ow[2] <= ow[3] + 1, ow)
    check('tap targets >= 28 px', pg.evaluate("[...document.querySelectorAll('.lvai-ib')].every(b=>b.getBoundingClientRect().width>=28&&b.getBoundingClientRect().height>=28)"))
    pg.screenshot(path=f'{TMP}/m_ai_result.png'); ctx.close()

    print('\n[9] Browser audio pipeline: decode formats, cut planning, WAV the server accepts')
    ctx, pg = new_page(browser); pg.goto(pg.base + '/studio/', wait_until='domcontentloaded'); pg.wait_for_timeout(500)
    for fmt in ('wav', 'mp3', 'ogg', 'm4a'):
        data = open(f'{TMP}/burst.{fmt}', 'rb').read()
        r = pg.evaluate("""async([b64,name])=>{const bin=atob(b64),u=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);
          try{const pcm=await LVAI.audio.decode(new File([u],name));return {ok:true,sec:pcm.length/16000}}catch(e){return {ok:false,err:e.message}}}""", [__import__('base64').b64encode(data).decode(), f'x.{fmt}'])
        if fmt in ('wav', 'mp3', 'ogg'):
            check(f'browser decodes {fmt} to 16 kHz mono of the right length', r['ok'] and abs(r['sec'] - 12.0) < 0.3, r)
        else:
            print(f'  INFO m4a/AAC decode in this headless Chromium: {r}')
    r = pg.evaluate("""async()=>{ // 100 s with a quiet gap around 29-31 s and 59-61 s
      const n=100*16000,p=new Float32Array(n);for(let i=0;i<n;i++){const t=i/16000;const quiet=(t>29&&t<31)||(t>59&&t<61);p[i]=quiet?0:0.4*Math.sin(2*Math.PI*300*t)}
      const rms=LVAI.audio.rmsFrames(p),cuts=LVAI.audio.planCuts(rms,0,100,30,120);
      const blob=LVAI.audio.wav(p,0,30);const ab=new Uint8Array(await blob.arrayBuffer());
      const resp=await fetch('/api/transcribe?language=en',{method:'POST',body:blob,headers:{'Content-Type':'audio/wav'}});
      return {cuts,size:blob.size,hdr:String.fromCharCode(...ab.slice(0,4))+String.fromCharCode(...ab.slice(8,12)),status:resp.status}}""")
    cuts = r['cuts']
    check('cut planning: contiguous, <= max length, the first two cuts land in the quiet gaps', cuts[0][0] == 0 and cuts[-1][1] == 100 and all(abs(a[1] - b[0]) < 1e-9 for a, b in zip(cuts, cuts[1:])) and all(b - a <= 120 for a, b in cuts) and all((29 <= c[1] <= 31) or (59 <= c[1] <= 61) for c in cuts[:2]) and cuts[2][1] <= 90 + 0.01, cuts)   # no quiet moment near 90 s: that cut falls back to the target
    check('WAV produced by the browser has a valid header and the real API accepts it (HTTP 200)', r['hdr'] == 'RIFFWAVE' and r['status'] == 200 and r['size'] == 44 + 30 * 32000, r)
    ctx2, pg2 = new_page(browser)
    pg2.goto(pg2.base + '/studio/#audio', wait_until='domcontentloaded')
    pages = ['/', '/studio/', '/editor/', '/editor/bn/', '/about/', '/developer/', '/credits/', '/privacy/', '/terms/', '/contact/', '/sitemap.xml', '/robots.txt', '/manifest.webmanifest', '/favicon.svg']
    codes = {p: urllib.request.urlopen(f'http://localhost:8800{p}', timeout=10).status for p in pages}
    check('all static routes served from the same origin as the API', all(c == 200 for c in codes.values()), codes)
    try: urllib.request.urlopen('http://localhost:8800/does-not-exist/', timeout=5); nf = 200
    except urllib.error.HTTPError as e: nf = e.code
    check('unknown routes return 404', nf == 404)
    ctx.close(); ctx2.close(); browser.close()


if __name__ == '__main__':
    main()
