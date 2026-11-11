/* Task/geometry audit against the shipped page. Findings are recorded, not hidden
 * behind a single parser-success flag. No live services or native effects. */
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
const {chromium}=require('playwright');

const root=path.resolve(__dirname,'../..');
const output=path.resolve(process.argv[2]||path.join(root,'artifacts/interactive-ui-usefulness-browser'));
fs.mkdirSync(output,{recursive:true});
const artifact=JSON.parse(fs.readFileSync(path.join(root,'src/blackrose/gui/static/ui-contract.json')));
const clone=d=>JSON.parse(JSON.stringify(d));
const documents={
  comparison:clone(artifact.examples.comparison),
  form:clone(artifact.examples.form),
  steps:clone(artifact.examples.steps),
  calculation:clone(artifact.examples.calculation),
  metrics:clone(artifact.examples.metrics),
  'long-text':{type:'text',title:'Long identifiers',text:'identifier_'+'x'.repeat(500)},
  'long-metrics':{type:'metrics',title:'Long dataset labels',unit:'milliseconds_per_processed_request',aggregate:'sum',data:[{label:'service_'+'x'.repeat(150),value:123456789},{label:'reference',value:12}]},
  'tiny-metrics':{type:'metrics',title:'Micro measurements',unit:'mg',aggregate:'sum',data:[{label:'A',value:0.00001},{label:'B',value:0.00002},{label:'C',value:0}]},
  'signed-metrics':{type:'metrics',title:'Signed changes',unit:'items',aggregate:'sum',data:[{label:'Loss',value:-5},{label:'Zero',value:0},{label:'Gain',value:5}]},
  'tiny-calculation':{type:'calculation',title:'Tiny prices',unit:'USD',scale_label:'Quantity multiplier',items:[{label:'Micro item',quantity:1,unit_price:0.00001}]},
  'optional-select':{type:'form',title:'Optional preference',submit_label:'Continue',fields:[{id:'language',label:'Optional language',kind:'select',options:['English','Spanish']}]},
  'numeric-comparison':{type:'comparison',title:'Ranked options',columns:['Name','Score'],rows:[{Name:'Best',Score:100},{Name:'Middle',Score:50},{Name:'Basic',Score:10}],submit_label:'Choose option'},
  'large-comparison':{type:'comparison',title:'200 supplied options',columns:['Name','Value','Notes'],rows:Array.from({length:200},(_,i)=>({Name:`Option ${i+1}`,Value:i,Notes:'Supplied fixture'}))},
  'wide-comparison':{type:'comparison',title:'Eight columns',columns:Array.from({length:8},(_,i)=>`Column ${i+1}`),rows:Array.from({length:3},(_,i)=>Object.fromEntries(Array.from({length:8},(_,j)=>[`Column ${j+1}`,`Row ${i+1} value ${j+1}`])))},
};

