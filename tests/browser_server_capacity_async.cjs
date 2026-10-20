'use strict';
// The browser's routes are fulfilled in memory. No TCP listener or remote request.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {firefox} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const source = path.resolve(__dirname, '..');
const output = path.join(process.env.BOARD_TEST_OUTPUT || '/private/tmp/agent-board-capacity-async-qa', 'result.json');
fs.mkdirSync(path.dirname(output), { recursive: true });
const owner='thescreamer', hash='a'.repeat(64), requests=[], held=[], errors=[];
const make=(id,observed,cleanup)=>({id,host_id:'host-one',catalog_key:id,profile_id:'profile-'+id,profile_version:1,
 launchspec_sha256:hash,mode:'messages',version:1,generation:0,desired_state:observed==='stopped'?'stopped':'running',
 observed_state:observed,observation_revision:1,reason:'',last_seen_at:1000,command_id:null,active:true,
 cleanup_confirmed:cleanup,can_start:true,control_reason:'',current_command:null});
const instances=[make('clean','stopped',true),make('busy','busy',false),make('review','needs_review',true),make('unclean','stopped',false)];
const host={id:'host-one',owner_id:owner,device_id:'device-one',name:'Synthetic host',version:1,catalog_version:1,revoked_at:null,enabled:true,instances};
const profiles=instances.map(i=>({id:i.profile_id,owner_id:owner,name:'Synthetic '+i.id,version:1,status:'active',project_ids:[],bindings:[],active_work:[]}));
const snapshot=(count=0)=>({limits:{owner_id:owner,version:1,max_concurrent:1,paused:false},active_count:count,
 executions:[],usage_observations:[]});
