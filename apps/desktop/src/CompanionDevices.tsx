import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Button } from "./ui";
import { api } from "./api";
type Attachment = { attachment_id: string; session_id: string; device_name: string; revoked: boolean; expires_at: string };
export function CompanionDevices({sessionId}:{sessionId:string|null}) {
 const [items,setItems]=useState<Attachment[]>([]),[error,setError]=useState<string|null>(null),[busy,setBusy]=useState(false);
 const load=async()=>{try{const result=await invoke<{items:Attachment[]}>("api_call",{method:"GET",path:"/companion/attachments",body:null});setItems(result.items.filter(a=>a.session_id===sessionId && !a.revoked && new Date(a.expires_at).getTime()>Date.now()));setError(null);}catch(e){setError(String(e));}};
 const revoke=async(id:string)=>{setBusy(true);try{await invoke("api_call",{method:"DELETE",path:`/companion/attachments/${id}`,body:null});await load();}catch(e){setError(String(e));}finally{setBusy(false);}};
 return <details onToggle={e=>{if(e.currentTarget.open)void load();}}><summary>Use your phone for guidance</summary><div className="ui-stack"><p>Sign in to CoRuby Mobile with this account, choose this profile, and open the active desktop call. Keep your call and headphones on this computer. The phone does not need microphone permission.</p><div className="ui-row-inline"><Button onClick={()=>void api.showOverlay(false).catch(e=>setError(String(e)))}>Hide desktop overlay</Button><Button onClick={()=>void api.showOverlay(true).catch(e=>setError(String(e)))}>Restore desktop overlay</Button><Button onClick={()=>void load()}>Refresh displays</Button></div>{items.map(a=><p key={a.attachment_id}>{a.device_name} · expires {new Date(a.expires_at).toLocaleTimeString()} <Button disabled={busy} onClick={()=>void revoke(a.attachment_id)}>Disconnect display</Button></p>)}{error&&<p role="alert">{error}</p>}<p className="ui-field-hint">Screen sharing depends on the selected app, window, and operating system. Check your sharing preview. Disconnecting a display keeps this call running.</p></div></details>;
}
