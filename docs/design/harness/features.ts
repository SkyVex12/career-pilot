// Deterministic fictional feature states for screenshot/accessibility verification.
const q = { id:"question-1",text:"Tell me about a time you traded latency for consistency.",kind:"experience",attempts:[],assistance:[],saved_for_later:false };
const practice = { id:"practice-1",status:"answering",setup:{topic:"Backend engineering"},questions:[q],current_question_id:q.id,elapsed_seconds:95,sources:[{id:"resume",label:"Current resume"}],spoken_language:"en",voice_available:false,speech_available:false,retention_notice:"Practice transcripts remain until deleted. Raw audio is never retained." };
const conversation = {id:"conversation-1",title:"Prepare for Acme",scope:{kind:"auto"},messages:[{id:"m1",request_id:"r1",question:"Which project best fits this role?",answer:"Your queue migration is relevant to the reliability requirements [S1]. Explain the delivery tradeoff and provide the measured outcome if available.",status:"done",sources:[{id:"source-1",kind:"document",label:"Queue migration notes",date:"2026-11-18"}]}]};
export function featureRest(method:string,path:string,_body?:unknown):unknown {
 const [p]=path.split("?");
 if(p==="/call-languages")return {items:[{id:"en",name:"English"}]};
 if(p==="/profile/call-preferences")return {spoken_language:"en"};
 if(p==="/ask/context-options")return {options:[{id:"app-1",label:"Acme · Backend engineer"}]};
 if(p==="/ask/conversations")return method==="GET"?{conversations:[conversation]}:conversation;
 if(p==="/ask/conversations/conversation-1")return conversation;
 if(p.startsWith("/ask/sources/"))return {content:"The team chose an outbox and idempotent queue processing. The measured impact still needs to be supplied."};
 if(p==="/practice/context")return {sources:[{id:"resume",label:"Current resume",excerpt:"Backend engineer with queue processing experience."}],notice:"Review selected profile and source records before starting."};
 if(p==="/practice/sessions")return method==="GET"?{sessions:[practice]}:practice;
 if(p==="/practice/sessions/practice-1")return practice;
 if(p==="/personal-ai/requests")return {requests:[]};
 if(p==="/calendars")return {items:[{provider:"google",available:false,connected:false,calendars:[],selected_calendars:[]},{provider:"outlook",available:false,connected:false,calendars:[],selected_calendars:[]}]};
 if(p==="/meeting-preparation")return {items:[],connections:[]};
 return undefined;
}
