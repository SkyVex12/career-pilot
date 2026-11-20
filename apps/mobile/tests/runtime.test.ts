import test from "node:test";
import assert from "node:assert/strict";
import { Api, type Tokens } from "../src/api.ts";
import { withDeadline, ConnectionGate } from "../src/requestScope.ts";
import { event, emptyLive, command } from "../src/liveState.ts";
const tokens = {access_token:"access",refresh_token:"refresh",expires_in:3600,email:"test@example.invalid"};
function store() { let value: Tokens | null = null; return {read:async()=>value,write:async(t:Tokens|null)=>{value=t;}}; }
test("request deadline settles a transport that ignores abort instead of leaving UI busy", async () => {
  let signal: AbortSignal | undefined;
  const api = new Api("https://example.invalid", store(), async (_u, options) => { signal=options?.signal as AbortSignal; return new Promise(()=>{}); }, false, false, 15);
  await assert.rejects(()=>api.request("/auth/config",undefined,"GET",false), {name:"TimeoutError"});
  assert.equal(signal?.aborted,true);
});
test("cancellation settles while a token refresh is pending and prevents the later mutation", async () => {
  let finish!: (r:Response)=>void; const paths:string[]=[];
  const api = new Api("https://example.invalid",store(),async u=>{paths.push(String(u));return new Promise(r=>{finish=r;});});
  await api.accept({...tokens,expires_in:0});
  const controller=new AbortController(); const request=api.request("/companion/sessions/session/attach",{},"POST",true,controller.signal);
  await new Promise(r=>setTimeout(r,0)); controller.abort();
  await assert.rejects(()=>request,{name:"AbortError"});
  finish(new Response(JSON.stringify(tokens))); await new Promise(r=>setTimeout(r,0));
  assert.equal(paths.length,1); assert(paths[0].endsWith("/auth/token/refresh"));
});
test("new account does not share an old account's outstanding refresh", async () => {
  let finish!: (r:Response)=>void; let calls=0;
  const api=new Api("https://example.invalid",store(),async()=>{calls++;if(calls===1)return new Promise(r=>{finish=r;});return new Response(JSON.stringify({...tokens,access_token:"new-account"}));});
  await api.accept({...tokens,expires_in:0}); const old=api.token().catch(e=>e);
  await new Promise(r=>setTimeout(r,0)); await api.accept({...tokens,refresh_token:"new-refresh",expires_in:0});
  assert.equal(await withDeadline(()=>api.token(),100),"new-account");
  finish(new Response(JSON.stringify(tokens))); assert((await old) instanceof Error);
  assert.equal(await api.token(),"new-account"); assert.equal(calls,2);
});
test("connection gate blocks duplicate token waits and invalidates late foreground continuations",()=>{
  const gate=new ConnectionGate(); const old=gate.begin()!; assert.equal(gate.begin(),null);
  gate.invalidate(); const current=gate.begin()!; assert.equal(gate.current(old),false);
  gate.finish(old); assert.equal(gate.begin(),null); gate.finish(current); assert.notEqual(gate.begin(),null);
});
test("folded snapshot insertion order cannot discard other questions or merged answers",()=>{
  const result=event(emptyLive(),{type:"companion.snapshot",seq:12,messages:[
    {type:"question.detected",seq:1,question_id:"q1",text:"First"},
    {type:"answer.delta",seq:11,question_id:"q1",pass:"deep",text:"Complete first answer"},
    {type:"question.detected",seq:5,question_id:"q2",text:"Second"},
    {type:"answer.delta",seq:8,question_id:"q2",pass:"fast",text:"Complete second answer"},
    {type:"answer.done",seq:12,question_id:"q1",pass:"deep"},
  ]});
  assert.equal(result.items.length,2); assert.equal(result.items.find(q=>q.id==="q1")?.deep,"Complete first answer");
  assert.equal(result.items.find(q=>q.id==="q2")?.fast,"Complete second answer");
  assert.equal(event(result,{type:"answer.delta",seq:8,question_id:"q2",text:"duplicate"}),result);
});
test("malformed or nested snapshots fail before publishing invalid React text",()=>{
  for(const message of [{type:"companion.snapshot",messages:{}},{type:"companion.snapshot",messages:[{type:"companion.snapshot",messages:[]}]},{type:"question.detected",question_id:"x",text:{private:"bad"}},{type:"answer.delta",seq:NaN}]) assert.throws(()=>event(emptyLive(),message as never));
});
test("oversized companion followups fail locally before sending invalid server commands",()=>{
  assert.throws(()=>command("id","followup","x".repeat(4001)),/4000/);
});
