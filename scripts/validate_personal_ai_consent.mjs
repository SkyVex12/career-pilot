import { createRequire } from 'node:module';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import assert from 'node:assert/strict';
const require = createRequire(resolve(process.env.PW_DIR, 'package.json'));
const { chromium } = require('playwright-core');
const origin = process.env.FIXTURE_ORIGIN;
const control = process.env.FIXTURE_CONTROL_TOKEN;
const out = resolve('docs/design/screens/personal-ai-consent');
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const context = await browser.newContext({ viewport: { width: 900, height: 950 } });
const page = await context.newPage();
const errors = [], violations = [], observations = [];
page.on('pageerror', e => errors.push(String(e)));
page.on('console', m => { if (/Content Security Policy|Refused to execute|violates.*directive/i.test(m.text())) errors.push(m.text()); });
page.on('request', request => {
  if (request.url().endsWith('/oauth/consent')) {
    observations.push(request.allHeaders().then(headers => ({origin:headers.origin, bearer:headers.authorization?.startsWith('Bearer ') === true, body:JSON.parse(request.postData())})));
  }
});
const verifier='a'.repeat(43);
const challenge=createHash('sha256').update(verifier).digest('base64url');
async function audit(name) {
  await page.evaluate(readFileSync('apps/desktop/node_modules/axe-core/axe.min.js','utf8'));
  const result=await page.evaluate(async()=> (await window.axe.run(document)).violations.map(v=>({id:v.id,impact:v.impact,targets:v.nodes.map(n=>n.target)})));
  violations.push({name,result});
  await page.screenshot({path:resolve(out,name+'.png'),fullPage:true});
}
async function signin(name) {
  const email='consent-'+randomUUID()+'@example.com';
  const query=new URLSearchParams({client_id:'browser-fixture',redirect_uri:origin+'/callback',response_type:'code',code_challenge:challenge,code_challenge_method:'S256',resource:origin+'/v1/personal-ai/mcp',scope:'context:read context:publish',state:name});
  const response=await page.goto(origin+'/v1/personal-ai/oauth/authorize?'+query);
  assert(response.headers()['content-security-policy'].includes("script-src 'nonce-"));
  await page.getByLabel('Email',{exact:true}).fill(email);
  await page.getByRole('button',{name:'Send sign-in code'}).click();
  await page.getByText('Check your email for the sign-in code.').waitFor();
  await page.getByLabel('Code',{exact:true}).fill('bad');
  await page.getByRole('button',{name:'Verify email'}).click();
  await page.getByText('That code is not right.').waitFor();
  const mail=await fetch(origin+'/_fixture/mail',{method:'POST',headers:{'Authorization':'Bearer '+control,'Content-Type':'application/json'},body:JSON.stringify({email})});
  assert.equal(mail.status,200);
  await page.getByLabel('Code',{exact:true}).fill((await mail.json()).code);
  await page.getByRole('button',{name:'Verify email'}).click();
  await page.getByText('Signed in as '+email).waitFor();
  await audit(name+'-review');
}
try {
  await signin('allow');
  await page.getByRole('button',{name:'Allow these permissions'}).click();
  await page.waitForURL(url=>url.pathname==='/callback');
  let callback=new URL(page.url());
  assert.equal(callback.searchParams.get('state'),'allow');
  assert.equal(callback.searchParams.get('iss'),origin);
  assert(callback.searchParams.get('code'));
  const token=await fetch(origin+'/v1/personal-ai/oauth/token',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({grant_type:'authorization_code',client_id:'browser-fixture',redirect_uri:origin+'/callback',resource:origin+'/v1/personal-ai/mcp',code:callback.searchParams.get('code'),code_verifier:verifier})});
  assert.equal(token.status,200);
  const access=(await token.json()).access_token;
  const tools=await fetch(origin+'/v1/personal-ai/mcp',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+access},body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/list'})});
  assert.equal((await tools.json()).result.tools.length,2);
  await fetch(origin+'/v1/personal-ai/oauth/revoke',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({token:access,client_id:'browser-fixture'})});
  await signin('deny');
  await page.setViewportSize({width:390,height:844});
  await audit('deny-mobile');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.getByRole('button',{name:'Deny connection'}).click();
  await page.waitForURL(url=>url.pathname==='/callback');
  callback=new URL(page.url());
  assert.equal(callback.searchParams.get('state'),'deny');
  assert.equal(callback.searchParams.get('error'),'access_denied');
  assert.equal(callback.searchParams.has('code'),false);
  const observedRequests = await Promise.all(observations);
  assert.equal(observedRequests.length,2);
  for(const observed of observedRequests) {assert.equal(observed.origin,origin);assert.equal(observed.bearer,true);assert.match(observed.body.handle,/^[A-Za-z0-9_-]{43}$/);}
  assert.equal(observedRequests[0].body.approve,true);assert.equal(observedRequests[1].body.approve,false);
  assert.deepEqual(errors,[]);
  assert(violations.every(a=>a.result.length===0),JSON.stringify(violations));
  const result={status:'passed',allow:true,deny:true,invalid_code:true,origin_and_bearer:true,nonce_csp_errors:errors,axe:violations,narrow_overflow:false,fictional_accounts_only:true};
  writeFileSync(resolve(out,'validation.json'),JSON.stringify(result,null,2)+'\n');
  console.log(JSON.stringify(result));
} finally {await browser.close();}
