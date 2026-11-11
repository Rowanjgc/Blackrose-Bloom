/* Render every captured reply in the real transcript at both widths. */
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
const {chromium}=require('playwright');

// Golden outputs for explicitly requested operations, independent of the model's
// chosen aggregate. Unspecified operations remain a source/manual review task.
const totals={'metrics-1':'40 pages','metrics-2':'18 ms','metrics-6':'3 items',
  'natural-metrics':'2 items','tiny-metrics':'3E-5 mg','count-distances-holdout':'4 items'};

(async()=>{
  const filename=path.resolve(process.argv[2]);
  const rows=JSON.parse(fs.readFileSync(filename));
  const root=path.resolve(__dirname,'../..');
  const output=path.join(path.dirname(filename),'usefulness-review');
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true});
  const report={page_errors:[],cases:[]};
  try {
    const page=await browser.newPage({viewport:{width:1280,height:1100}});
    await page.route(/^https?:/,route=>route.abort());
    await page.addInitScript(()=>{window.pywebview={api:{ready:async()=>({commands:[],keymap:[]}),save_layout:async()=>true}};});
    page.on('pageerror',e=>report.page_errors.push(e.message));
    await page.goto(pathToFileURL(path.join(root,'src/blackrose/gui/static/index.html')).href);
    for(const row of rows)for(const width of [1280,390]) {
      await page.setViewportSize({width,height:1100});
      await page.evaluate(row=>{
        BlackroseUI.unmountAll();
        bloom.receive([['transcript',{messages:[{role:'assistant',label:row.name,html:'',blocks:row.blocks||[]}]}],
          ['status',{state:'Ready',generating:false,model:'local audit',can_retry:false}]]);
        // Control replies are shown literally for activation review; do not
        // interpret captured model text as page HTML.
        if(!row.blocks?.length){
          const body=document.querySelector('.msg.assistant .body');
          if(body){body.textContent=row.content||row.error||'';body.style.whiteSpace='pre-wrap';}
        }
      },row);
      const expected=(row.blocks||[]).filter(b=>b.kind==='interactive_ui').length;
      await page.waitForFunction(expected=>document.querySelectorAll('.blackrose-ui').length===expected,expected);
      if(expected)await page.locator('.blackrose-ui h3,.blackrose-ui [role=status]').first().waitFor();
      const rendered=await page.locator('.msg.assistant').evaluate(el=>({
        text:el.innerText,
        // A source-validation error inside the fallback is a schema outcome,
        // not a renderer crash. Runtime alerts and observability errors differ.
        runtime_errors:el.querySelectorAll('[data-ui-error],.blackrose-ui > .ui-error,.blackrose-ui [role=alert]').length,
        source_fallbacks:[...el.querySelectorAll('.blackrose-ui[data-ui-status]')]
          .map(el=>el.dataset.uiStatus).filter(status=>!['ready','streaming'].includes(status)),
        tables:[...el.querySelectorAll('table')].map(t=>({client:t.clientWidth,scroll:t.scrollWidth})),
        bars:[...el.querySelectorAll('.ui-bar-track')].map(t=>({display:getComputedStyle(t).display,width:t.getBoundingClientRect().width})),
        buttons:[...el.querySelectorAll('button')].map(b=>({text:b.textContent,disabled:b.disabled})),
        aggregate_outputs:[...el.querySelectorAll('.ui-total output')].map(el=>el.textContent),
      }));
      // Give the scrollable transcript enough vertical room for an unobscured
      // full-reply capture. Preserve viewport width and all product CSS.
      const height=await page.locator('.msg.assistant').evaluate(el=>Math.ceil(el.getBoundingClientRect().height)+1000);
      await page.setViewportSize({width,height:Math.max(1100,height)});
      await page.locator('.msg.assistant').screenshot({path:path.join(output,`${row.name}-r${row.repeat+1}-${width}.png`)});
      const expectedTotal=totals[row.name];
      report.cases.push({name:row.name,repeat:row.repeat,width,...rendered,
        ...(expectedTotal ? {expected_total:expectedTotal,
          aggregate_matches:rendered.aggregate_outputs.length===1 && rendered.aggregate_outputs[0]===expectedTotal} : {})});
    }
    fs.writeFileSync(path.join(output,'render-check.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify({replies:rows.length,renders:report.cases.length,page_errors:report.page_errors,
      source_fallbacks:report.cases.filter(c=>c.source_fallbacks.length).map(c=>({name:c.name,repeat:c.repeat,width:c.width,statuses:c.source_fallbacks})),
      runtime_errors:report.cases.filter(c=>c.runtime_errors).map(c=>({name:c.name,width:c.width})),
      aggregate_mismatches:report.cases.filter(c=>c.aggregate_matches===false).map(c=>({name:c.name,width:c.width,actual:c.aggregate_outputs,expected:c.expected_total}))}));
    if(report.page_errors.length || report.cases.some(c=>c.runtime_errors || c.aggregate_matches===false))
      throw new Error('Rendered replay failed; inspect render-check.json');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
