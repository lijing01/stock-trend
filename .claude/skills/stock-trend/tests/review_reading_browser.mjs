// Dependency-free headless Chrome acceptance check. Pass a fixture HTML path.
import {spawn} from 'node:child_process';
import {mkdtempSync, readFileSync, existsSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import assert from 'node:assert/strict';

const report = process.argv[2];
assert(report, 'Pass a local fixture HTML path');
const directory = mkdtempSync('/private/tmp/review-reading-chrome-');
const child = spawn(process.env.REVIEW_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--disable-background-networking', `--user-data-dir=${directory}`, '--remote-debugging-port=0', 'about:blank'
], {stdio: 'ignore'});
let socket;
const pause = () => new Promise(resolve => setTimeout(resolve, 100));
try {
  const portFile = directory + '/DevToolsActivePort';
  for (let i = 0; i < 300 && !existsSync(portFile); i++) await pause();
  assert(existsSync(portFile), 'headless Chrome did not start');
  const port = readFileSync(portFile, 'utf8').split('\n')[0];
  const page = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, {method: 'PUT'})).json();
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, {once: true});
    socket.addEventListener('error', reject, {once: true});
  });
  let id = 0;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const data = JSON.parse(event.data);
    if (pending.has(data.id)) {
      const {resolve, reject} = pending.get(data.id);
      pending.delete(data.id);
      data.error ? reject(new Error(JSON.stringify(data.error))) : resolve(data.result);
    }
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const call = ++id;
    pending.set(call, {resolve, reject});
    socket.send(JSON.stringify({id: call, method, params}));
  });
  const evaluate = async expression => {
    const result = await send('Runtime.evaluate', {expression, returnByValue: true});
    assert(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const navigate = async hash => {
    await send('Page.navigate', {url: pathToFileURL(report).href + hash});
    for (let i = 0; i < 50; i++) {
      if (await evaluate('document.readyState === "complete" && !!document.querySelector(".market-detail")')) return;
      await pause();
    }
    throw new Error('fixture did not load');
  };
  const results = [];
  const targets = ['index_trend', 'volume', 'breadth', 'zt_emotion', 'capital'];
  for (const width of [375, 768, 1440]) {
    await send('Emulation.setDeviceMetricsOverride', {width, height: 900, deviceScaleFactor: 1, mobile: false});
    await navigate('');
    assert.equal(await evaluate('document.querySelectorAll(".market-detail[open]").length'), 0);
    assert.equal(await evaluate('document.querySelectorAll("#market-component-summary a").length'), 5);
    for (const key of targets) {
      await evaluate(`document.querySelector('#market-component-summary a[href="#market-detail-${key}"]').click()`);
      await pause();
      assert(await evaluate(`document.getElementById('market-detail-${key}').open`));
      const top = await evaluate(`document.getElementById('market-detail-${key}').getBoundingClientRect().top`);
      assert(top >= -1 && top < 900, JSON.stringify({width, key, top}));
      await evaluate(`document.querySelector('#market-detail-${key} a[href="#market-component-summary"]').click()`);
      await pause();
      assert.equal(await evaluate('location.hash'), '#market-component-summary');
    }
    await evaluate('document.querySelectorAll("details").forEach(d => d.open = true)');
    const sizes = await evaluate('({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})');
    assert(sizes.scroll <= sizes.client + 1, JSON.stringify({width, ...sizes}));
    assert(await evaluate(`document.querySelectorAll('.market-detail-links a[href^="https:"]').length > 0`));
    if (width === 375) {
      for (const selector of ['.summary-table-wrap', '.market-explanation-table-wrap']) {
        assert(await evaluate(`document.querySelector('${selector}').scrollWidth > document.querySelector('${selector}').clientWidth`), selector);
      }
    }
    assert(await evaluate(`Array.from(document.querySelectorAll('.market-detail-links a[href^="https:"]')).every(a => a.target === '_blank' && a.rel.includes('noopener') && a.rel.includes('noreferrer'))`));
    await navigate('#market-detail-volume');
    await pause();
    assert(await evaluate('document.getElementById("market-detail-volume").open'));
    await evaluate('document.querySelector("#market-component-summary a").click()');
    await pause();
    await evaluate('document.getElementById("market-detail-volume").open=false; history.back()');
    await pause();
    assert(await evaluate('location.hash === "#market-detail-volume" && document.getElementById("market-detail-volume").open'));
    await evaluate('document.getElementById("market-detail-index_trend").open=false; history.forward()');
    await pause();
    assert(await evaluate('location.hash === "#market-detail-index_trend" && document.getElementById("market-detail-index_trend").open'));
    results.push({width, ...sizes, links: 5, directHash: true, history: true});
  }
  await send('Emulation.setScriptExecutionDisabled', {value: true});
  await navigate('');
  // CDP input remains available with page JavaScript disabled.
  const point = await evaluate(`(()=>{const s=document.querySelector('.market-detail summary');s.scrollIntoView();const r=s.getBoundingClientRect();return {x:r.x+10,y:r.y+10};})()`);
  await send('Input.dispatchMouseEvent', {type: 'mousePressed', ...point, button: 'left', clickCount: 1});
  await send('Input.dispatchMouseEvent', {type: 'mouseReleased', ...point, button: 'left', clickCount: 1});
  assert(await evaluate('document.querySelector(".market-detail").open'), 'native summary must work without JavaScript');
  console.log(JSON.stringify({report, results, noJavaScript: true}, null, 2));
} finally {
  socket?.close();
  child.kill();
}
