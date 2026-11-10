// Offline browser rendering. Run with the installed Node Playwright and Chromium.
const path = require('path');
const fs = require('fs');
const http = require('http');
const {chromium} = require('playwright');

(async () => {
  const server=http.createServer((req,res)=>{
    if(req.url==='/'||req.url==='/dashboard.html'){
      res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});
      res.end(fs.readFileSync(path.join(__dirname,'dashboard.html')));
    }else{res.writeHead(404);res.end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin='http://127.0.0.1:'+server.address().port;
  let browser;
  try {
    browser = await chromium.launch({executablePath:'/usr/bin/chromium',headless:true});
    const errors = [], network = [];
    const context = await browser.newContext({viewport:{width:1600,height:1060},deviceScaleFactor:1,reducedMotion:'reduce',acceptDownloads:true});
    await context.route('**/*',route=>route.request().url().startsWith(origin+'/')?route.continue():route.abort());
    const page = await context.newPage();
    page.on('pageerror',e=>errors.push(String(e)));
    page.on('request',r=>{if(/^https?:/.test(r.url())&&!r.url().startsWith(origin+'/'))network.push(r.url());});
    await page.goto(origin+'/dashboard.html');
    await page.waitForTimeout(250);
    const check=(value,message)=>{if(!value)throw new Error(message);};
    const cards=()=>page.locator('#asset-grid .asset-card');
    check(await cards().count()===66,'Expected66 initial assets');
    await page.locator('#search').fill('600660');
    check(await cards().count()===1,'Code search should return one asset');
    check((await cards().innerText()).includes('福耀玻璃'),'Search returned wrong name');
    await cards().click();
    check(await page.locator('#drawer-title').innerText()==='福耀玻璃','Wrong detail title');
    check((await page.locator('#drawer-code').innerText()).includes('600660.SH'),'Wrong market suffix');
    await page.locator('#copy-symbol').click();
    await page.keyboard.press('Escape');
    await page.locator('[data-favorite="600660.SH"]').click();
    await page.reload();
    await page.locator('#favorite-filter').click();
    check(await cards().count()===1,'Favorite should persist after reload');
    await page.locator('[data-favorite="600660.SH"]').click();
    check(await cards().count()===0,'Removing last favorite should show empty state');
    await page.locator('#clear-filters').click();
    await page.locator('[data-type="etf"]').click();
    check(await cards().count()===11,'Expected11 ETFs');
    const downloadPromise=page.waitForEvent('download');
    await page.locator('#export-csv').click();
    const download=await downloadPromise;
    const csvPath=path.join(__dirname,'verified-export.csv');
    await download.saveAs(csvPath);
    const csv=fs.readFileSync(csvPath);
    check(csv.subarray(0,3).equals(Buffer.from([239,187,191])),'Export needs UTF8 BOM');
    check(csv.toString('utf8').split(/\r?\n/).filter(Boolean).length===12,'Export must contain header+11 ETFs');
    await page.locator('#clear-filters').click();
    await page.locator('[data-type="reference"]').click();
    check(await cards().count()===12,'Expected12 reference indices');
    await page.locator('#clear-filters').click();
    await page.locator('[data-camp="科技进攻"]').click();
    check(await cards().count()===22,'Expected22 technology assets');
    await page.locator('[data-type="stock"]').click();
    check(await cards().count()===13,'Expected13 technology stocks');
    await page.locator('#clear-filters').click();
    await page.locator('#sort').selectOption('code');
    check((await page.locator('.asset-code').first().innerText())==='000001','Code sort failed');
    await page.locator('#clear-filters').click();
    await page.locator('[data-view="relations"]').click();
    check(await page.locator('#asset-grid').isHidden(),'Board must hide in relationship view');
    check(await page.locator('#relationship-grid .theme-card').count()>0,'Relationship groups missing');
    await page.screenshot({path:path.join(__dirname,'dashboard-relations.png'),fullPage:true});
    await page.locator('#relationship-grid [data-open="300308.SZ"]').first().click();
    check(await page.locator('#drawer-title').innerText()==='中际旭创','Relationship detail jump failed');
    await page.screenshot({path:path.join(__dirname,'dashboard-detail.png'),fullPage:false});
    await page.keyboard.press('Escape');
    await page.locator('[data-view="board"]').click();
    await page.locator('#clear-filters').click();
    await page.reload();
    const overview = await page.evaluate(()=>({title:document.title,
      inputs:[...document.querySelectorAll('input')].map(e=>({id:e.id,placeholder:e.placeholder})),
      buttons:[...document.querySelectorAll('button')].slice(0,25).map(e=>({text:e.textContent.trim(),aria:e.getAttribute('aria-label'),id:e.id})),
      scrollWidth:document.documentElement.scrollWidth,width:window.innerWidth,
      assets:document.querySelectorAll('#asset-grid .asset-card').length}));
    if(overview.scrollWidth>overview.width)throw new Error('Desktop horizontal overflow');
    await page.screenshot({path:path.join(__dirname,'dashboard-preview.png'),fullPage:false});
    await page.screenshot({path:path.join(__dirname,'dashboard-full.png'),fullPage:true});
    const mobile = await browser.newContext({viewport:{width:390,height:844},deviceScaleFactor:1,isMobile:true,hasTouch:true,reducedMotion:'reduce'});
    await mobile.route('**/*',route=>route.request().url().startsWith(origin+'/')?route.continue():route.abort());
    const mp = await mobile.newPage();
    mp.on('pageerror',e=>errors.push(String(e)));
    await mp.goto(origin+'/dashboard.html');
    await mp.waitForTimeout(150);
    const sizes=await mp.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth}));
    if(sizes.width!==390||sizes.scrollWidth>390)throw new Error('Mobile viewport/overflow: '+JSON.stringify(sizes));
    await mp.screenshot({path:path.join(__dirname,'dashboard-mobile.png'),fullPage:true});
    if(errors.length)throw new Error('Browser errors: '+JSON.stringify(errors));
    if(network.length)throw new Error('Unexpected network request: '+JSON.stringify(network));
    const result={desktop:overview,mobile:sizes,pageErrors:errors,externalRequests:network,
      functionalChecks:['66 initial cards','code/name search','detail market suffix','copy action','favorite add/remove/persistence','empty state','11ETF filter','UTF8-BOM filtered CSV export','12reference filter','22tech filter','13tech stocks intersection','code sort','relationship view and detail jump','view visibility','mobile viewport and overflow']};
    fs.writeFileSync(path.join(__dirname,'render-validation.json'),JSON.stringify(result,null,2));
    process.stdout.write(JSON.stringify(result,null,2)+'\n');
  } finally { if(browser)await browser.close();await new Promise(resolve=>server.close(resolve)); }
})().catch(e=>{process.stderr.write(String(e)+'\n');process.exit(1);});
