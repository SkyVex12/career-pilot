import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { Button } from "./ui";
export type CallSuggestion = { id:string; platform:string; kind:"start"|"end" };
/** Dedicated local window: deliberately has no account, API, audio or session hooks. */
export function BackgroundSuggestion() {
  const [suggestion,setSuggestion]=useState<CallSuggestion|null>(null);
  const [error,setError]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);
  useEffect(()=>{
    let alive=true; let revision=0;
    const load=async()=>{const current=++revision;try{const value=await invoke<CallSuggestion|null>("background_suggestion");if(alive && current===revision)setSuggestion(value);}catch(e){if(alive && current===revision)setError(String(e));}};
    const listener=listen<CallSuggestion|null>("copilot://call-suggestion",()=>{void load();});
    void listener.then(()=>{if(alive)void load();}).catch(e=>{if(alive)setError(String(e));});
    return ()=>{alive=false;void listener.then(stop=>stop());};
  },[]);
  const respond=async(accept:boolean)=>{
    if(!suggestion)return;setBusy(true);setError(null);
    try{await invoke("background_respond",{id:suggestion.id,accept});setSuggestion(null);}
    catch(e){setError(String(e));}finally{setBusy(false);}
  };
  return <main className="ui-stack" style={{padding:16}}>{suggestion && <><strong>{suggestion.kind==="end" ? "Call may have ended" : "Call detected"}</strong><p>{suggestion.kind==="end" ? "Review whether to end Copilot." : "Start Copilot for live assistance."}</p><div className="ui-row-inline"><Button disabled={busy} onClick={()=>void respond(true)}>{suggestion.kind==="end" ? "Review end call" : "Start Copilot"}</Button><Button disabled={busy} onClick={()=>void respond(false)}>Not now</Button></div></>}{error && <p role="alert">{error}</p>}</main>;
}
