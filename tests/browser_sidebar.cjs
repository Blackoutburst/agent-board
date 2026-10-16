// Synthetic loopback fixture only; verify viewport behavior, not document visibility.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const playwright=require(process.env.PLAYWRIGHT_MODULE||'/Users/florianchanson/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const engine=process.env.BOARD_TEST_BROWSER||'chromium';
const origin='http://127.0.0.1:18766';
const output=process.env.BOARD_TEST_OUTPUT||'/tmp/board-sidebar-qa';fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await playwright[engine].launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const login=await(await context.request.post(origin+'/api/login',{data:{username:'blackoutburst',password:'local-ui-fixture-only'}})).json();
  const headers={'X-CSRF-Token':login.csrf_token};
  const post=async(route,data)=>{const r=await context.request.post(origin+'/api'+route,{headers,data});assert.equal(r.status(),200);return r.json();};
  const p=await post('/projects',{name:'Sidebar viewport',participants:['blackoutburst'],idempotency_key:'sidebar-project'});
  const t=await post('/projects/'+p.id+'/tickets',{title:'Long page',description:Array.from({length:100},(_,i)=>`## Section ${i+1}\n\nParagraph content for the long page.`).join('\n\n'),idempotency_key:'sidebar-task'});
  for(let i=0;i<30;i++)await post('/threads',{title:'Scroll conversation '+String(i+1).padStart(2,'0'),participants:['blackoutburst'],idempotency_key:'sidebar-thread-'+i});
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(origin+'/?project='+p.id+'&ticket='+t.id);
  await page.getByRole('heading',{name:'Long page',exact:true}).waitFor();
  const verify=async(label)=>{
   const checks=await page.locator('.sidebar-foot').evaluate(foot=>[...foot.querySelectorAll('button,a')].map(n=>{const r=n.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return {name:n.textContent.trim(),visible:r.top>=0&&r.bottom<=innerHeight+1,clickable:hit===n||n.contains(hit)};}));
   assert.equal(checks.length,3);
   for(const result of checks){assert.ok(result.visible,label+': '+result.name+' must be in viewport');assert.ok(result.clickable,label+': '+result.name+' must be clickable');}
   const geometry=await page.locator('.sidebar').evaluate(side=>{const footer=side.querySelector('.sidebar-foot').getBoundingClientRect();return {bottom:footer.bottom+parseFloat(getComputedStyle(side).paddingBottom),viewport:innerHeight};});
   assert.ok(Math.abs(geometry.bottom-geometry.viewport)<=2,label+': footer should sit at viewport bottom');
  };
  await verify('page start');
  await page.evaluate(()=>window.scrollTo({top:document.documentElement.scrollHeight/2,behavior:'instant'}));
  await verify('page middle');
  await page.locator('.conversation-nav>summary').click();
  const menu=page.locator('.thread-list');
  await menu.getByText('Scroll conversation 30',{exact:true}).waitFor({state:'attached'});
  assert.ok(await menu.evaluate(n=>n.scrollHeight>n.clientHeight),'long conversation list must scroll independently');
  const scroll=await page.evaluate(()=>window.scrollY);
  await menu.evaluate(n=>n.scrollTop=n.scrollHeight);
  await verify('expanded conversations');assert.equal(await page.evaluate(()=>window.scrollY),scroll);
  await page.evaluate(()=>window.scrollTo({top:document.documentElement.scrollHeight,behavior:'instant'}));
  await verify('page end');
  await page.screenshot({path:path.join(output,'sidebar-long-page.png'),fullPage:false});
  for(const viewport of [{width:1000,height:620},{width:1440,height:500}]){
   await page.setViewportSize(viewport);await verify('resized '+viewport.width+'x'+viewport.height);
  }
  await page.setViewportSize({width:390,height:844});
  await page.evaluate(()=>window.scrollTo({top:0,behavior:'instant'}));
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  for(const name of ['Settings','Connect agent','Documentation'])assert.ok(await page.locator('.sidebar-foot').getByText(name,{exact:true}).isVisible());
  await page.screenshot({path:path.join(output,'sidebar-mobile.png'),fullPage:false});
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({pass:true,browser:engine,flows:['long-page start/middle/end','all footer controls visible and clickable','conversation list independent scroll','short and medium desktop viewports','mobile navigation preserved'],javascriptErrors:errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
