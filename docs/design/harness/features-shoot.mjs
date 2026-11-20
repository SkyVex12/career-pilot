import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
const require=createRequire(resolve(process.env.PW_DIR ?? process.cwd(),'package.json'));
const {chromium}=require('playwright-core');
const browser=await chromium.launch({channel:'msedge',headless:true});
const page=await browser.newPage({viewport:{width:920,height:720}});
const out=resolve('docs/design/screens/features');mkdirSync(out,{recursive:true});
const results=[];const errors=[];page.on('pageerror',e=>errors.push(String(e)));
await page.goto('http://127.0.0.1:1430/');
await page.getByRole('navigation',{name:'Main'}).waitFor({timeout:10000}).catch(async e=>{console.log(JSON.stringify({errors,body:await page.locator('body').innerText()}));await page.screenshot({path:resolve(out,'load-failure.png')});await browser.close();throw e;});
for(const name of ['Ask','Practice']){
 await page.getByRole('navigation',{name:'Main'}).getByRole('button',{name:name==='Ask'?'Work context':'Interview prep',exact:true}).click();
 await page.getByRole('button',{name:name==='Ask'?'Ask a question':'Practice interview',exact:true}).first().click();
 if(name==='Ask'){await page.screenshot({path:resolve(out,'ask-empty.png'),fullPage:true});await page.getByRole('combobox',{name:'Saved conversations'}).click();await page.getByRole('option',{name:'Prepare for Acme'}).click();await page.getByText('Which project best fits this role?',{exact:true}).waitFor();}
 if(name==='Practice'){await page.screenshot({path:resolve(out,'practice-setup.png'),fullPage:true});await page.getByRole('combobox',{name:'Practice history'}).click();await page.getByRole('option',{name:/Backend engineering/}).click();await page.getByRole('heading',{name:/Tell me about/}).waitFor();}
 await page.addScriptTag({content:readFileSync('apps/desktop/node_modules/axe-core/axe.min.js','utf8')});
 const audit=await page.evaluate(async()=>{const r=await window.axe.run(document,{rules:{'color-contrast':{enabled:true}}});return r.violations.map(x=>({id:x.id,impact:x.impact,nodes:x.nodes.map(n=>n.target)}));});
 await page.screenshot({path:resolve(out,`${name.replaceAll(' ','-').toLowerCase()}.png`),fullPage:true});
 results.push({name,violations:audit,overflow:await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth)});
}
await page.setViewportSize({width:430,height:760});await page.screenshot({path:resolve(out,'practice-narrow.png'),fullPage:true});
results.push({name:'Practice narrow',overflow:await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth)});
writeFileSync(resolve(out,'metrics.json'),JSON.stringify({results,errors},null,2));console.log(JSON.stringify({results,errors}));await browser.close();