(async()=>{
  const browser=await chromium.launch({headless:true});
  const report={fixtures:documents,checks:[],geometry:[],page_errors:[],network_requests:[]};
  const check=(name,pass,evidence)=>report.checks.push({name,pass,evidence});
  try {
    const page=await browser.newPage({viewport:{width:1280,height:1000}});
    await page.emulateMedia({reducedMotion:'reduce'});
    page.on('pageerror',e=>report.page_errors.push(e.message));
    await page.route(/^https?:/,route=>{report.network_requests.push(route.request().url());return route.abort();});
    await page.addInitScript(()=>{
      window.audit={states:[],actions:[]};
      window.pywebview={api:{ready:async()=>({commands:[],keymap:[]}),
        ui_state:async p=>{audit.states.push(p);return {ok:true,state_sequence:p.state_sequence};},
        ui_action:async p=>{audit.actions.push(p);return {ok:true};},save_layout:async()=>true}};
    });
    await page.goto(pathToFileURL(path.join(root,'src/blackrose/gui/static/index.html')).href);
    const block=d=>({block_id:'audit',message_id:2,conversation_id:1,position:0,kind:'interactive_ui',protocol_version:1,
      schema_hash:artifact.schema_hash,library_version:artifact.library_version,source_revision:1,source_sequence:1,
      source:JSON.stringify(d),document:d,status:'ready',fallback_text:d.title,view_id:'audit-view',actions_enabled:true});
    async function show(d){
      await page.evaluate(block=>{BlackroseUI.unmountAll();bloom.receive([
        ['transcript',{messages:[{role:'assistant',label:'Audit',blocks:[block]}]}],
        ['status',{state:'Ready',generating:false,model:'fixture',can_retry:false}]]);},block(d));
      await page.locator('.blackrose-ui h3').waitFor();
    }
    async function snap(name){
      await page.locator('.blackrose-ui').scrollIntoViewIfNeeded();
      await page.screenshot({path:path.join(output,name+'.png')});
    }
    fs.writeFileSync(path.join(output,'fixtures.json'),JSON.stringify(documents,null,2));
    for(const [name,d] of Object.entries(documents)) {
      for(const width of [320,390,520,768,1280])for(const font of [16,32]) {
        await page.setViewportSize({width,height:1000});
        await page.evaluate(font=>document.documentElement.style.fontSize=font+'px',font);
        await page.evaluate(()=>window.dispatchEvent(new Event('resize')));
        await page.waitForTimeout(100);
        await show(d);
        const geometry=await page.evaluate(()=>{
          const host=document.querySelector('.blackrose-ui'),box=host.getBoundingClientRect();
          const clipped=[...host.querySelectorAll('h3,h4,p,li > span:not(.ui-bar-track),output,label')]
            .filter(el=>el.getClientRects().length&&!el.closest('details:not([open])'))
            .map(el=>({tag:el.tagName,text:el.textContent.slice(0,80),left:el.getBoundingClientRect().left,right:el.getBoundingClientRect().right,client:el.clientWidth,scroll:el.scrollWidth}))
            .filter(el=>el.left<box.left-1||el.right>box.right+1||el.scroll>el.client+1);
          return {viewport:innerWidth,document:document.documentElement.scrollWidth,host_width:box.width,clipped,
            table_scroll:[...host.querySelectorAll('.ui-table-scroll')].map(el=>({client:el.clientWidth,scroll:el.scrollWidth,tabindex:el.getAttribute('tabindex')}))};
        });
        report.geometry.push({fixture:name,width,font,...geometry});
        if((width===390&&font===16)||(width===1280&&font===16)||geometry.clipped.length)await snap(`${name}-${width}-${font}`);
      }
    }
    await page.setViewportSize({width:1280,height:1000});
    await page.evaluate(()=>document.documentElement.style.fontSize='16px');
    await show(documents.calculation);
    await page.getByLabel('Quantity multiplier').fill('2');
    check('shared multiplier updates subtotal and combined total',await page.locator('.ui-total output').textContent()==='50 USD');
    await page.getByLabel('Quantity multiplier').fill('0');
    check('zero multiplier is preserved',await page.locator('.ui-total output').textContent()==='0 USD');
    await show(documents['tiny-calculation']);
    check('nonzero calculator price remains distinguishable from zero',!(await page.locator('.ui-total output').textContent()).startsWith('0 '),await page.locator('.ui-total').innerText());
    await show(documents['tiny-metrics']);
    check('nonzero metric values remain distinguishable from zero',!(await page.locator('.ui-total output').textContent()).startsWith('0 '),await page.locator('.ui-bars').innerText());
    check('tiny chart retains visible relative magnitude',await page.locator('.ui-bar-track span').nth(1).evaluate(el=>el.getBoundingClientRect().width)>2,await page.locator('.ui-bar-track span').nth(1).evaluate(el=>({width:el.getBoundingClientRect().width,css:el.style.width})));
    await show(documents['signed-metrics']);
    const signs=await page.locator('.ui-bar-track span').evaluateAll(els=>els.map(el=>({width:el.getBoundingClientRect().width,x:el.getBoundingClientRect().x,color:getComputedStyle(el).backgroundColor})));
    check('signed bars distinguish loss and gain visually',signs[0].x!==signs[2].x||signs[0].color!==signs[2].color,{bars:signs,note:'Signed values are visible as text; bar position distinguishes direction from a shared zero baseline.'});
    await show(documents['optional-select']);
    await page.getByLabel('Optional language').selectOption('English');
    check('optional select can return to unspecified',await page.getByLabel('Optional language').locator('option[value=""]').isEnabled(),await page.getByLabel('Optional language').innerHTML());
    await show(documents['numeric-comparison']);
    check('comparison preserves supplied ranking before explicit sorting',(await page.locator('tbody tr').first().innerText()).includes('Best'),await page.locator('tbody').innerText());
    await page.getByLabel('Sort by').selectOption('Score');
    check('numeric sort is numeric ascending',(await page.locator('tbody tr').first().innerText()).includes('Basic'));
    await page.getByRole('radio',{name:'Select Best'}).check();
    await page.getByLabel('Filter rows').fill('Basic');
    const choose=page.getByRole('button',{name:'Choose option',exact:true});
    const disabled=await choose.isDisabled();
    const actionCount=await page.evaluate(()=>audit.actions.length);
    if(!disabled) {
      await choose.click();
      await page.waitForFunction(n=>audit.actions.length>n,actionCount);
    }
    const submitted=await page.evaluate(n=>audit.actions.length>n ? audit.actions.at(-1).values : null,actionCount);
    check('filter does not silently submit a hidden selection',disabled||submitted?.selected!==0,{disabled,submitted});
    await page.getByLabel('Filter rows').fill('no match');
    check('empty filter has a visible result count',(await page.locator('.ui-hint').first().innerText()).includes('0 of 3'));
    await show(documents.steps);
    await page.getByRole('button',{name:'Next',exact:true}).focus();
    await page.keyboard.press('Enter');
    check('steps navigate with keyboard',await page.locator('.blackrose-ui h4').textContent()==='Check');
    check('step changes have a live announcement or focused heading',await page.evaluate(()=>!!document.querySelector('.blackrose-ui [aria-live],.blackrose-ui [role=status]')||document.activeElement.tagName.startsWith('H')),await page.evaluate(()=>({focus:document.activeElement.outerHTML,heading:document.querySelector('.blackrose-ui h4').textContent})));
    await show(documents['wide-comparison']);
    await page.setViewportSize({width:390,height:1000});
    const scroll=page.locator('.ui-table-scroll');
    await scroll.focus();
    const before=await scroll.evaluate(el=>el.scrollLeft);
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(150);
    check('wide comparison scrolls with keyboard',await scroll.evaluate(el=>el.scrollLeft)>before,
      await scroll.evaluate(el=>({wrapper:{client:el.clientWidth,scroll:el.scrollWidth},table:{client:el.firstElementChild.clientWidth,scroll:el.firstElementChild.scrollWidth,tabindex:el.firstElementChild.tabIndex}})));
    await show(documents.metrics);
    check('mobile metrics retain a visual comparison',await page.locator('.ui-bar-track').first().isVisible());
    check('no unsolicited network requests',report.network_requests.length===0,report.network_requests);
    check('no renderer exceptions',report.page_errors.length===0,report.page_errors);
    fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify({checks:report.checks,geometry_cases:report.geometry.length,geometry_failures:report.geometry.filter(g=>g.clipped.length||g.document>g.viewport+1).map(g=>({fixture:g.fixture,width:g.width,font:g.font,clipped:g.clipped}))},null,2));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
