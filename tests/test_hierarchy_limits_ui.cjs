// Real form handler, mocked transport; no live permissions or browser.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('web/workspace.js','utf8');
const handler=source.slice(source.indexOf('  async function hierarchyLimitsEditor('),source.indexOf('  async function revokeHierarchy('));
async function run() {
 let submit,posts=[],fail=true,closed=0;
 const ctx={state:{actor:{kind:'human',owner_id:'alice'}},el:()=>({}),check:()=>({}),field:()=>({}),select:()=>({}),
  formButtons:()=>({}),openModal:()=>{},bindForm:(_f,a)=>submit=a,idempotencyKey:()=> 'limits-key',closeModal:()=>closed++,agents:async()=>{},
  api:async(_path,options)=>{if(!options)return {limits:null};posts.push(options.data);if(fail)throw new Error('uncertain');return {owner_id:'alice',version:1};}};
 vm.createContext(ctx);vm.runInContext(handler+'\nthis.edit=hierarchyLimitsEditor;',ctx);await ctx.edit();
 const values=new Map([['max_depth','0'],['autonomy','responses']]);
 for(const kind of ['projects','profiles','delegations'])for(const key of ['live','created'])values.set(kind+'_'+key,'0');
 await assert.rejects(submit(values),/Confirmez/);assert.equal(posts.length,0);
 values.set('confirm_limits','on');values.set('profiles_live','1.5');await assert.rejects(submit(values),/entiers/);assert.equal(posts.length,0);
 values.set('profiles_live','0');await assert.rejects(submit(values),/uncertain/);fail=false;await submit(values);
 assert.equal(JSON.stringify(posts[0]),JSON.stringify(posts[1]));assert.equal(posts[0].expected_version,0);
 assert.equal(posts[0].policy.allow_new_projects,false);assert.equal(posts[0].policy.operations.length,0);assert.equal(closed,1);
 ctx.state.actor.owner_id='bob';await assert.rejects(submit(values),/Compte changé/);assert.equal(posts.length,2);
 console.log('Limits UI: explicit confirmation, no default grant, integer validation, stable retry and owner fence pass');
}
run().catch(e=>{console.error(e);process.exitCode=1;});
