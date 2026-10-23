const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const playwright = require(process.env.PLAYWRIGHT_MODULE || '/Users/florianchanson/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const browserName = process.env.BOARD_TEST_BROWSER || 'chromium';
const output = process.env.BOARD_TEST_OUTPUT || '/tmp/board-request-choices';
const root = path.resolve(__dirname, '..'), origin = 'http://127.0.0.1:18775';
fs.mkdirSync(output, {recursive:true});
(async () => {
  let browser, server, serverErrors = '';
  const errors = [], resolutions = [];
  try {
    server = spawn(process.env.BOARD_TEST_PYTHON || path.join(root, '.venv/bin/python'), ['tests/serve_request_choices_preview.py'],
      {cwd:root, env:{...process.env,PYTHONPATH:root}, stdio:['ignore','ignore','pipe']});
    server.stderr.on('data', value => {serverErrors += String(value);});
    for (let i=0;i<200;i++) {
      if (server.exitCode !== null) throw new Error(serverErrors);
      try {if ((await fetch(origin)).ok) break;} catch {}
      await new Promise(resolve => setTimeout(resolve,50));
    }
    browser = await playwright[browserName].launch({headless:true, args:browserName==='chromium'?['--mute-audio']:[]});
    const context = await browser.newContext({viewport:{width:1280,height:1000}}), page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {if(request.url().includes('/resolve') && request.method()==='POST') resolutions.push(request.postDataJSON());});
    await page.goto(origin);
    await page.getByText('Local development sign-in',{exact:true}).click();
    await page.getByLabel('Username',{exact:true}).fill('blackoutburst');
    await page.getByLabel('Password',{exact:true}).fill('local-ui-fixture-only');
    await page.getByRole('button',{name:'Sign in',exact:true}).click();
    await page.getByRole('heading',{name:'Projects',exact:true}).waitFor();
    await page.getByRole('button',{name:'Needs you',exact:false}).click();
    const card = title => page.locator('.human-request').filter({has:page.getByRole('link',{name:new RegExp('^#\\d+ '+title+'$')})});
    const choices = card('Choices');
    await choices.waitFor();
    assert.equal(await choices.locator('details').getAttribute('open'),null);
    assert.equal(await choices.getByText(/LONG_CONTEXT_SENTINEL/).isVisible(),false);
    assert.equal(await choices.getByRole('radio').count(),4);
    assert.equal(await choices.getByRole('radio').evaluateAll(items=>items.some(item=>item.checked)),false);
    assert.equal(await choices.getByRole('button',{name:'Send answer',exact:true}).isDisabled(),true);
    await choices.getByRole('radio',{name:'Staging first',exact:true}).check();
    assert.equal(resolutions.length,0,'Selecting a choice does not submit');
    await page.screenshot({path:path.join(output,'needs-you-choices-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await choices.screenshot({path:path.join(output,'needs-you-choice-card-mobile.png')});
    await page.setViewportSize({width:1280,height:1000});
    await choices.locator('details>summary').click();
    assert.equal(await choices.getByText(/LONG_CONTEXT_SENTINEL/).isVisible(),true);
    await choices.locator('details>summary').click();
    // An interrupted save preserves the selected answer for an explicit retry.
    await page.route('**/api/human-requests/*/resolve', async route => {
      await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Synthetic retry'})});
    }, {times:1});
    await choices.getByRole('button',{name:'Send answer',exact:true}).click();
    await choices.getByRole('alert').waitFor();
    assert.equal(await choices.getByRole('radio',{name:'Staging first',exact:true}).isChecked(),true);
    await choices.getByRole('button',{name:'Send answer',exact:true}).click();
    await choices.waitFor({state:'detached'});
    const custom = card('Custom reply');
    await custom.getByRole('radio',{name:'Write a custom reply',exact:true}).check();
    await custom.getByLabel('Your response',{exact:true}).fill('Purple — été 日本語');
    await custom.getByRole('button',{name:'Send answer',exact:true}).click();
    await custom.waitFor({state:'detached'});
    const command = card('Command'), expectedCommand = "printf '%s\\n' 'été 日本語'\n";
    assert.equal(await command.locator('.request-command').textContent(),expectedCommand);
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.__copiedCommand=text;}}}));
    await command.getByRole('button',{name:'Copy command',exact:true}).click();
    assert.equal(await page.evaluate(()=>window.__copiedCommand),expectedCommand);
    assert.equal(resolutions.length,3,'Copying does not complete an action');
    await card('Approval').getByRole('radio',{name:'Decline',exact:true}).check();
    await card('Approval').getByRole('button',{name:'Send answer',exact:true}).click();
    await card('Approval').waitFor({state:'detached'});
    assert.equal(await card('Legacy action').locator('.request-command').textContent(),'printf legacy');
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:path.join(output,'needs-you-choices-mobile.png'),fullPage:true});
    await page.reload();
    await card('Command').waitFor();
    const all = await (await context.request.get(origin+'/api/human-requests?include_resolved=true')).json();
    for(const [title,answer] of [['Choices','Staging first'],['Custom reply','Purple — été 日本語'],['Approval','Decline']]) {
      const request = all.find(item=>item.title===title);
      assert.equal(request.status,'resolved'); assert.equal(request.answer,answer);
    }
    assert.equal(all.find(item=>item.title==='Command').status,'open');
    // The browser authoring form feeds the same presentation fields as MCP.
    const tasks = await (await context.request.get(origin+'/api/projects/'+all[0].project_id+'/tickets')).json();
    const ready = tasks.find(item=>item.title==='Unfinished prerequisite');
    await page.goto(origin+'/?ticket='+ready.id);
    await page.getByRole('button',{name:'Ask a human',exact:true}).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('What do you need?',{exact:true}).fill('Pick a preview colour');
    await dialog.getByLabel('Answer choices (optional)',{exact:true}).fill('Blue\nGreen');
    await dialog.getByLabel('Command (optional)',{exact:true}).fill('printf preview\n');
    await dialog.getByText('Additional context',{exact:true}).click();
    await dialog.getByLabel('Details',{exact:true}).fill('Supporting context stays in Details.');
    await dialog.getByRole('button',{name:'Request input',exact:true}).click();
    await dialog.waitFor({state:'hidden'});
    const made = (await (await context.request.get(origin+'/api/human-requests')).json()).find(item=>item.ticket_id===ready.id);
    assert.equal(made.summary,'Pick a preview colour');
    assert.equal(made.question,'Pick a preview colour\n\nSupporting context stays in Details.');
    assert.equal(made.command,'printf preview\n');
    assert.deepEqual(made.choices,[{id:'A',label:'Blue'},{id:'B',label:'Green'}]);
    assert.deepEqual(errors,[]);
    const result={pass:true,browser:browserName,flows:['short prompt and collapsed exact details','A/B/C without default selection','explicit submission','retry keeps selected answer','custom Unicode reply','exact command clipboard and no completion','approval decline','legacy command','reload persistence','mobile choice layout','browser authoring form'],javascriptErrors:errors};
    fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify(result,null,2)+'\n');
    console.log(JSON.stringify(result));
  } finally {
    if(browser) await browser.close();
    if(server && server.exitCode===null){const stopped=new Promise(resolve=>server.once('exit',resolve));server.kill('SIGTERM');await stopped;}
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
