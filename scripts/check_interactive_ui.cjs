/* Render the real page and trusted runtime at desktop/mobile sizes, offline. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require('playwright');

(async()=>{
  const staticRoot=path.resolve(__dirname,'../src/blackrose/gui/static');
  const artifact=JSON.parse(fs.readFileSync(path.join(staticRoot,'ui-contract.json'),'utf8'));
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({locale:'en-US',viewport:{width:1280,height:1000}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(()=>{
      window.uiTest={events:[],states:[],saved:{},navigation:[],messages:[]};
      window.pywebview={api:{ready:async()=>({commands:[],keymap:[]}),
        ui_state:async p=>{
          uiTest.states.push(p);
          if(p.values.days>7)return {ok:false,error:'days: number outside allowed range'};
          uiTest.saved[p.block_id]={saved_state:p.values,state_sequence:p.state_sequence};
          return {ok:true,state_sequence:p.state_sequence};
        },
        ui_action:async p=>{uiTest.events.push(p);return {ok:true};},
        new_conversation:async()=>{uiTest.navigation.push('new');return true;},
        submit:async text=>{uiTest.messages.push(text);return true;},save_layout:async()=>true}};
    });
    await page.goto(pathToFileURL(path.join(staticRoot,'index.html')).href);
    const blocks=Object.values(artifact.examples).map((d,i)=>({block_id:`fixture_${i}`,message_id:2,
      conversation_id:1,position:i,kind:'interactive_ui',protocol_version:1,
      schema_hash:artifact.schema_hash,library_version:artifact.library_version,source_revision:1,
      source_sequence:1,source:JSON.stringify(d),status:'ready',document:d,
      fallback_text:d.title,view_id:`view_${i}`,actions_enabled:true}));
    blocks[0].document.title='<script>globalThis.uiAttack=true</script> & literal text';
    await page.evaluate(blocks=>{
      bloom.receive([['transcript',{messages:[{role:'assistant',label:'Fixture',blocks}]}],
        ['status',{state:'Ready',generating:false,model:'fixture',can_retry:false}]]);
    },blocks);
    const days=page.getByLabel('Days per week *',{exact:true});
    assert.equal(await days.inputValue(),'');
    assert.equal(await page.getByLabel('Preferred format',{exact:true}).inputValue(),'');
    await page.getByRole('button',{name:'Plan my reading',exact:true}).click();
    assert.equal(await page.evaluate(()=>uiTest.events.length),0);
    assert(await days.evaluate(el=>el.validity.valueMissing));
    await days.fill('3');
    await page.getByRole('button',{name:'Plan my reading',exact:true}).click();
    await page.waitForFunction(()=>uiTest.events.length===1);
    assert.equal(await page.evaluate(()=>uiTest.events[0].values.days),3);
    assert.equal(await page.evaluate(()=>globalThis.uiAttack),undefined);
    await page.getByLabel('Filter rows',{exact:true}).fill('varies');
    await page.waitForFunction(()=>document.querySelector('.blackrose-ui tbody')?.children.length===1);
    assert.equal(await page.locator('.blackrose-ui').first().locator('tbody tr').count(),1);
    await page.getByLabel('Quantity multiplier',{exact:true}).fill('2');
    await page.waitForFunction(()=>[...document.querySelectorAll('.ui-total output')].some(el=>el.textContent==='50 USD'));
    await page.getByRole('button',{name:'Next',exact:true}).click();
    await page.getByRole('heading',{name:'Step 2 of 2: Check',exact:true}).waitFor();
    assert(await page.locator('.blackrose-ui h4').evaluate(el=>el===document.activeElement));
    const output=path.resolve(__dirname,'../artifacts/browser/interactive-ui');
    fs.mkdirSync(output,{recursive:true});
    for(const [label,width] of [['desktop',1280],['mobile',390]]) {
      await page.setViewportSize({width,height:1000});
      await page.locator('.blackrose-ui form').scrollIntoViewIfNeeded();
      assert(await page.getByLabel('Days per week *',{exact:true}).isVisible());
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      await page.screenshot({path:path.join(output,`${label}.png`)});
    }
    await page.emulateMedia({reducedMotion:'reduce'});
    await page.evaluate(()=>BlackroseUI.setBusy(true));
    // React commits the busy re-render asynchronously; wait for it rather than racing it.
    await page.waitForFunction(()=>[...document.querySelectorAll('.blackrose-ui button')]
      .some(button=>button.textContent.trim()==='Plan my reading'&&button.disabled),null,{timeout:5000});
    assert(await page.getByRole('button',{name:'Plan my reading',exact:true}).isDisabled());
    // Saved identities are checked independently. Readable fallback/source and
    // saved values survive, while neither edits nor continuation can run.
    const effectsBefore=await page.evaluate(()=>[uiTest.states.length,uiTest.events.length]);
    for(const mismatch of [{protocol_version:999},{library_version:'blackrose-semantic/999'},
      {schema_hash:'unsupported-historical-schema'}]) {
      await page.evaluate(block=>{
        BlackroseUI.unmountAll();
        bloom.receive([['transcript',{messages:[{role:'assistant',blocks:[block]}]}]]);
      },{...blocks[1],...mismatch,saved_state:{days:3},state_sequence:4});
      await page.getByText('This answer needs a newer UI runtime.',{exact:true}).waitFor();
      assert.equal(await page.locator('.blackrose-ui form').count(),0);
      assert.equal(await page.locator('.ui-fallback > pre').textContent(),blocks[1].fallback_text);
      assert.equal(await page.locator('.ui-fallback details pre').textContent(),blocks[1].source);
      assert.deepEqual(await page.evaluate(()=>BlackroseUI.inspect()[0].state),{days:3});
      await page.evaluate(()=>BlackroseUI.flushAll());
    }
    // An unsupported format may also change the document shape. Never inspect
    // its fields for defaults, on initial mount or on an in-place update.
    const future={...blocks[1],schema_hash:'future-schema',document:{type:'form',future_fields:[]}};
    await page.evaluate(block=>{
      BlackroseUI.unmountAll();
      bloom.receive([['transcript',{messages:[{role:'assistant',blocks:[block]}]}]]);
    },future);
    await page.getByText('This answer needs a newer UI runtime.',{exact:true}).waitFor();
    await page.evaluate(block=>{
      BlackroseUI.unmountAll();
      BlackroseUI.mount(document.querySelector('.interactive-block'),{...block,document:null},{});
      BlackroseUI.update(block.block_id,block);
    },future);
    await page.getByText('This answer needs a newer UI runtime.',{exact:true}).waitFor();
    assert.equal(await page.locator('.ui-fallback > pre').textContent(),future.fallback_text);
    assert.deepEqual(await page.evaluate(()=>[uiTest.states.length,uiTest.events.length]),effectsBefore);
    await page.evaluate(()=>{BlackroseUI.unmountAll();});
    assert.equal(await page.evaluate(()=>BlackroseUI.inspect().length),0);

    async function show(block) {
      await page.evaluate(block=>{
        bloom.receive([['transcript',{messages:[{role:'assistant',blocks:[block]}]}],
          ['status',{state:'Ready',generating:false,model:'fixture',can_retry:false}]]);
      },block);
      await page.locator('.blackrose-ui').waitFor();
      // The app refits its panes in the frame after a resize or text-size change;
      // measure after that frame, not against the previous pane layout.
      await page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done))));
    }

    const fixture=(id,document,extra={})=>({...blocks[0],block_id:id,document,
      source:JSON.stringify(document),fallback_text:document.title,...extra});
    // Prose table styles apply in the actual transcript. The labeled region,
    // rather than an unfocusable inner table, must own horizontal scrolling.
    const wideComparison={type:'comparison',title:'Wide comparison',
      columns:Array.from({length:8},(_,i)=>`Column ${i+1}`),rows:[Object.fromEntries(
        Array.from({length:8},(_,i)=>[`Column ${i+1}`,`Supplied value ${i+1}`]))]};
    const wideCalculation={type:'calculation',title:'Wide calculation',unit:'USD',
      scale_label:'Quantity multiplier',items:[{label:'Supplied item',quantity:2,unit_price:3.5}]};
    const tableGeometry=[];
    for(const width of [390,1280])for(const font of [16,32])for(const document of [wideComparison,wideCalculation]) {
      await page.setViewportSize({width,height:1000});
      await page.evaluate(font=>{
        window.document.documentElement.style.fontSize=font+'px';
        window.dispatchEvent(new Event('resize'));
      },font);
      await show(fixture(`table-${document.type}`,document));
      const region=page.getByRole('region',{name:document.title,exact:true});
      const table=region.getByRole('table');
      assert.equal(await region.getAttribute('tabindex'),'0');
      assert.equal(await table.evaluate(el=>getComputedStyle(el).display),'table');
      assert.equal(await table.getByRole('columnheader').count(),document.type==='comparison'?8:4);
      const dimensions=await region.evaluate(el=>({client:el.clientWidth,scroll:el.scrollWidth,
        tableClient:el.firstElementChild.clientWidth,tableScroll:el.firstElementChild.scrollWidth}));
      assert(dimensions.tableScroll<=dimensions.tableClient+1,'Inner table must not clip its own columns');
      if(width===390)assert(dimensions.scroll>dimensions.client,'Narrow table must overflow inside its region');
      if(dimensions.scroll>dimensions.client+1) {
        await region.focus();
        const max=dimensions.scroll-dimensions.client;
        for(let i=0;i<Math.ceil(max/20)+15;i++) {
          if(await region.evaluate(el=>el.scrollLeft>=el.scrollWidth-el.clientWidth-1))break;
          await page.keyboard.press('ArrowRight');
          await page.waitForTimeout(20);
        }
        await page.waitForFunction(el=>el.scrollLeft>=el.scrollWidth-el.clientWidth-1,await region.elementHandle());
        const atEnd=await region.evaluate(el=>el.scrollLeft);
        await page.keyboard.press('ArrowLeft');
        await page.waitForFunction(({el,before})=>el.scrollLeft<before,{el:await region.elementHandle(),before:atEnd});
        assert(await region.evaluate(el=>el===window.document.activeElement && el.matches(':focus-visible') && getComputedStyle(el).outlineWidth==='2px'));
        if(width===390)await page.screenshot({path:path.join(output,`table-${document.type}-${font}.png`)});
      }
      tableGeometry.push({type:document.type,width,font,...dimensions});
    }
    fs.writeFileSync(path.join(output,'table-scroll.json'),JSON.stringify(tableGeometry,null,2));
    await page.evaluate(()=>{
      document.documentElement.style.fontSize='';
      window.dispatchEvent(new Event('resize'));
    });
    await page.setViewportSize({width:1280,height:1000});
    const measurements=[0.00001,-0.00001,0,0.25,2500,1e-8,-1e-8,Number.MIN_VALUE];
    const displayed=['1E-5','-1E-5','0','0.25','2,500','1E-8','-1E-8','5E-324'];
    const metrics={type:'metrics',title:'Precision regression',unit:'mg',aggregate:'sum',
      data:measurements.map((value,i)=>({label:`Measurement ${i}`,value}))};
    await show(fixture('precision',metrics));
    assert.deepEqual(await page.locator('.ui-bars li > span:last-child').allTextContents(),displayed.map(v=>`${v} mg`));
    const tiny={...metrics,data:metrics.data.slice(0,3)};
    await show(fixture('tiny-bars',tiny));
    assert.equal(await page.locator('.ui-total output').textContent(),'0 mg');
    assert.deepEqual(await page.locator('.ui-bar-track span').evaluateAll(els=>els.map(el=>el.style.width)),['50%','50%','0%']);
    assert(await page.locator('.ui-bar-track span').first().evaluate(el=>el.getBoundingClientRect().width)>2);
    const positiveTiny={...metrics,data:[{label:'A',value:1e-5},{label:'B',value:2e-5},{label:'C',value:0}]};
    for(const [aggregate,total] of [['sum','3E-5 mg'],['average','1E-5 mg'],['count','3 items']]) {
      await show(fixture(`tiny-${aggregate}`,{...positiveTiny,aggregate}));
      assert.equal(await page.locator('.ui-total output').textContent(),total);
      assert.deepEqual(await page.locator('.ui-bar-track span').evaluateAll(els=>els.map(el=>el.style.width)),['50%','100%','0%']);
    }
    await show(fixture('zero-bars',{...metrics,data:[{label:'Zero',value:0}]}));
    assert.equal(await page.locator('.ui-bar-track span').evaluate(el=>el.style.width),'0%');
    await show(fixture('subunit-bars',{...metrics,data:[{label:'A',value:0.25},{label:'B',value:0.5}]}));
    assert.deepEqual(await page.locator('.ui-bar-track span').evaluateAll(els=>els.map(el=>el.style.width)),['50%','100%']);
    // Signed charts use one domain and a shared zero, at mobile and desktop
    // widths, including enlarged text. Position encodes sign without color.
    const chartGeometry=[];
    for(const width of [390,1280])for(const font of [16,32])for(const values of [[-5,0,5],[-3,0,5],[-5,-2],[-5,-2,0],[0,0],[0,2,5]]) {
      await page.setViewportSize({width,height:1000});
      await page.evaluate(font=>{document.documentElement.style.fontSize=font+'px';window.dispatchEvent(new Event('resize'));},font);
      const chart={...metrics,title:'Signed measurements',data:values.map((value,i)=>({label:`Observation ${i}: supplied measurement with a long readable label`,value}))};
      await show(fixture('signed-chart',chart));
      const geometry=await page.locator('.ui-bar-track').evaluateAll(els=>els.map(el=>{
        const track=el.getBoundingClientRect(), bar=el.firstElementChild.getBoundingClientRect();
        return {display:getComputedStyle(el).display,track:track.width,zero:parseFloat(el.style.getPropertyValue('--ui-zero')),
          left:parseFloat(el.firstElementChild.style.left),width:parseFloat(el.firstElementChild.style.width),pixels:bar.width,
          dot:getComputedStyle(el,'::after').content,sign:el.dataset.sign};
      }));
      for(const [i,g] of geometry.entries()) {
        assert.notEqual(g.display,'none');assert(g.track>30, `Bar track too narrow: ${JSON.stringify({width,font,values,i,g})}`);
        assert.equal(g.zero,geometry[0].zero);
        if(values[i]<0) {assert(g.left<g.zero);assert(Math.abs(g.left+g.width-g.zero)<0.001);}
        else assert(Math.abs(g.left-g.zero)<0.001,'Nonnegative bars start at the shared zero');
        if(values[i]===0) {assert.equal(g.width,0);assert.equal(g.dot,'""');}
        else assert(g.pixels>2);
      }
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      chartGeometry.push({width,font,values,geometry});
      if(values[0]===-5 && values[1]===0) {
        // Give the scroll pane enough height for an unobscured capture; layout
        // measurements above still use the ordinary 1000px viewport.
        await page.setViewportSize({width,height:font===32?6000:2400});
        await page.locator('.blackrose-ui').scrollIntoViewIfNeeded();
        await page.locator('.blackrose-ui').screenshot({path:path.join(output,`signed-chart-${width}-${font}.png`)});
      }
    }
    fs.writeFileSync(path.join(output,'signed-charts.json'),JSON.stringify(chartGeometry,null,2));
    await page.evaluate(()=>{document.documentElement.style.fontSize='';window.dispatchEvent(new Event('resize'));});
    await page.setViewportSize({width:1280,height:1000});

    const stepBlock=fixture('keyboard-steps',{type:'steps',title:'Keyboard review',items:[
      {title:'Read',text:'Read the source.'},{title:'Compare',text:'Compare the evidence.'},{title:'Check',text:'Check the result.'}]});
    await show(stepBlock);
    const previous=page.getByRole('button',{name:'Previous',exact:true});
    const next=page.getByRole('button',{name:'Next',exact:true});
    assert(await previous.isDisabled());
    for(const [button,name] of [[next,'Step 2 of 3: Compare'],[next,'Step 3 of 3: Check'],
      [previous,'Step 2 of 3: Compare'],[previous,'Step 1 of 3: Read']]) {
      await button.focus();await page.keyboard.press('Enter');
      const title=page.getByRole('heading',{name,exact:true});
      await title.waitFor();assert(await title.evaluate(el=>el===document.activeElement));
      assert.equal(await title.getAttribute('tabindex'),'-1');
      await page.keyboard.press('Tab');
      assert(await (name.includes('Step 1')?next:previous).evaluate(el=>el===document.activeElement));
    }
    await next.click();await next.click();
    assert(await next.isDisabled());
    await page.evaluate(()=>BlackroseUI.flushAll());
    const stepSaved=await page.evaluate(()=>uiTest.saved['keyboard-steps']);
    assert.equal(stepSaved.saved_state.step,2);
    await page.evaluate(()=>BlackroseUI.unmountAll());
    await page.locator('#input').focus();
    await show({...stepBlock,...stepSaved});
    await page.getByRole('heading',{name:'Step 3 of 3: Check',exact:true}).waitFor();
    assert(await page.locator('#input').evaluate(el=>el===document.activeElement),'Reopening must not steal focus');
    await previous.focus();await page.keyboard.press('Enter');
    assert(await page.getByRole('heading',{name:'Step 2 of 3: Compare',exact:true}).evaluate(el=>el===document.activeElement));
    await show(fixture('single-step',{...stepBlock.document,items:[stepBlock.document.items[0]]}));
    assert(await previous.isDisabled());assert(await next.isDisabled());
    for(const [i,value] of measurements.entries()) {
      if(value<0)continue; // Prices are nonnegative in the accepted source contract.
      await show(fixture(`calculation-${i}`,{type:'calculation',title:'Precision calculation',unit:'USD',
        scale_label:'Quantity multiplier',items:[{label:'Supplied item',quantity:1,unit_price:value}]}));
      assert.equal(await page.getByLabel('Quantity for Supplied item',{exact:true}).inputValue(),'1');
      assert.deepEqual(await page.locator('tbody td').allTextContents(),['',`${displayed[i]} USD`,`${displayed[i]} USD`]);
      assert.equal(await page.locator('.ui-total output').textContent(),`${displayed[i]} USD`);
    }
    await show(fixture('tiny-quantity',{type:'calculation',title:'Tiny quantity',unit:'USD',
      scale_label:'Quantity multiplier',items:[{label:'Fractional item',quantity:1e-5,unit_price:2}]}));
    assert.equal(Number(await page.getByLabel('Quantity for Fractional item',{exact:true}).inputValue()),1e-5);
    assert.deepEqual(await page.locator('tbody td').allTextContents(),['','2 USD','2E-5 USD']);
    assert.equal(await page.locator('.ui-total output').textContent(),'2E-5 USD');

    // Existing documents and scale-only snapshots gain independent item edits.
    // A zero initial quantity must still be editable, with no follow-up call.
    const office=fixture('independent-quantities',{type:'calculation',title:'Office estimate',unit:'USD',
      scale_label:'Total Cost',items:[{label:'Desk',quantity:1,unit_price:320},
        {label:'Chair',quantity:2,unit_price:210},{label:'Monitor',quantity:0,unit_price:180}]});
    const officeActionCount=await page.evaluate(()=>uiTest.events.length);
    await show({...office,saved_state:{scale:2},state_sequence:1});
    assert.equal(await page.getByLabel('Quantity multiplier',{exact:true}).inputValue(),'2');
    assert.equal(await page.getByLabel('Total Cost',{exact:true}).count(),0);
    assert.equal(await page.locator('.ui-total output').textContent(),'1,480 USD');
    await page.getByLabel('Quantity multiplier',{exact:true}).fill('1');
    await page.getByLabel('Quantity for Chair',{exact:true}).fill('1');
    await page.getByLabel('Quantity for Monitor',{exact:true}).fill('1');
    assert.equal(await page.locator('.ui-total output').textContent(),'710 USD');
    await page.evaluate(()=>BlackroseUI.flushAll());
    let officeSaved=await page.evaluate(()=>uiTest.saved['independent-quantities']);
    assert.deepEqual(officeSaved.saved_state,{scale:1,quantities:[1,1,1]});
    await page.evaluate(()=>BlackroseUI.unmountAll());
    await show({...office,...officeSaved});
    assert.equal(await page.getByLabel('Quantity for Monitor',{exact:true}).inputValue(),'1');
    assert.equal(await page.locator('.ui-total output').textContent(),'710 USD');
    await page.getByLabel('Quantity for Desk',{exact:true}).fill('0');
    await page.getByLabel('Quantity for Chair',{exact:true}).fill('0.5');
    await page.getByLabel('Quantity for Monitor',{exact:true}).fill('0');
    assert.equal(await page.locator('.ui-total output').textContent(),'105 USD');
    await page.getByLabel('Quantity multiplier',{exact:true}).fill('0.5');
    assert.equal(await page.locator('.ui-total output').textContent(),'52.5 USD');
    await page.getByLabel('Quantity multiplier',{exact:true}).fill('0');
    assert.equal(await page.locator('.ui-total output').textContent(),'0 USD');
    await page.getByLabel('Quantity for Chair',{exact:true}).fill('');
    assert.equal(await page.locator('.ui-total output').textContent(),'Unavailable USD');
    await page.evaluate(()=>BlackroseUI.flushAll());
    officeSaved=await page.evaluate(()=>uiTest.saved['independent-quantities']);
    assert.deepEqual(officeSaved.saved_state,{scale:0,quantities:[0,null,0]});
    await page.evaluate(()=>BlackroseUI.unmountAll());
    await show({...office,...officeSaved});
    assert.equal(await page.getByLabel('Quantity for Chair',{exact:true}).inputValue(),'');
    assert.equal(await page.locator('.ui-total output').textContent(),'Unavailable USD');
    assert.equal(await page.evaluate(()=>uiTest.events.length),officeActionCount);
    for(const width of [390,1280])for(const font of [16,32]) {
      await page.setViewportSize({width,height:1000});
      await page.evaluate(font=>{
        document.documentElement.style.fontSize=font+'px';window.dispatchEvent(new Event('resize'));
      },font);
      await show({...office,saved_state:{scale:1,quantities:[1,1,1]},state_sequence:2});
      const monitor=page.getByLabel('Quantity for Monitor',{exact:true});
      await monitor.focus();await monitor.scrollIntoViewIfNeeded();
      assert(await monitor.evaluate(el=>el===document.activeElement && el.clientWidth>=70));
      await page.keyboard.press('ArrowUp');
      assert.equal(await monitor.inputValue(),'2');
      assert.equal(await page.locator('.ui-total output').textContent(),'890 USD');
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      await page.screenshot({path:path.join(output,`independent-quantities-${width}-${font}.png`)});
      await page.evaluate(()=>BlackroseUI.unmountAll());
    }
    await page.evaluate(()=>{document.documentElement.style.fontSize='';window.dispatchEvent(new Event('resize'));});
    await page.setViewportSize({width:1280,height:1000});

    const comparison=fixture('filter-selection',{type:'comparison',title:'Ranked options',
      columns:['Name','Score'],rows:[{Name:'Best',Score:100},{Name:'Middle',Score:50},{Name:'Basic',Score:10}],submit_label:'Choose option'});
    await show(comparison);
    const rowNames=()=>page.locator('tbody tr td:nth-child(2)').allTextContents();
    assert.deepEqual(await rowNames(),['Best','Middle','Basic']);
    assert.equal(await page.getByLabel('Sort by',{exact:true}).inputValue(),'');
    assert(await page.getByLabel('Sort direction',{exact:true}).isDisabled());
    const choose=page.getByRole('button',{name:'Choose option',exact:true});
    assert(await choose.isDisabled());
    await page.getByRole('radio',{name:'Select Best',exact:true}).check();
    await page.getByLabel('Filter rows',{exact:true}).fill('Basic');
    assert(await choose.isDisabled());
    await page.getByRole('status').filter({hasText:'selected row is hidden'}).waitFor();
    const actionsBefore=await page.evaluate(()=>uiTest.events.length);
    await choose.evaluate(button=>button.click());
    assert.equal(await page.evaluate(()=>uiTest.events.length),actionsBefore);
    await page.getByLabel('Filter rows',{exact:true}).fill('no matches');
    assert(await choose.isDisabled());
    assert.equal(await page.locator('tbody tr').count(),0);
    await page.evaluate(()=>BlackroseUI.flushAll());
    const hiddenSaved=await page.evaluate(()=>uiTest.saved['filter-selection']);
    assert.equal(hiddenSaved.saved_state.selected,0);
    await page.evaluate(()=>BlackroseUI.unmountAll());
    await show({...comparison,...hiddenSaved});
    assert(await choose.isDisabled());
    await page.getByRole('status').filter({hasText:'selected row is hidden'}).waitFor();
    await page.getByLabel('Filter rows',{exact:true}).fill('');
    assert(await page.getByRole('radio',{name:'Select Best',exact:true}).isChecked());
    assert(await choose.isEnabled());
    await page.getByLabel('Sort by').selectOption('Score');
    assert((await page.locator('tbody tr').first().innerText()).includes('Basic'));
    await page.getByLabel('Sort direction',{exact:true}).selectOption('descending');
    assert.deepEqual(await rowNames(),['Best','Middle','Basic']);
    assert.equal(await page.getByRole('columnheader',{name:'Score',exact:true}).getAttribute('aria-sort'),'descending');
    await page.evaluate(()=>BlackroseUI.flushAll());
    const rankedSaved=await page.evaluate(()=>uiTest.saved['filter-selection']);
    await page.evaluate(()=>BlackroseUI.unmountAll());
    await show({...comparison,...rankedSaved});
    assert.deepEqual(await rowNames(),['Best','Middle','Basic']);
    assert.equal(await page.getByLabel('Sort direction',{exact:true}).inputValue(),'descending');
    for(const width of [390,1280]) {
      await page.setViewportSize({width,height:2400});
      await page.locator('.blackrose-ui').screenshot({path:path.join(output,`comparison-order-${width}.png`)});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    }
    assert(await page.getByRole('radio',{name:'Select Best',exact:true}).isChecked());
    await choose.click();
    await page.waitForFunction(n=>uiTest.events.length===n+1,actionsBefore);
    assert.equal(await page.evaluate(()=>uiTest.events.at(-1).values.selected),0);
    await page.getByLabel('Filter rows',{exact:true}).fill('Basic');
    await page.getByRole('radio',{name:'Select Basic',exact:true}).check();
    await choose.click();
    await page.waitForFunction(n=>uiTest.events.length===n+2,actionsBefore);
    assert.equal(await page.evaluate(()=>uiTest.events.at(-1).values.selected),2);

    const dated=fixture('dated-order',{type:'comparison',title:'Supplied chronology',columns:['Name','Date','Value'],rows:[
      {Name:'Later',Date:'2026-11-03',Value:0.5},{Name:'Earlier',Date:'2026-01-01',Value:-2},
      {Name:'Same date',Date:'2026-11-03',Value:10}]});
    await show(dated);
    const names=()=>page.locator('tbody tr td:first-child').allTextContents();
    assert.deepEqual(await names(),['Later','Earlier','Same date']);
    await page.getByLabel('Sort by',{exact:true}).selectOption('Date');
    assert.deepEqual(await names(),['Earlier','Later','Same date']);
    await page.getByLabel('Sort direction',{exact:true}).selectOption('descending');
    assert.deepEqual(await names(),['Later','Same date','Earlier']);
    await page.getByLabel('Sort by',{exact:true}).selectOption('Value');
    assert.deepEqual(await names(),['Same date','Later','Earlier']);
    await page.getByLabel('Sort direction',{exact:true}).selectOption('ascending');
    assert.deepEqual(await names(),['Earlier','Later','Same date']);
    await page.getByLabel('Sort by',{exact:true}).selectOption('');
    assert.deepEqual(await names(),['Later','Earlier','Same date']);
    await page.evaluate(()=>BlackroseUI.flushAll());
    const sourceSaved=await page.evaluate(()=>uiTest.saved['dated-order']);
    assert(!('sort' in sourceSaved.saved_state));
    await page.evaluate(()=>BlackroseUI.unmountAll());
    await show({...dated,...sourceSaved});
    assert.deepEqual(await names(),['Later','Earlier','Same date']);
    await page.evaluate(()=>BlackroseUI.unmountAll());
    await show({...dated,saved_state:{sort:'Value'},state_sequence:1});
    assert.deepEqual(await names(),['Earlier','Later','Same date'],'Legacy column-only state stays ascending');

    // Rejected edits stay visible, but cannot veto New chat or ordinary messages.
    await show(blocks[1]);
    await page.getByLabel('Days per week *',{exact:true}).fill('99');
    await page.locator('#new-chat').click();
    await page.waitForFunction(()=>uiTest.navigation.length===1);
    assert.equal(await page.getByLabel('Days per week *',{exact:true}).inputValue(),'99');
    await page.locator('.ui-error').filter({hasText:'days: number outside allowed range'}).waitFor();
    await page.locator('#input').fill('An ordinary Markdown question');
    await page.locator('#composer').evaluate(form=>form.requestSubmit());
    await page.waitForFunction(()=>uiTest.messages.length===1);
    assert.equal(await page.evaluate(()=>uiTest.messages[0]),'An ordinary Markdown question');
    // UI continuation still requires a successful save, even if native validity is bypassed.
    const actionCount=await page.evaluate(()=>uiTest.events.length);
    await page.locator('.blackrose-ui form').evaluate(form=>form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
    await page.waitForFunction(()=>!document.querySelector('.blackrose-ui button[type="submit"]').disabled);
    assert.equal(await page.evaluate(()=>uiTest.events.length),actionCount);

    // A persisted empty state is different from a form which has never been edited.
    const optionalDocument={type:'form',title:'Optional quantity',submit_label:'Use quantity',
      fields:[{id:'count',label:'Optional count',kind:'number',default:5}]};
    const optional={...blocks[1],block_id:'optional',document:optionalDocument,source:JSON.stringify(optionalDocument)};
    await show(optional);
    assert.equal(await page.getByLabel('Optional count',{exact:true}).inputValue(),'5');
    await page.getByLabel('Optional count',{exact:true}).fill('');
    await page.evaluate(()=>BlackroseUI.flushAll());
    const saved=await page.evaluate(()=>uiTest.saved.optional);
    assert.deepEqual(saved,{saved_state:{},state_sequence:1});
    await show({...optional,...saved});
    assert.equal(await page.getByLabel('Optional count',{exact:true}).inputValue(),'');
    await show({...optional,saved_state:{count:2},state_sequence:2});
    assert.equal(await page.getByLabel('Optional count',{exact:true}).inputValue(),'2');

    // Optional choices can return to omission; required choices need an answer.
    const choiceDocument={type:'form',title:'Language preferences',submit_label:'Use languages',fields:[
      {id:'optional_language',label:'Optional language',kind:'select',options:['English','Spanish'],default:'English'},
      {id:'required_language',label:'Required language',kind:'select',options:['English','Spanish'],required:true}]};
    const choiceBlock=fixture('clear-choice',choiceDocument);
    await show(choiceBlock);
    const optionalChoice=page.getByLabel('Optional language',{exact:true});
    const requiredChoice=page.getByLabel('Required language *',{exact:true});
    assert.equal(await optionalChoice.inputValue(),'English');
    assert.equal(await requiredChoice.inputValue(),'');
    assert(await requiredChoice.locator('option[value=""]').isDisabled());
    const beforeChoice=await page.evaluate(()=>uiTest.events.length);
    await page.getByRole('button',{name:'Use languages',exact:true}).click();
    assert.equal(await page.evaluate(()=>uiTest.events.length),beforeChoice);
    await optionalChoice.selectOption('Spanish');
    await requiredChoice.selectOption('English');
    await optionalChoice.selectOption('');
    // Debounced saves omit cleared choices rather than passing an invalid "".
    await page.waitForFunction(()=>uiTest.saved['clear-choice']?.saved_state.required_language==='English' &&
      !('optional_language' in uiTest.saved['clear-choice'].saved_state));
    await optionalChoice.selectOption('Spanish');
    await optionalChoice.selectOption('');
    await requiredChoice.focus(); // blur flushes without waiting for debounce
    await page.waitForFunction(()=>uiTest.saved['clear-choice']?.state_sequence>=2);
    const choiceSaved=await page.evaluate(()=>uiTest.saved['clear-choice']);
    assert.deepEqual(choiceSaved.saved_state,{required_language:'English'});
    await page.evaluate(()=>BlackroseUI.unmountAll());
    await show({...choiceBlock,...choiceSaved});
    assert.equal(await optionalChoice.inputValue(),'');
    assert.equal(await requiredChoice.inputValue(),'English');
    await page.getByRole('button',{name:'Use languages',exact:true}).click();
    await page.waitForFunction(n=>uiTest.events.length===n+1,beforeChoice);
    assert.deepEqual(await page.evaluate(()=>uiTest.events.at(-1).values),{required_language:'English'});

    // The first valid revision initializes defaults without replacing the live view.
    const invalid={...blocks[1],block_id:'repair',document:null,status:'invalid',source:'bad'};
    await show(invalid);
    // This fixture explicitly supplies initial preferences, unlike the blank example.
    const defaultDocument=structuredClone(artifact.examples.form);
    defaultDocument.fields[0].default=5;
    defaultDocument.fields[1].default='Either';
    const repaired={...invalid,document:defaultDocument,status:'ready',source_revision:2,
      source_sequence:2,source:JSON.stringify(defaultDocument)};
    await page.evaluate(block=>BlackroseUI.update(block.block_id,block),repaired);
    await page.getByLabel('Days per week *',{exact:true}).waitFor();
    assert.equal(await page.getByLabel('Days per week *',{exact:true}).inputValue(),'5');
    assert.equal(await page.getByLabel('Preferred format',{exact:true}).inputValue(),'Either');
    await page.getByLabel('Days per week *',{exact:true}).fill('3');
    await page.evaluate(block=>BlackroseUI.update(block.block_id,block),{...repaired,source_sequence:3});
    assert.equal(await page.getByLabel('Days per week *',{exact:true}).inputValue(),'3');
    await page.evaluate(()=>BlackroseUI.flushAll());
    assert.equal(await page.evaluate(()=>uiTest.saved.repair.saved_state.days),3);
    await page.evaluate(()=>BlackroseUI.unmountAll());
    assert.deepEqual(errors,[]);

    // Load the real page with an unavailable or syntactically corrupt bundle.
    // Route only this local resource; no files are modified and no network is used.
    for(const broken of [{status:404,body:''},{status:200,body:'const = corrupt bundle;'}]) {
      const fallbackPage=await browser.newPage();
      try {
        await fallbackPage.route('**/ui-runtime.js',route=>route.fulfill({...broken,contentType:'text/javascript'}));
        await fallbackPage.goto(pathToFileURL(path.join(staticRoot,'index.html')).href);
        await fallbackPage.evaluate(block=>bloom.receive([['transcript',{messages:[
          {role:'assistant',blocks:[block]}, {role:'assistant',html:'<p>Ordinary saved answer.</p>'}]}]]),blocks[1]);
        await fallbackPage.getByText(blocks[1].fallback_text,{exact:true}).waitFor();
        await fallbackPage.getByText('Ordinary saved answer.',{exact:true}).waitFor();
        assert.equal(await fallbackPage.locator('.interactive-block form').count(),0);
        assert.equal(await fallbackPage.evaluate(()=>typeof BlackroseUI),'undefined');
      } finally {await fallbackPage.close();}
    }
    console.log('Interactive UI passed: actual page, literal text, numeric precision, signed/mobile chart baselines, source order and sort directions with legacy replay, keyboard step focus and replay, keyboard table scrolling and semantics at narrow/scaled sizes, filtered selection guards and replay, local controls, state flush, invalid-edit navigation, cleared-state replay, repaired defaults, incompatible saved identities, missing/corrupt runtime fallback, busy guard, unmount, desktop/mobile layout and reduced motion.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
