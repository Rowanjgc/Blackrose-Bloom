/* Offline gallery of live outputs using the shipped renderer; no native bridge. */
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
const {chromium}=require('playwright');

(async()=>{
  const filename=path.resolve(process.argv[2]);
  const rows=JSON.parse(fs.readFileSync(filename,'utf8')).filter(r=>r.mode==='ui');
  const staticRoot=path.resolve(__dirname,'../../src/blackrose/gui/static');
  const output=path.join(path.dirname(filename),'review');fs.mkdirSync(output,{recursive:true});
  const json=JSON.stringify(rows).replace(/</g,'\\u003c');
  const html=`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
    <link rel="stylesheet" href="${pathToFileURL(path.join(staticRoot,'bloom.css')).href}">
    <link rel="stylesheet" href="${pathToFileURL(path.join(staticRoot,'ui.css')).href}">
    <style>body{display:block;overflow:auto;padding:32px;max-width:1000px;margin:auto}.trial{margin-block:48px;border-top:1px solid #444}h1{margin-top:32px}.task{color:#b3b3b3}</style>
    <title>Interactive UI model review</title></head><body><h1>Live model outputs · offline review</h1>
    <p>Fixture/task data only. Follow-up actions are disabled. Schema acceptance does not verify the facts or completeness.</p>
    <script src="${pathToFileURL(path.join(staticRoot,'ui-runtime.js')).href}"></script>
    <script>const rows=${json}; for(const row of rows){
      const section=document.createElement('section');section.className='trial';section.id=row.profile+'-'+row.index+'-'+row.repeat;
      const title=document.createElement('h2');title.textContent=row.profile+' / task '+(row.index+1)+' / repeat '+(row.repeat+1);section.appendChild(title);
      const task=document.createElement('p');task.className='task';task.textContent=row.task;section.appendChild(task);document.body.appendChild(section);
      for(const block of row.blocks||[]){if(block.kind!=='interactive_ui')continue;const container=document.createElement('div');section.appendChild(container);
        BlackroseUI.mount(container,{...block,actions_enabled:false},{state:async p=>({ok:true,state_sequence:p.state_sequence}),action:async()=>({ok:false,error:'Offline review'})});}
    }</script></body></html>`;
  const gallery=path.join(output,'index.html');fs.writeFileSync(gallery,html);
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1100,height:1050}}), errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(pathToFileURL(gallery).href);
    const expected=rows.flatMap(r=>r.blocks||[]).filter(b=>b.kind==='interactive_ui').length;
    await page.waitForFunction(expected=>document.querySelectorAll('.blackrose-ui').length===expected,expected);
    await page.waitForTimeout(200);
    const report=await page.evaluate(()=>({mounted:BlackroseUI.inspect().length,ready:document.querySelectorAll('[data-ui-status=ready]').length,
      fallback:document.querySelectorAll('[data-ui-status=invalid]').length,runtime_errors:document.querySelectorAll('[data-ui-error]').length}));
    for(const profile of ['comparison','form','steps','calculation','metrics']) {
      const target=page.locator(`#${profile}-0-0`);
      if(await target.count()){await target.scrollIntoViewIfNeeded();await target.screenshot({path:path.join(output,`${profile}.png`)});}
    }
    report.page_errors=errors;
    fs.writeFileSync(path.join(output,'render-check.json'),JSON.stringify(report,null,2)+'\n');
    console.log(JSON.stringify(report));
    if(errors.length||report.runtime_errors)process.exitCode=1;
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
