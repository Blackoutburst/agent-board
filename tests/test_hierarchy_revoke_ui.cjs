// Exercise the real UI handler with a mocked transport; no browser or live grants.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('web/workspace.js', 'utf8');
const handler = source.slice(source.indexOf('  async function revokeHierarchy('), source.indexOf('  async function renderHierarchy('));
async function run() {
  const row = {id:'grant',owner_id:'alice',status:'active',version:4,beneficiary_id:'profile'};
  let submit, posts=[], fail=true, closed=0;
  const context = {state:{actor:{kind:'human',owner_id:'alice'}},
    workPath:encodeURIComponent,idempotencyKey:()=> 'stable-key',
    el:()=>({}),formButtons:()=>({}),bindForm:(_form,action)=>submit=action,
    openModal:()=>{},closeModal:()=>closed++,agents:async()=>{},
    api:async(path,options)=> {
      if (!options) return row;
      posts.push(options.data);
      if (fail) throw new Error('ambiguous network response');
      return Object.assign({},row,{status:'revoked',version:5});
    }};
  vm.createContext(context);vm.runInContext(handler+'\nthis.revoke = revokeHierarchy;',context);
  await context.revoke('grant');
  await assert.rejects(submit(),/ambiguous/);fail=false;
  await submit();assert.equal(posts.length,2);
  assert.equal(JSON.stringify(posts[0]),JSON.stringify(posts[1]));
  assert.equal(posts[0].expected_version,4);assert.equal(posts[0].revoke,true);assert.equal(closed,1);
  context.state.actor.owner_id='bob';await assert.rejects(submit(),/Compte changé/);
  assert.equal(posts.length,2);
  context.state.actor={kind:'agent',owner_id:'alice'};submit=null;
  await context.revoke('grant');assert.equal(submit,null);
  context.state.actor={kind:'human',owner_id:'alice'};row.status='revoked';
  await assert.rejects(context.revoke('grant'),/déjà révoquée/);
  console.log('UI revocation: stable retry, CAS, changed owner and agent denied, revoked history preserved');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
