// Isolated fixture only. No production sessions, accounts or writes.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {firefox}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin='http://127.0.0.1:18766';
const output=process.env.BOARD_TEST_OUTPUT||'/tmp/board-markdown-qa';
fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await firefox.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1100}});
  const login=await (await context.request.post(origin+'/api/login',{data:{username:'blackoutburst',password:'local-ui-fixture-only'}})).json();
  const headers={'X-CSRF-Token':login.csrf_token};
  const api=async(route,data,method='POST')=>{const r=await context.request.fetch(origin+'/api'+route,{method,headers,data});assert.equal(r.status(),200,await r.text());return r.json();};
  const fencedCode='const answer = 42;\n  indented();  \n\tconst greeting = "雪 café 🚀";\nconst literal = "<b>literal & text</b>";\n';
  const indentedCode='indented code  \n  nested\t雪 <i>literal & safe</i>\n';
  const indentedMarkdown=indentedCode.split('\n').slice(0,-1).map(line=>'    '+line).join('\n');
  const markdown='# Rich heading\n\n**Bold text** and *emphasis* with ~~removed~~ and `inline only`.\n\n- First item\n- Second item\n\n> A quoted decision\n\n| Key | Value |\n| --- | --- |\n| Name | Test |\n\n```js\n'+fencedCode+'```\n\n'+indentedMarkdown+'\n\n```text\n```\n\n[Reference](https://example.com/docs)\n\nPlain line one\nPlain line two';
  const p=await api('/projects',{name:'Markdown project',participants:['blackoutburst','thescreamer'],description:'Project **description**',context:markdown,environment:'Environment `local`',idempotency_key:'markdown-project'});
  let task=await api('/projects/'+p.id+'/tickets',{title:'Markdown task',description:markdown,acceptance_criteria:'## Acceptance\n\n1. **First check**\n2. Second check',idempotency_key:'markdown-task'});
  task=await api('/tickets/'+task.id,{expected_version:task.version,progress:'Progress **reported**',outcome:'Outcome *recorded*',evidence:'Evidence `checked`'},'PATCH');
  const agents=await api('/agents?project_id='+p.id,undefined,'GET');
  const agent=agents.find(a=>a.name==='Claude Code · Mac Solem');assert.ok(agent);
  const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6kL8AAAAASUVORK5CYII=','base64');
  const upload=await context.request.post(origin+'/api/threads/'+task.thread_id+'/attachments',{headers,multipart:{file:{name:'local.png',mimeType:'image/png',buffer:png}}});
  assert.equal(upload.status(),200);const image=await upload.json();
  const hostile='<img src=x onerror="window.__markdownXss=1">\n\n<svg onload="window.__markdownXss=2"></svg>\n\n[bad](javascript:window.__markdownXss=3) [encoded](javascript&#58;alert(1)) [data](data:text/html,test)\n\n![remote](https://example.invalid/tracker.png)\n\n`@Claude Code · Mac Solem` and @Claude Code · Mac Solem';
  await api('/threads/'+task.thread_id+'/messages',{body:markdown+'\n\n'+hostile+'\n\n![Local picture]('+origin+'/api/attachments/'+image.id+'?inline=true)',mentions:['agent:'+agent.id],idempotency_key:'markdown-message'});
  const request=await api('/tickets/'+task.id+'/input',{owner_id:'blackoutburst',question:'## Needed\n\nPlease **confirm** the decision.',idempotency_key:'markdown-request'});
  const page=await context.newPage();const errors=[],externalImages=[];
  const captureClipboard=()=>page.evaluate(()=>{
   window.__codeCopyCalls=[];window.__codeCopyRejectNext=false;
   Object.defineProperty(navigator.clipboard,'writeText',{configurable:true,value:async value=>{
    window.__codeCopyCalls.push(value);
    if(window.__codeCopyRejectNext){window.__codeCopyRejectNext=false;throw new DOMException('Synthetic clipboard denial','NotAllowedError');}
   }});
  });
  const copyAndCheck=async(block,expected,key)=>{
   const button=block.locator('button');
   assert.equal(await button.count(),1);
   assert.equal(await button.getAttribute('type'),'button');
   const before=await page.evaluate(()=>window.__codeCopyCalls.length);
   if(key){await button.focus();assert.ok(await button.evaluate(node=>node===document.activeElement));await button.press(key);}
   else await button.click();
   await block.getByRole('button',{name:'Copied!',exact:true}).waitFor();
   assert.deepEqual(await page.evaluate(()=>window.__codeCopyCalls.slice(-1)),[expected]);
   assert.equal(await page.evaluate(()=>window.__codeCopyCalls.length),before+1);
   assert.equal(await block.locator('pre').textContent(),expected);
  };
  await page.addInitScript(()=>window.__markdownXss=0);
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.url().includes('example.invalid'))externalImages.push(r.url());});
  await page.goto(origin+'/?project='+p.id+'&ticket='+task.id);
  await page.getByRole('heading',{name:'Markdown task',exact:true}).waitFor();
  const description=page.locator('.panel').filter({has:page.getByRole('heading',{name:'Description',exact:true})});
  await description.getByRole('heading',{name:'Rich heading',exact:true}).waitFor({timeout:1500});
  assert.equal(await description.locator('strong').textContent(),'Bold text');
  assert.equal(await description.locator('ul li').count(),2);
  assert.equal((await description.locator('blockquote').textContent()).trim(),'A quoted decision');
  assert.equal(await description.locator('table tbody tr').count(),1);
  assert.equal(await description.locator('pre').count(),3);
  assert.equal(await description.locator('.code-block').count(),3);
  assert.equal(await description.getByRole('button',{name:'Copy code',exact:true}).count(),3);
  assert.deepEqual(await description.locator('pre').allTextContents(),[fencedCode,indentedCode,'']);
  assert.ok(await description.locator('pre').evaluateAll(nodes=>nodes.every(node=>node.parentElement.matches('.code-block'))));
  assert.equal(await description.locator('code').filter({hasText:'inline only'}).evaluate(node=>node.closest('.code-block')),null);
  await captureClipboard();
  const copyBlocks=description.locator('.code-block');
  await copyAndCheck(copyBlocks.nth(0),fencedCode);
  await copyAndCheck(copyBlocks.nth(0),fencedCode); // Repeat while its success label is still visible.
  await copyAndCheck(copyBlocks.nth(1),indentedCode,'Enter');
  await copyAndCheck(copyBlocks.nth(2),'','Space');
  await copyBlocks.nth(0).getByRole('button',{name:'Copy code',exact:true}).waitFor({timeout:3500});
  await page.evaluate(()=>{window.__codeCopyRejectNext=true;window.getSelection().removeAllRanges();});
  await copyBlocks.nth(1).locator('button').click();
  await copyBlocks.nth(1).getByRole('button',{name:'Select and copy',exact:true}).waitFor();
  assert.equal(await copyBlocks.nth(1).getByRole('button',{name:'Copied!',exact:true}).count(),0);
  assert.deepEqual(await page.evaluate(()=>window.__codeCopyCalls),[fencedCode,fencedCode,indentedCode,'',indentedCode]);
  // Firefox exposes Windows selection line endings as CRLF; clipboard payloads above remain exact.
  assert.equal(await page.evaluate(()=>window.getSelection().toString().replace(/\r\n/g,'\n')),indentedCode);
  assert.ok(await copyBlocks.nth(1).locator('pre').evaluate(node=>{
   const selection=window.getSelection();
   return node.contains(selection.anchorNode)&&node.contains(selection.focusNode);
  }));
  await copyAndCheck(copyBlocks.nth(1),indentedCode); // A denial does not disable a later retry.
  assert.equal(await page.getByRole('heading',{name:'Acceptance',exact:true}).count(),1);
  assert.equal(await page.getByRole('heading',{name:'Needed',exact:true}).count(),1);
  assert.equal(await page.getByText('reported',{exact:true}).evaluate(n=>n.tagName),'STRONG');
  const messages=page.locator('#messages');
  assert.equal(await messages.getByRole('heading',{name:'Rich heading',exact:true}).count(),1);
  assert.equal(await messages.locator('svg,script,iframe,style,form,input').count(),0);
  assert.equal(await messages.locator('img').count(),1);
  await messages.getByRole('img',{name:'Local picture',exact:true}).scrollIntoViewIfNeeded();
  await page.waitForFunction(()=>document.querySelector('#messages img')?.naturalWidth>0);
  assert.equal(await messages.locator('img[onerror],img[onload]').count(),0);
  assert.equal(await messages.locator('a[href^="javascript:"],a[href^="data:"]').count(),0);
  assert.equal(await messages.locator('code .mention').count(),0);
  assert.equal(await messages.locator('.markdown-body .mention').count(),1);
  assert.equal(await page.evaluate(()=>window.__markdownXss),0);
  assert.deepEqual(externalImages,[]);
  assert.equal(await messages.getByRole('link',{name:'Reference',exact:true}).getAttribute('rel'),'noopener noreferrer');
  await page.getByRole('button',{name:'Edit task',exact:true}).click();
  assert.equal(await page.getByRole('dialog').getByLabel('Description',{exact:true}).inputValue(),markdown);
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  await page.getByRole('link',{name:'Markdown project',exact:true}).click();
  await page.getByRole('button',{name:'Overview',exact:true}).click();
  await page.getByRole('heading',{name:'Rich heading',exact:true}).waitFor();
  assert.equal(await page.getByText('description',{exact:true}).evaluate(n=>n.tagName),'STRONG');
  await page.screenshot({path:path.join(output,'markdown-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await copyAndCheck(page.locator('.code-block').first(),fencedCode);
  await page.screenshot({path:path.join(output,'markdown-mobile.png'),fullPage:true});
  await page.goto(origin+'/?page=setup');
  await page.getByRole('heading',{name:'Connect an agent',exact:true}).waitFor();
  await captureClipboard();
  const connector=page.locator('.panel').filter({has:page.getByRole('heading',{name:'2. Run the connector',exact:true})});
  assert.equal(await connector.locator('.code-block').count(),2);
  await copyAndCheck(connector.locator('.code-block').nth(0),'python3 ~/Downloads/board-connect.py');
  await copyAndCheck(connector.locator('.code-block').nth(1),'python3 ~/Downloads/board-connect.py --client claude-code','Enter');
  const staticCode='  <img src=x onerror="window.__markdownXss=9">\n\t雪 & literal  \n';
  await page.evaluate(value=>{
   window.__codeFormSubmits=0;
   const form=document.createElement('form');form.id='code-helper-fixture';
   form.addEventListener('submit',event=>{event.preventDefault();window.__codeFormSubmits++;});
   form.append(Markdown.codeBlock(value,'prose'));document.querySelector('#main').append(form);
  },staticCode);
  const helper=page.locator('#code-helper-fixture .code-block');
  assert.equal(await helper.count(),1);
  assert.equal(await helper.locator('img').count(),0);
  assert.equal(await helper.locator('pre').getAttribute('class'),'prose');
  await copyAndCheck(helper,staticCode,'Enter');
  assert.equal(await page.evaluate(()=>window.__codeFormSubmits),0);
  assert.equal(await page.evaluate(()=>window.__markdownXss),0);
  await page.locator('#code-helper-fixture').evaluate(node=>node.remove());
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({pass:true,flows:['project description/context/environment','task description/criteria/progress/outcome/evidence','discussion markdown','request markdown','HTML and unsafe URL rejection','same-origin image renders without external image fetch','mention highlighting outside code','source-preserving editor','fenced/indented/empty code copy with exact Unicode and whitespace','inline code excluded','keyboard and repeat copy with label reset','clipboard denial selects only visible code and permits retry','static setup commands and literal helper do not submit forms','desktop/mobile copy'],javascriptErrors:errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
