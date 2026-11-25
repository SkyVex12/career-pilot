// Production-build replay of desktop IPC delivery to DOM commit and the next paint opportunity.
// No model calls; the known complete sentence is useful for the fixture's question.
// Example: PLAYWRIGHT_MODULE=/path/to/playwright-core node scripts/answer-render-benchmark.mjs
//   --url http://127.0.0.1:1432 --browser /path/to/msedge --output /private/render.json
// Add --check-layout to assert stable reading positions and manual scrolling in each layout.
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { parseArgs } from 'node:util';
const { values } = parseArgs({ options: {
  url: { type: 'string', default: 'http://127.0.0.1:1432' },
  browser: { type: 'string' }, output: { type: 'string' },
  label: { type: 'string', default: 'current' },
  samples: { type: 'string', default: '40' },
  history: { type: 'string', default: '0,20,100' },
  'check-layout': { type: 'boolean', default: false },
} });
if (!values.output) throw new Error('--output is required; store replay artifacts outside historical evidence.');
const count = Number(values.samples);
const histories = values.history.split(',').map(Number);
if (!Number.isSafeInteger(count) || count < 1 || count > 1000 || histories.some(n => !Number.isSafeInteger(n) || n < 0 || n > 1000)) throw new Error('Invalid bounded sample/history count.');
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const browser = await chromium.launch({ executablePath: values.browser, headless: true });
const errors = [], trials = [], layoutChecks = [];
const percentile = (xs, p) => { const a = [...xs].sort((a,b) => a-b); return a[Math.ceil(p*a.length)-1] ?? null; };
async function newReplayPage(history, compact = true) {
  const page = await browser.newPage({ viewport: { width: 1000, height: 700 } });
  page.on('pageerror', e => errors.push(String(e)));
  await page.addInitScript(({ history, compact }) => {
    let next = 0, callback = 0;
    const callbacks = new Map(), listeners = new Map();
    const envelope = message => ({ n: next++, epoch: 1, event: { kind: 'server', message: { seq: next, ...message } } });
    const events = [{ n: next++, epoch: 1, event: { kind: 'connection', state: { state: 'live', session_id: 'render-fixture', resumed: false } } }, envelope({ type: 'session.ready', session_id: 'render-fixture', server_time: '2026-11-25T05:00:00Z', limits: { max_minutes: 60 }, spoken_language: 'en', language_switching: true, deep_auto_types: [] })];
    const historyText = 'I chose a shared cache for short-lived results because multiple workers need the same view. Expiration limits stale entries, while the database remains authoritative.\n\nI would check hit rate, latency, and memory use before changing the design. Cache misses fall back to the database, and I would cap that concurrency to protect it during a cold start.\n\nFor updates, the application invalidates affected keys after the database commit. If consistency requirements are stricter, I would avoid caching that data until a safe invalidation policy is established.';
    for (let i = 0; i < history; i++) events.push(
      envelope({ type: 'question.detected', question_id: `history-${i}`, text: `Earlier technical question ${i}`, question_type: 'technical', confidence: 1, speculative: false, answer_strategy: 'adaptive_on_demand' }),
      envelope({ type: 'answer.delta', question_id: `history-${i}`, pass: 'fast', sequence: 0, text: historyText }),
      envelope({ type: 'answer.done', question_id: `history-${i}`, pass: 'fast', delta_count: 1, memory_refs: [], usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0 } }),
    );
    window.__send = m => { const payload = envelope(m); for (const h of listeners.get('copilot://event') || []) callbacks.get(h)?.({ event: 'copilot://event', id: h, payload }); };
    window.__TAURI_INTERNALS__ = { metadata: { currentWindow: { label: 'overlay' }, currentWebview: { label: 'overlay' } }, transformCallback(fn) { callbacks.set(++callback, fn); return callback; }, unregisterCallback(id) { callbacks.delete(id); }, invoke: async (cmd,args) => {
      if (cmd === 'plugin:event|listen') { const list = listeners.get(args.event) || []; list.push(args.handler); listeners.set(args.event,list); return args.handler; }
      if (cmd === 'snapshot') return [events,true,next,1];
      if (cmd === 'overlay_prefs') return { opacity: 1, compact, click_through: false, on_top: true, hotkeys: {} };
      if (cmd === 'coding_prefs') return { mode: 'interview', language: '' };
      if (cmd === 'screenshot_tray') return [];
      if (cmd === 'live_status') return { running: true };
      if (cmd === 'api_call' && args.path === '/call-languages') return { items: [{ id: 'en', name: 'English' }] };
      return null;
    } };
    window.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
    localStorage.setItem('overlay.compact',compact ? '1' : '0');
  }, { history, compact });
  await page.goto(`${values.url}/#overlay`);
  await page.waitForFunction(({ history, compact }) => document.querySelectorAll('article[data-q]').length === history && document.querySelector('.ov')?.classList.contains('is-compact') === compact, { history, compact });
  return page;
}

