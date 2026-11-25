"""Self-contained blind human review: local autosave, no network, JSON export."""

# ruff: noqa: E501 -- Self-contained HTML/JS template.

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from copilot_evals.sensei_runtime import private_directory


def review_html(review: dict[str, Any]) -> str:
    if review.get("reviewer_kind") or review.get("reviewer_id"):
        raise ValueError("Generate human review from an unreviewed blind set")
    if any(item.get("preference") is not None for item in review["items"]):
        raise ValueError("Human review must not include previous preferences")
    for item in review["items"]:
        for side in ("A", "B"):
            values = item[side]
            if any(score is not None for score in values.get("scores", {}).values()):
                raise ValueError("Human review must not include previous quality scores")
            if (
                any(
                    values.get(field) is not None
                    for field in (
                        "first_useful_sentence",
                        "first_useful_cue",
                        "first_useful_complete_sentence",
                        "redundant",
                        "meaningfully_adds",
                        "unnecessarily_verbose",
                        "incorrect",
                    )
                )
                or values.get("unit_grammar")
                or any(
                    values.get(field)
                    for field in (
                        "first_useful_assessed",
                        "first_useful_cue_assessed",
                        "first_useful_complete_sentence_assessed",
                    )
                )
            ):
                raise ValueError("Human review must not include previous judgments")
    encoded = json.dumps(review, ensure_ascii=True).replace("<", "\\u003c")
    encoded = encoded.replace(">", "\\u003e").replace("&", "\\u0026")
    digest = hashlib.sha256(encoded.encode()).hexdigest()[:20]
    return TEMPLATE.replace("__DATA__", encoded).replace("__IDENTITY__", digest)


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src data:; base-uri 'none'; form-action 'none'">
<title>Blind live-answer review</title>
<style>
:root{color-scheme:light;font:15px/1.5 system-ui,sans-serif;color:#172b35;background:#f4f6f7}
*{box-sizing:border-box}body{max-width:1440px;margin:auto;padding:20px}h1{font-size:24px;margin:0}
h2{font-size:20px;margin:12px 0}h3{font-size:18px;margin:0 0 10px}p{margin:8px 0}
header{display:flex;gap:20px;align-items:center;justify-content:space-between;margin-bottom:12px}
button,select,input,textarea{font:inherit;border:1px solid #a4b5be;border-radius:6px;padding:7px;background:white;color:inherit}
button{cursor:pointer}button:disabled{opacity:.45;cursor:default}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid #41a3bb;outline-offset:2px}
button[aria-pressed=true]{background:#075d70;color:#fff;border-color:#075d70}textarea{width:100%;min-height:70px}
.panel,article{background:#fff;border:1px solid #d0dce1;border-radius:10px;padding:16px;margin:12px 0}
.answers{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}.answers article{margin:0;min-width:0}
.text{white-space:pre-wrap;overflow-wrap:anywhere;margin:12px 0}.muted{color:#526671;font-size:13px}.warning{color:#8a341c}
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.preference{position:sticky;bottom:0;box-shadow:0 -4px 12px #172b3510}
.unit{display:block;text-align:left;width:100%;margin:5px 0}.unit span{font-size:12px;color:#63757f;margin-right:8px}
label{display:block;margin:6px 0}.scores{display:grid;grid-template-columns:1fr 1fr;gap:6px}.scores label{display:flex;gap:8px;align-items:center;justify-content:space-between}
summary{cursor:pointer;font-weight:600}progress{width:180px}pre{white-space:pre-wrap;font-size:13px;overflow-wrap:anywhere}
@media(max-width:780px){body{padding:10px}.answers{grid-template-columns:1fr}header{display:block}.scores{grid-template-columns:1fr}.preference{position:static}}
</style></head><body>
<header><div><h1>Blind live-answer review</h1><p class="muted">Human judgments • source evidence stays in this browser • no model labels or scores</p></div><div><progress id="progress" value="0" max="1"></progress><div id="count" class="muted"></div></div></header>
<details class="panel"><summary>How to review</summary><p>Compare the two answers using only the supplied question and evidence. Judge correctness, grounding, relevance, personalization, completeness and appropriate depth. Missing personal facts do not prove that an experience did not happen.</p><p>Review streaming boundary units in their visible order. A useful cue can be a phrase or bullet fragment. Separately select the first useful complete sentence: it must be grammatically complete and answer the question, possibly in the expansion. Punctuation alone does not prove completeness. Skip filler; leave unknown judgments unselected. Timing is hidden.</p><p>Where a separate expansion exists, judge whether it adds meaningful information, merely repeats, becomes verbose, or introduces errors. Flags may overlap. Optional quality scores run from 1 (poor) to 5 (excellent); leave unknown values unscored.</p><p>Use A / B / T / U for preference and ← / → to navigate when not typing. Review output is provisional until exported by a human reviewer. This page cannot provide human calibration automatically.</p></details>
<div class="panel"><div class="controls"><button id="prev">← Previous</button><button id="next">Next →</button><button id="unreviewed">Next unreviewed</button><label for="jump">Pair</label><select id="jump"></select><span id="category" class="muted"></span></div><h2 id="question"></h2><details><summary>Authorized source evidence</summary><pre id="evidence"></pre></details></div>
<div class="answers" id="answers"></div>
<div class="panel preference"><div class="controls" id="choices"></div><label for="notes">Notes: unsupported claims, important omissions, or why this preference</label><textarea id="notes"></textarea></div>
<div class="panel"><div class="controls"><label for="reviewer">Reviewer name or ID</label><input id="reviewer" autocomplete="off"><label><input type="checkbox" id="attest"> I am a human reviewer and made these judgments myself.</label><button id="export">Export judgments</button></div><p id="save-status" class="muted" role="status">Autosave stays in this browser; export a JSON file for analysis.</p></div>
<script type="application/json" id="source">__DATA__</script>
<script>
'use strict';
const source=JSON.parse(document.getElementById('source').textContent), storageKey='career-pilot-blind-__IDENTITY__';
let review=structuredClone(source), index=0;
const byId=id=>document.getElementById(id), flags=['redundant','meaningfully_adds','unnecessarily_verbose','incorrect'];
const labels={redundant:'Expansion repeats without adding value',meaningfully_adds:'Expansion adds meaningful information',unnecessarily_verbose:'Expansion is unnecessarily verbose',incorrect:'Expansion introduces or contains an error'};
function immutable(item){return JSON.stringify([item.question,item.category,item.evidence,...['A','B'].map(s=>[item[s].initial,item[s].expanded,item[s].sentences,item[s].boundary_units,item[s].has_expansion,item[s].delivery])]);}
try{const stored=JSON.parse(localStorage.getItem(storageKey));if(stored&&stored.review.items.length===source.items.length&&stored.review.items.every((v,i)=>v.pair_id===source.items[i].pair_id&&immutable(v)===immutable(source.items[i]))){review=stored.review;index=Math.max(0,Math.min(stored.index,review.items.length-1));byId('reviewer').value=stored.reviewer||'';}}catch{}
function save(){try{localStorage.setItem(storageKey,JSON.stringify({review,index,reviewer:byId('reviewer').value}));byId('save-status').textContent='Saved locally. Export JSON to submit judgments.';}catch{byId('save-status').textContent='Browser storage unavailable. Export JSON before closing this page.';}}
function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
function updateProgress(){const n=review.items.filter(i=>i.preference!==null).length;byId('progress').max=review.items.length;byId('progress').value=n;byId('count').textContent=n+' / '+review.items.length+' pairs have a preference';}
function selectValue(value,choices,onChange){const select=el('select');for(const [v,t] of choices){const option=el('option',t);option.value=v;select.append(option);}select.value=value===null?'':String(value);select.onchange=()=>{onChange(select.value);save();};return select;}
function renderSide(side,item){const value=item[side],card=el('article');card.append(el('h3','Answer '+side));if(value.response_preferences){card.append(el('p','Requested style: '+value.response_preferences.answer_style+'; length: '+value.response_preferences.response_length));}const delivery=value.delivery||{};
 if(!delivery.primary_complete)card.append(el('p',value.initial?'Primary stream incomplete; judge the visible partial answer.':'No primary answer was delivered.','warning'));
 card.append(el('div',value.initial||'(No answer)','text'));
 const units=el('details');units.open=true;units.append(el('summary','Streaming boundary units'));
 if(value.boundary_units){
  units.append(el('p','Select a useful cue separately from a useful complete sentence. The latter explicitly confirms grammatical completeness.','muted'));
  for(const unit of value.boundary_units){const block=el('div',undefined,'panel');block.append(el('div',(unit.pass==='deep'?'Expansion ':'Initial ')+unit.id.split(':')[1]+': '+unit.text,'text'));
   const controls=el('div',undefined,'controls');
   for(const [field,label] of [['first_useful_cue','First useful cue'],['first_useful_complete_sentence','First useful complete sentence']]){const b=el('button',label);b.setAttribute('aria-pressed',String(value[field]===unit.id));b.onclick=()=>{value[field]=unit.id;value[field+'_assessed']=true;if(field==='first_useful_complete_sentence'){value.unit_grammar??={};value.unit_grammar[unit.id]=true;}save();render();};controls.append(b);}
   const grammar=el('label','Grammar ');grammar.append(selectValue((value.unit_grammar||{})[unit.id]??null,[['','Unjudged'],['true','Complete sentence'],['false','Fragment / incomplete']],v=>{value.unit_grammar??={};value.unit_grammar[unit.id]=v===''?null:v==='true';if(v!=='true'&&value.first_useful_complete_sentence===unit.id){value.first_useful_complete_sentence=null;value.first_useful_complete_sentence_assessed=false;}render();}));controls.append(grammar);block.append(controls);units.append(block);
  }
  for(const [field,label] of [['first_useful_cue','No useful cue'],['first_useful_complete_sentence','No useful complete sentence']]){const b=el('button',label);b.setAttribute('aria-pressed',String(value[field+'_assessed']===true&&value[field]===null));b.onclick=()=>{value[field]=null;value[field+'_assessed']=true;save();render();};units.append(b);}
 }else{
  units.append(el('p','Legacy source: these primary units support cue review only; complete-sentence timing is unavailable.','warning'));
  value.sentences.forEach((text,i)=>{const b=el('button',(i+1)+'. '+text,'unit');b.setAttribute('aria-pressed',String(value.first_useful_sentence===i+1));b.onclick=()=>{value.first_useful_sentence=i+1;value.first_useful_assessed=true;save();render();};units.append(b);});
  const none=el('button','No useful cue');none.setAttribute('aria-pressed',String(value.first_useful_assessed===true&&value.first_useful_sentence===null));none.onclick=()=>{value.first_useful_sentence=null;value.first_useful_assessed=true;save();render();};units.append(none);
 }
 card.append(units);
 if(value.has_expansion){const expansion=el('details');expansion.append(el('summary','Separate expansion'));if(!delivery.expanded_complete)expansion.append(el('p','Expansion stream incomplete.','warning'));expansion.append(el('div',value.expanded,'text'));for(const flag of flags){const label=el('label',labels[flag]+' ');label.append(selectValue(value[flag],[['','Unjudged'],['true','Yes'],['false','No']],v=>value[flag]=v===''?null:v==='true'));expansion.append(label);}card.append(expansion);}
 const scores=el('details');scores.append(el('summary','Optional quality scores'));const grid=el('div',undefined,'scores');for(const criterion of review.rubric){const label=el('label',criterion.replaceAll('_',' ')+' ');label.append(selectValue(value.scores[criterion],[['','Unscored'],...Array.from({length:5},(_,i)=>[String(i+1),String(i+1)])],v=>value.scores[criterion]=v===''?null:Number(v)));grid.append(label);}scores.append(grid);card.append(scores);return card;}
function render(){const item=review.items[index];if(!item){byId('question').textContent='This review set contains no pairs.';return;}byId('question').textContent=item.question;byId('category').textContent=item.category;byId('evidence').textContent=JSON.stringify(item.evidence,null,2);byId('answers').replaceChildren(...['A','B'].map(s=>renderSide(s,item)));byId('notes').value=item.notes||'';byId('choices').replaceChildren(el('strong','More useful answer:'));
 for(const [v,label] of [['A','A'],['B','B'],['tie','Tie'],['uncertain','Uncertain'],[null,'Clear']]){const b=el('button',label);b.setAttribute('aria-pressed',String(item.preference===v));b.onclick=()=>{item.preference=v;save();render();};byId('choices').append(b);}byId('prev').disabled=index===0;byId('next').disabled=index===review.items.length-1;byId('jump').value=String(index);updateProgress();}
review.items.forEach((_,i)=>{const option=el('option',String(i+1));option.value=String(i);byId('jump').append(option);});
function move(n){index=Math.max(0,Math.min(review.items.length-1,n));save();render();}
byId('prev').onclick=()=>move(index-1);byId('next').onclick=()=>move(index+1);byId('jump').onchange=()=>move(Number(byId('jump').value));byId('unreviewed').onclick=()=>{for(let step=1;step<=review.items.length;step++){const candidate=(index+step)%review.items.length;if(review.items[candidate].preference===null){move(candidate);return;}}};
byId('notes').oninput=()=>{review.items[index].notes=byId('notes').value;save();};byId('reviewer').oninput=save;
document.addEventListener('keydown',event=>{if(['INPUT','TEXTAREA','SELECT'].includes(event.target.tagName)||event.ctrlKey||event.metaKey||event.altKey)return;if(event.key==='ArrowLeft')move(index-1);else if(event.key==='ArrowRight')move(index+1);else{const p={a:'A',b:'B',t:'tie',u:'uncertain'}[event.key.toLowerCase()];if(p){review.items[index].preference=p;save();render();}}});
byId('export').onclick=()=>{if(!byId('attest').checked||!byId('reviewer').value.trim()){byId('save-status').textContent='Enter a reviewer ID and confirm that these are your own human judgments before exporting.';return;}
 const output={...review,reviewer_kind:'human',reviewer_id:byId('reviewer').value.trim(),reviewed_at:new Date().toISOString()};const url=URL.createObjectURL(new Blob([JSON.stringify(output,null,2)],{type:'application/json'}));const link=el('a');link.href=url;link.download='human-review.json';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);byId('save-status').textContent='Exported human-review.json. Unjudged values remain null; no automatic quality scores were assigned.';};
render();
</script></body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    private_directory(args.out.resolve().parent)
    if args.out.exists():
        raise ValueError("Refusing to overwrite an existing human review")
    review = json.loads(args.review.read_text("utf-8"))
    args.out.write_text(review_html(review), encoding="utf-8")
    print(json.dumps({"status": "ready", "pairs": len(review["items"]), "human_reviews": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
