/* Actual loopback app integration. Notification transport is recorded in-page;
   this does not claim to test Windows notification delivery. No market fixture. */
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const python = process.env.CQ_TEST_PYTHON || '/workspace/czsc-tdx-bridge/.venv/bin/python';
const out = path.join(root, 'desktop-preview');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'cq-desktop-test-'));
const emptySource = path.join(temporary, 'empty-vipdoc');
fs.mkdirSync(emptySource);
fs.mkdirSync(out, { recursive: true });
const checks = [], pageErrors = [], externalRequests = [];
function check(name, condition = true) { assert.ok(condition, name); checks.push(name); }
const serverCode = `
from pathlib import Path
import sys
import desktop_alerts as app
from desktop_alerts import AlertHTTPServer, load_universe
from alerts.monitor import AlertStore, LocalMonitor
from alerts.online import OnlineNetworkError
universe=load_universe()
store=AlertStore(Path(sys.argv[1])/'alerts.sqlite3')
def synthetic_transport_failure(symbol,now=None):
    raise OnlineNetworkError('SYNTHETIC offline test: no market connection was made')
def synthetic_probe(symbol,now=None):
    return dict(symbol=symbol,asof=None,last_five_asof=None,bars30_count=0,
        status='insufficient_history',snapshot={},quality={'alertable':False},
        source={'provider':'synthetic_contract_fixture','label':'SYNTHETIC test fixture; not market data'},fresh=False)
app.check_online_market=synthetic_probe
monitor=LocalMonitor(universe,store,online_fetcher=synthetic_transport_failure)
server=AlertHTTPServer(('127.0.0.1',0),monitor,store,universe)
print(server.server_address[1],flush=True)
try: server.serve_forever(poll_interval=.1)
finally: monitor.close(); server.server_close()
`;
const server = spawn(python, ['-u', '-c', serverCode, temporary], { cwd: root, stdio: ['ignore','pipe','pipe'] });
let serverErrors = '';
server.stderr.on('data', data => serverErrors += data);
const ready = new Promise((resolve, reject) => {
  let buffer = '';
  server.stdout.on('data', data => { buffer += data; const line = buffer.split('\n').find(x => /^\d+$/.test(x)); if(line) resolve(Number(line)); });
  server.once('error', reject);
  server.once('exit', code => reject(new Error('server exited '+code+' '+serverErrors)));
});
function recordNotifications(permission='default') {
  const transport = function(title, options) { window.__notificationCalls.push({title,options}); };
  window.__notificationCalls = []; window.__permissionRequests = 0;
  transport.permission=permission;
  transport.requestPermission=async()=>{window.__permissionRequests++; transport.permission='granted'; return 'granted';};
  Object.defineProperty(window,'Notification',{configurable:true,value:transport});
}
function recordAudio() {
  const NativeAudio=window.AudioContext;
  window.__audioContexts=[]; window.__beeps=0;
  if(!NativeAudio)return;
  class RecordedAudio extends NativeAudio {
    constructor(...args){super(...args);window.__audioContexts.push(this);}
    createOscillator(...args){window.__beeps++;return super.createOscillator(...args);}
  }
  Object.defineProperty(window,'AudioContext',{configurable:true,value:RecordedAudio});
}
let browser;
(async()=>{
  const port=await ready, origin='http://127.0.0.1:'+port;
  browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
  const context=await browser.newContext({viewport:{width:1600,height:1060},deviceScaleFactor:1,acceptDownloads:true});
  await context.route('**/*',route=>{
    const url=route.request().url();
    if(!url.startsWith(origin+'/')) { externalRequests.push(url); return route.abort(); }
    if(url.endsWith('/favicon.ico')) return route.fulfill({status:204,body:''});
    return route.continue();
  });
  await context.addInitScript(recordNotifications);
  await context.addInitScript(recordAudio);
  const page=await context.newPage();
  page.on('pageerror',error=>pageErrors.push(String(error)));
  await page.goto(origin);
  await page.waitForFunction(()=>document.querySelectorAll('#stock-list .stock-row').length===43&&!document.querySelector('#start-monitor').disabled);
  check('43 stocks and four rules load from actual backend',await page.locator('#rules-list input:checked').count()===4);
  check('unconfigured data shows no invented prices',await page.locator('#latest-bar').innerText()==='—');
  check('all initial states translated',!(await page.locator('#stock-list').innerText()).includes('waiting'));
  check('notification permission is not requested at load',await page.evaluate(()=>window.__permissionRequests===0));
  await page.screenshot({path:path.join(out,'desktop-alerts.png'),fullPage:true});
  await page.locator('#stock-search').fill('福耀');
  check('search filters by Chinese stock name',await page.locator('#stock-list .stock-row').count()===1);
  await page.locator('[data-detail="600660.SH"]').click();
  await page.waitForFunction(()=>document.querySelector('#detail-overlay').classList.contains('open'));
  await page.waitForFunction(()=>Math.abs(document.querySelector('#detail-drawer').getBoundingClientRect().right-window.innerWidth)<1);
  check('stock detail has conditional research and no fake current data',(await page.locator('#detail-content').innerText()).includes('业务'));
  await page.screenshot({path:path.join(out,'stock-detail.png'),fullPage:false});
  await page.keyboard.press('Escape');
  await page.locator('#stock-search').fill('');
  await page.locator('#unselect-visible').click();
  await page.locator('#stock-search').fill('福耀');
  await page.locator('#select-visible').click();
  await page.locator('#stock-search').fill('');
  check('selection changes are retained across filters',await page.locator('[data-select]:checked').count()===1);
  await page.locator('#poll-interval').fill('10');
  await page.locator('#save-settings').click();
  await page.waitForFunction(()=>!document.querySelector('#error-banner').hidden);
  check('invalid scan interval gives readable error',(await page.locator('#error-text').innerText()).includes('15'));
  await page.locator('#poll-interval').fill('15');
  await page.locator('#source-dir').fill(emptySource);
  await page.locator('#save-settings').click();
  await page.waitForFunction(()=>document.querySelector('#save-state').textContent.includes('已保存'));
  await page.locator('#start-monitor').click();
  await page.waitForFunction(()=>document.querySelector('#run-state-text').textContent==='监控运行中');
  await page.locator('#refresh-now').click();
  await page.waitForFunction(()=>Array.from(document.querySelectorAll('.stock-row')).find(x=>x.dataset.symbol==='600660.SH')?.textContent.includes('文件未找到'));
  check('missing local file reports error without signals',await page.locator('[data-alert-id]').count()===0);
  await page.locator('#stop-monitor').click();
  await page.waitForFunction(()=>document.querySelector('#run-state-text').textContent==='监控已暂停');
  check('pause control stops monitor');
  await page.locator('#enable-notifications').click();
  check('permission request occurs only after user click',await page.evaluate(()=>window.__permissionRequests===1));
  await page.locator('#sound-enabled').check();
  await page.locator('#test-alert').click();
  await page.waitForFunction(()=>document.querySelectorAll('[data-alert-id]').length===1);
  await page.waitForFunction(()=>window.__notificationCalls.length===1);
  check('test reminder is explicitly marked non-real',(await page.locator('#alert-list').innerText()).includes('非真实信号'));
  check('notification transport invoked once for new test event',await page.evaluate(()=>window.__notificationCalls.length===1));
  await page.locator('#refresh-now').click();
  check('same event is not repeated',await page.evaluate(()=>window.__notificationCalls.length===1));
  const download=page.waitForEvent('download');
  await page.locator('#export-alerts').click();
  const downloaded=await download;
  const bytes=fs.readFileSync(await downloaded.path());
  check('CSV downloads with UTF-8 BOM and explicit test row',bytes.subarray(0,3).equals(Buffer.from([239,187,191]))&&bytes.toString('utf8').includes('TEST'));
  await page.reload();
  await page.waitForFunction(()=>document.querySelectorAll('[data-alert-id]').length===1);
  await page.locator('#refresh-now').click();
  check('reopening page displays history silently',await page.evaluate(()=>window.__notificationCalls.length===0));
  check('saved sound preference is restored',await page.locator('#sound-enabled').isChecked());
  await page.locator('#start-monitor').click();
  await page.waitForFunction(()=>window.__audioContexts.some(c=>c.state==='running'));
  check('a user gesture unlocks saved sound preference after reload');
  await page.locator('#stop-monitor').click();
  await page.locator('#enable-notifications').click();
  const peer=await context.newPage();
  peer.on('pageerror',error=>pageErrors.push(String(error)));
  await peer.goto(origin);
  await peer.waitForFunction(()=>document.querySelectorAll('[data-alert-id]').length===1);
  await peer.locator('#enable-notifications').click();
  await Promise.all([page.evaluate(()=>{window.__notificationCalls=[];window.__beeps=0;}),peer.evaluate(()=>{window.__notificationCalls=[];window.__beeps=0;})]);
  await page.locator('#test-alert').click();
  await Promise.all([page.locator('#refresh-now').click(),peer.locator('#refresh-now').click()]);
  await peer.waitForFunction(()=>document.querySelectorAll('[data-alert-id]').length===2);
  await page.waitForFunction(()=>{try{return JSON.parse(localStorage.getItem('cq-alert-delivery')).notice>=2;}catch{return false;}});
  const calls=await Promise.all([page.evaluate(()=>({notices:window.__notificationCalls.length,sound:window.__beeps})),peer.evaluate(()=>({notices:window.__notificationCalls.length,sound:window.__beeps}))]);
  check('two tabs deliver one notification and one sound per event',calls.reduce((sum,x)=>sum+x.notices,0)===1&&calls.reduce((sum,x)=>sum+x.sound,0)===1);
  await peer.close();
  await page.locator('#data-source').selectOption('eastmoney');
  check('online mode hides and disables LC5 directory inputs',!(await page.locator('#local-source-fields').isVisible())&&await page.locator('#source-dir').isDisabled());
  await page.screenshot({path:path.join(out,'desktop-online.png'),fullPage:true});
  await page.locator('#check-market').click();
  await page.waitForFunction(()=>document.querySelector('#market-check-result').textContent.includes('SYNTHETIC test fixture'));
  check('connection check displays provenance and creates no event',await page.locator('[data-alert-id]').count()===2&&(await page.locator('#market-check-result').innerText()).includes('不创建预警'));
  await page.locator('#save-settings').click();
  await page.locator('#start-monitor').click();
  await page.locator('#refresh-now').click();
  await page.waitForFunction(()=>Array.from(document.querySelectorAll('.stock-row')).find(x=>x.dataset.symbol==='600660.SH')?.textContent.includes('联网失败'));
  check('failed online source reports network error without fabricating signals',await page.locator('[data-alert-id]').count()===2&&await page.locator('#latest-bar').innerText()==='—');
  await page.locator('#stop-monitor').click();
  await page.reload();
  await page.waitForFunction(()=>document.querySelectorAll('#stock-list .stock-row').length===43);
  check('online source selection persists across reload',await page.locator('#data-source').inputValue()==='eastmoney');
  const mobile=await context.newPage();
  await mobile.setViewportSize({width:390,height:844});
  mobile.on('pageerror',error=>pageErrors.push(String(error)));
  await mobile.goto(origin);
  await mobile.waitForFunction(()=>document.querySelectorAll('#stock-list .stock-row').length===43);
  check('mobile layout has no page overflow',await mobile.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  await mobile.screenshot({path:path.join(out,'mobile-alerts.png'),fullPage:true});
  await mobile.close();
  page.once('dialog',dialog=>dialog.accept());
  await page.locator('#shutdown').click();
  await page.waitForFunction(()=>document.querySelector('#run-state-text').textContent==='程序已退出');
  check('exit button shuts down local application');
  check('no page JavaScript errors',pageErrors.length===0);
  check('no external browser requests',externalRequests.length===0);
  const result={checks:checks.length,passed:checks,pageErrors,externalRequests,notificationTransport:'recorded in-page; native Windows delivery not tested',marketData:'no live data; missing-file state, TEST reminder, and explicitly synthetic offline network fixtures'};
  fs.writeFileSync(path.join(out,'browser-validation.json'),JSON.stringify(result,null,2));
  console.log(JSON.stringify(result,null,2));
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  server.kill();
  fs.rmSync(temporary,{recursive:true,force:true});
});