let capacity=snapshot(), holdAllRuntime=false, commands=0;
const browserPromise=firefox.launch({headless:true});
(async()=>{
 const browser=await browserPromise;
 try {
  const context=await browser.newContext({viewport:{width:1280,height:1000},serviceWorkers:'block'}), page=await context.newPage();
  page.on('pageerror',e=>errors.push(e.message));
  await context.route('**/*',async route=>{
   const req=route.request(), url=new URL(req.url());
   assert.equal(url.hostname,'capacity-review.invalid');
   const finish=value=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(value)});
   if(url.pathname.startsWith('/api/')){
    requests.push({method:req.method(),path:url.pathname,body:req.postDataJSON()});
    if(url.pathname==='/api/me')return finish({actor:{kind:'human',owner_id:owner},csrf_token:'synthetic',owners:[{id:owner,display_name:'Synthetic owner'}]});
    if(url.pathname==='/api/runtime'){
     const value=structuredClone(capacity);
     if(holdAllRuntime)await new Promise(resolve=>held.push(resolve));
     return finish(value);
    }
    if(url.pathname==='/api/inbox')return finish({count:0,messages:[],tickets:[],tasks:[]});
    if(['/api/agent-profiles','/api/agents'].includes(url.pathname))return finish(profiles);
    if(url.pathname==='/api/settings')return finish({devices:[{id:'device-one',owner_id:owner,label:'Synthetic device',revoked:false}]});
    if(url.pathname==='/api/server-hosts')return finish({hosts:[(( {instances,...summary})=>summary)(host)]});
    if(url.pathname==='/api/server-hosts/host-one')return finish(host);
    if(url.pathname.startsWith('/api/server-instances/')&&req.method()==='POST'){
     const body=req.postDataJSON(), instance=instances.find(i=>i.id===url.pathname.split('/')[3]);
     assert.ok(instance);assert.equal(body.action,'stop');assert.equal(body.budget,0);
     instance.version++;instance.desired_state='stopped';instance.command_id='command-'+(++commands);
     instance.current_command={id:instance.command_id,seq:commands,instance_id:instance.id,action:'stop',generation:instance.generation,
      launchspec_sha256:hash,instance_version:instance.version,budget:0,revision:1,status:'queued',stop_confirmed:false,created_at:1000,updated_at:1000};
     return finish(instance.current_command);
    }
    return finish([]);
   }
   const file=path.join(source,'web',url.pathname==='/'?'index.html':url.pathname.slice(1));
   assert.ok(file.startsWith(path.join(source,'web')+path.sep));
   if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
   return route.fulfill({status:200,contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(file)});
  });
  const card=id=>page.locator(`[data-server-instance="${id}"]`), button=(id,text)=>card(id).getByRole('button',{name:text,exact:true});
  const hosted=async()=>{await page.getByRole('button',{name:'Sur le serveur',exact:true}).click();await card('clean').waitFor();};
  const reload=async()=>{await page.getByRole('button',{name:'Actualiser',exact:true}).click();await card('clean').waitFor();};
  await page.goto('https://capacity-review.invalid/?page=agents');await hosted();
  await page.getByText('0 / 1 place occupée',{exact:true}).waitFor();
  capacity=snapshot(1);capacity.executions=[{body:'SENTINEL_PRIVATE_HISTORY'}];capacity.usage_observations=[{secret:'SENTINEL_PRIVATE_USAGE'}];
  await reload();await page.getByText('1 / 1 place occupée',{exact:true}).waitFor();
  assert.ok(await button('clean','Démarrer').isDisabled());assert.ok(await button('clean','Redémarrer').isEnabled());
  for(const id of ['review','unclean'])assert.ok(await button(id,'Démarrer').isDisabled());
  assert.ok(await button('busy','Arrêter').isEnabled());assert.ok(await button('busy','Redémarrer').isEnabled());
  assert.ok(!(await page.locator('#main').innerText()).includes('SENTINEL_'));
  // Same-owner refresh out-of-order: the late full response must not override fresh free capacity.
  holdAllRuntime=true;await reload();await page.waitForFunction(()=>document.querySelector('.server-capacity-count')?.textContent==='Capacité inconnue');
  assert.ok(await button('busy','Arrêter').isEnabled());
  await page.waitForTimeout(100);assert.equal(held.length,1);
  holdAllRuntime=false;capacity=snapshot(0);await reload();await page.getByText('0 / 1 place occupée',{exact:true}).waitFor();
  held.shift()();await page.waitForTimeout(100);assert.ok(await button('clean','Démarrer').isEnabled());
  assert.equal(await page.locator('.server-capacity-count').innerText(),'0 / 1 place occupée');
  // Critical control path: every optional capacity read stays pending during STOP.
  holdAllRuntime=true;await reload();await page.waitForTimeout(100);assert.equal(held.length,1);
  await button('busy','Arrêter').click();await page.getByRole('dialog').getByRole('button',{name:'Arrêter',exact:true}).click();
  await button('busy','Arrêt demandé').waitFor();
  await page.getByText('Demande enregistrée. L’état observé reste confirmé par le serveur.',{exact:true}).waitFor({timeout:2000});
  assert.equal(commands,1);assert.equal(held.length,2);
  const whileHeld={dialog_open:await page.getByRole('dialog').count(),notification:await page.locator('#notice').innerText(),
   notice_hidden:await page.locator('#notice').isHidden(),pending_text:await card('busy').innerText(),capacity_reads_waiting:held.length};
  const stopAcknowledgedBeforeCapacity = /Demande enregistrée/.test(whileHeld.notification)&&!whileHeld.notice_hidden;
  // Release only after verifying command confirmation while capacity is still pending.
  holdAllRuntime=false;while(held.length)held.shift()();
  await page.getByText('Demande enregistrée. L’état observé reste confirmé par le serveur.',{exact:true}).waitFor();
  const afterRelease={notification:await page.locator('#notice').innerText(),dialog_open:await page.getByRole('dialog').count()};
  assert.equal(stopAcknowledgedBeforeCapacity,true);
  assert.deepEqual(errors,[]);
  assert.ok(requests.every(r=>r.method==='GET'||r.path==='/api/server-instances/busy/commands'));
  fs.writeFileSync(output,JSON.stringify({source:'current repository web files',
   result:'control_feedback_independent_of_optional_runtime_capacity',checks:['full_only_start_and_clean_guards','same_owner_late_capacity_request_discarded','stop_pending_optional_capacity'],
   while_held:whileHeld,after_release:afterRelease,stop_acknowledged_before_capacity:stopAcknowledgedBeforeCapacity,
   sentinels_rendered:false,model_calls:0,production_calls:0,tcp_listeners:0,synthetic_commands:commands,page_errors:errors},null,2)+'\n');
  console.log(JSON.stringify({output,stop_acknowledged_before_capacity:stopAcknowledgedBeforeCapacity,while_held:whileHeld,after_release:afterRelease}));
 } finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