try {
  for (const history of histories) {
    const page = await newReplayPage(history);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    // Five warm-ups exercise the same event path. They are retained but not in quantiles.
    for (let i = -5; i < count; i++) {
      const qid = `trial-${i}`;
      await page.evaluate(qid => {
        window.__send({ type: 'question.detected', question_id: qid, text: 'Why did you choose Redis?', question_type: 'technical', confidence: 1, speculative: false, answer_strategy: 'adaptive_on_demand' });
        window.__send({ type: 'answer.delta', question_id: qid, pass: 'fast', sequence: 0, text: 'I use Redis to share expiring cache entries' });
      }, qid);
      await page.waitForFunction(qid => document.querySelector(`[data-q="${qid}"]`)?.textContent.includes('I use Redis'), qid);
      const measured = await page.evaluate(qid => new Promise((resolve,reject) => {
        const sentence = 'I use Redis to share expiring cache entries across workers.';
        const card = document.querySelector(`[data-q="${qid}"]`);
        const timeout = setTimeout(() => { observer.disconnect(); reject(new Error('Missing sentence commit')); },5000);
        let start;
        const observer = new MutationObserver(() => {
          if (!card.textContent.includes(sentence)) return;
          const committed = performance.now(); observer.disconnect(); clearTimeout(timeout);
          requestAnimationFrame(() => requestAnimationFrame(() => resolve({ dom_ms: committed-start, paint_opportunity_ms: performance.now()-start })));
        });
        observer.observe(card,{subtree:true,childList:true,characterData:true});
        start = performance.now();
        window.__send({ type: 'answer.delta', question_id: qid, pass: 'fast', sequence: 1, text: ' across workers.' });
      }), qid);
      trials.push({ history, sample: i, warmup: i < 0, ...measured });
      await page.evaluate(qid => window.__send({ type: 'question.cancelled', question_id: qid, reason: 'superseded' }), qid);
      await page.waitForFunction(qid => !document.querySelector(`[data-q="${qid}"]`),qid);
    }
    await page.close();
  }

  if (values['check-layout']) for (const layout of ['split', 'compact', 'stacked']) {
    const page = await newReplayPage(0, layout === 'compact');
    await page.evaluate(layout => {
      localStorage.setItem('overlay.layout', layout === 'stacked' ? 'stacked' : 'split');
      window.dispatchEvent(new Event('copilot:preferences'));
      window.__send({ type: 'question.detected', question_id: 'layout', text: 'Design a notification system.', question_type: 'system_design', confidence: 1, speculative: false, answer_strategy: 'adaptive_on_demand' });
      window.__send({ type: 'answer.delta', question_id: 'layout', pass: 'fast', sequence: 0, text: 'I would accept notifications durably, then deliver them through separate queues for each channel.' });
    }, layout);
    await page.getByRole('group', { name: 'Answer', exact: true }).waitFor();
    await page.waitForSelector(layout === 'compact' ? '.ov.is-compact' : `.ov-${layout}`);
    const measured = await page.evaluate(async layout => {
      const frame = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const pane = () => document.querySelector(layout === 'stacked' ? '.ov-now-pinned' : '.ov-pane-answers .ov-pane-body');
      const position = () => ({ y: document.querySelector('[data-q="layout"] [role="group"] p').getBoundingClientRect().y, scroll_top: pane().scrollTop });
      await frame();
      const positions = [position()];
      for (let i = 1; i <= 12; i++) {
        window.__send({ type: 'answer.delta', question_id: 'layout', pass: 'fast', sequence: i, text: `\n\nStep ${i}: Separate delivery channels and retry transient failures with backoff. Record delivery status and check user preferences before sending.` });
        await frame(); positions.push(position());
      }
      pane().scrollTop = 120; await frame(); const before = position();
      window.__send({ type: 'answer.delta', question_id: 'layout', pass: 'fast', sequence: 13, text: '\n\nContinue processing with bounded concurrency.' });
      await frame(); const during = position();
      window.__send({ type: 'answer.done', question_id: 'layout', pass: 'fast', delta_count: 14, memory_refs: [], usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0 } });
      await frame(); const after = position();
      return { positions, manual_scroll: { before, during, after }, horizontal_overflow: document.documentElement.scrollWidth > innerWidth };
    }, layout);
    const ys = measured.positions.map(p => p.y);
    const shift = Math.max(...ys) - Math.min(...ys);
    const scroll = measured.manual_scroll;
    const passed = shift < 1 && scroll.before.scroll_top === scroll.during.scroll_top && scroll.before.scroll_top === scroll.after.scroll_top && !measured.horizontal_overflow;
    layoutChecks.push({ layout, passed, first_sentence_shift_px: shift, ...measured });
    await page.close();
  }

  const summary = histories.map(history => {
    const selected = trials.filter(t => t.history === history && !t.warmup);
    return { history, n: selected.length, dom_ms: Object.fromEntries([.5,.9,.95].map(p=>[`p${p*100}`,percentile(selected.map(t=>t.dom_ms),p)])), paint_opportunity_ms: Object.fromEntries([.5,.9,.95].map(p=>[`p${p*100}`,percentile(selected.map(t=>t.paint_opportunity_ms),p)])) };
  });
  const result = { label: values.label, measured_at: new Date().toISOString(), browser: await browser.version(), scope: 'Production headless desktop mock IPC callback to DOM mutation and second animation frame; not physical paint, provider latency, or end-to-end speech latency.', fixture: 'One known useful sentence; 0/20/100 prior fixed three-paragraph answers by default. 100 is a stress condition. Five warmups per condition.', errors, summary, trials, layout_checks: layoutChecks };
  mkdirSync(dirname(values.output),{recursive:true}); writeFileSync(values.output,JSON.stringify(result,null,2));
  console.log(JSON.stringify({ label: result.label, errors, summary, layout_checks: layoutChecks }));
  if (errors.length || layoutChecks.some(check => !check.passed)) process.exitCode = 1;
} finally { await browser.close(); }
