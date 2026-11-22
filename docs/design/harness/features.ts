// Deterministic fictional feature states for screenshot/accessibility verification.
import languages from "./languages.json";
const q = { id:"question-1",text:"Tell me about a time you traded latency for consistency.",kind:"experience",attempts:[],assistance:[],saved_for_later:false };
const practice = { id:"practice-1",status:"answering",setup:{topic:"Backend engineering"},questions:[q],current_question_id:q.id,elapsed_seconds:95,sources:[{id:"resume",label:"Current resume"}],spoken_language:"en",voice_available:false,speech_available:false,retention_notice:"Practice transcripts remain until deleted. Raw audio is never retained." };
const conversation = {id:"conversation-1",title:"Prepare for Acme",scope:{kind:"auto"},messages:[{id:"m1",request_id:"r1",question:"Which project best fits this role?",answer:"Your queue migration is relevant to the reliability requirements [S1]. Explain the delivery tradeoff and provide the measured outcome if available.",status:"done",sources:[{id:"source-1",kind:"document",label:"Queue migration notes",date:"2026-11-18"}]}]};
export function featureRest(method:string,path:string,_body?:unknown):unknown {
 const [p]=path.split("?");
 if(p==="/call-languages")return {items:languages};
 if(p==="/profile/call-preferences")return {spoken_language:"en"};
 if(p==="/ask/context-options")return {options:[{id:"app-1",label:"Acme · Backend engineer"}]};
 if(p==="/ask/conversations")return method==="GET"?{conversations:[conversation]}:conversation;
 if(p==="/ask/conversations/conversation-1")return conversation;
 if(p.startsWith("/ask/sources/"))return {content:"The team chose an outbox and idempotent queue processing. The measured impact still needs to be supplied."};
 if(p==="/practice/context")return {sources:[{id:"resume",label:"Current resume",excerpt:"Backend engineer with queue processing experience."}],notice:"Review selected profile and source records before starting."};
 if(p==="/practice/sessions")return method==="GET"?{sessions:[practice]}:practice;
 if(p==="/practice/sessions/practice-1")return practice;
 if(p==="/personal-ai/requests")return {requests:[]};
 if(p==="/personal-ai/oauth/connections")return {connections:[]};
 const calendarState = new URLSearchParams(location.search).get("calendar");
 const connected = calendarState === "connected" || calendarState === "reauth";
 if(p==="/calendars")return {items:[{provider:"google",available:connected,connected,status:calendarState === "reauth" ? "reauth_required" : connected ? "connected" : "disconnected",last_error:calendarState === "reauth" ? "Calendar permission expired. Sign in again." : undefined,calendars:connected?[{id:"calendar-1",name:"Interviews and work"}]:[],selected_calendars:connected?["calendar-1"]:[],last_sync_at:connected?new Date().toISOString():undefined},{provider:"outlook",available:connected,connected:false,status:"disconnected",calendars:[],selected_calendars:[]}]};
 if(p==="/meeting-preparation")return connected?{items:[{meeting:{account_id:"calendar-account-1",calendar_id:"calendar-1",instance_id:"meeting-1",title:"Northwind · Backend engineering interview",start:new Date(Date.now()+15*60000).toISOString(),end:new Date(Date.now()+60*60000).toISOString()},candidates:[{application_id:"app-1",company:"Northwind",job_title:"Senior backend engineer"}],selected_application_id:"app-1",requires_confirmation:true,overlaps:false,previous:[{notes:"Discuss the incident response ownership and request the team's reliability goals.",unfinished_actions:["Prepare the migration tradeoff example."]}],linked_session_id:"session-1"}],connections:[{connection_id:"calendar-account-1",provider:"google"}],saved_sessions:[{id:"session-1",title:"Northwind recruiter conversation",started_at:new Date(Date.now()-7*86400000).toISOString()}]}:{items:[],connections:[]};
 return undefined;
}
