import React from 'react';
import {createRoot} from 'react-dom/client';
import {flushSync} from 'react-dom';
import {Renderer} from '@openuidev/react-lang';
import {Host, library, compile} from './library.jsx';

const mounts = new Map();
let busy=false;
class Boundary extends React.Component {
  state={error:null};
  static getDerivedStateFromError(error) {return {error:String(error)};}
  componentDidCatch(error) {this.props.report(String(error));}
  render() {return this.state.error ? <p role="alert">Interactive view unavailable. Use the text below.</p> : this.props.children;}
}
function defaults(document) {
  return document.type==='form' ? Object.fromEntries(document.fields.filter(f=>'default' in f).map(f=>[f.id,f.default])) : {};
}
function supported(block) {
  return block.protocol_version===1 && block.library_version==='blackrose-semantic/1' && block.schema_hash===__UI_SCHEMA_HASH__;
}
function render(record) {
  const b=record.block;
  const compatible=supported(b);
  const usable=compatible && b.document && ['ready','streaming'].includes(b.status);
  const disabled=b.status!=='ready'||busy||!b.actions_enabled||record.submitting;
  const reason=b.status!=='ready'?'Available when this answer finishes.':busy||record.submitting?'Wait for the current reply to finish.':!b.actions_enabled?'Follow-up actions are disabled in settings.':'';
  const host={block:b,state:record.state,effectsDisabled:disabled,disabledReason:reason,
    change:(key,value)=>{
      if(value===undefined) delete record.state[key]; else record.state[key]=value;
      record.dirty=true;
      clearTimeout(record.timer);
      record.timer=setTimeout(()=>flush(record).catch(error=>{record.error=String(error);render(record);}),250);
      render(record);
    },
    submit:async()=>{
      // `disabled` is captured at render time and React commits it later; the
      // record's own lock is what makes a second submit inside one commit a
      // no-op (a double click, or Enter twice before the button disables).
      if(disabled||record.submitting)return;
      record.submitting=true; record.error=''; render(record);
      try {
        await flush(record);
        const event_id=globalThis.crypto?.randomUUID?.() || `event_${Date.now()}_${Math.random().toString(36).slice(2)}_${Math.random().toString(36).slice(2)}`;
        const result=await record.callbacks.action({...identity(b),event_id,action_id:'submit',action_type:'continue_conversation',values:{...record.state}});
        if(!result?.ok) throw new Error(result?.error||'Could not submit choices');
      } catch(error) {record.error=String(error.message||error);}
      finally {record.submitting=false;if(mounts.has(b.block_id))render(record);}
    },
  };
  record.root.render(<div className="blackrose-ui" data-ui-status={compatible?b.status:'unsupported_version'}>
    {usable && <Boundary report={error=>{record.error=error;}}><Host.Provider value={host}><Renderer library={library} response={compile(b.document)} isStreaming={b.status==='streaming'} publishObservability={false}
      onError={errors=>{if(errors.length){record.error='The renderer reported an error. Use the text fallback.'; record.container.dataset.uiError='true';}}}/></Host.Provider></Boundary>}
    {!usable && <p role="status">{!compatible?'This answer needs a newer UI runtime.':b.status==='streaming'?'Preparing interactive answer…':`Interactive answer ${b.status}.`}</p>}
    {record.error && <p role="alert" className="ui-error">{record.error}</p>}
    <details className="ui-fallback" open={!usable}><summary>Text and source</summary><pre>{b.fallback_text||'No complete block was received.'}</pre>
      {b.error && <p className="ui-error">{b.error}</p>}
      <details><summary>Source JSON</summary><pre>{b.source}</pre></details>
      <button type="button" onClick={async()=>{
        if(record.callbacks.export){
          const result=await record.callbacks.export(identity(b));
          if(result?.error){record.error=result.error;render(record);}
          return;
        }
        const a=document.createElement('a'); const url=URL.createObjectURL(new Blob([b.source],{type:'application/json'}));
        a.href=url;a.download='interactive-answer.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
      }}>Export source</button></details>
  </div>);
}
function identity(b) {return {conversation_id:b.conversation_id,assistant_message_id:b.message_id,block_id:b.block_id,source_revision:b.source_revision,view_id:b.view_id};}
async function flush(record) {
  clearTimeout(record.timer);
  if(record.saving) await record.saving;
  if(!record.dirty || !['ready','streaming'].includes(record.block.status))return;
  record.dirty=false;
  const resultPromise=record.callbacks.state({...identity(record.block),state_sequence:record.sequence+1,values:{...record.state}});
  record.saving=Promise.resolve(resultPromise);
  try {
    const result=await record.saving;
    if(!result?.ok)throw new Error(result?.error||'Could not save UI state');
    record.sequence=result.state_sequence;
  } catch(error) {record.dirty=true;throw error;}
  finally {record.saving=null;}
}
export function mount(container, block, callbacks) {
  if(mounts.has(block.block_id)) {update(block.block_id,block);return;}
  const record={container,root:createRoot(container),block,callbacks,
    // A saved snapshot is complete, including fields deliberately cleared.
    state:block.state_sequence>0 ? {...block.saved_state} : {...defaults(supported(block)?block.document||{}:{}),...(block.saved_state||{})},
    stateInitialized:!!block.document||block.state_sequence>0,
    sequence:block.state_sequence||0,error:'',dirty:false};
  container.addEventListener('focusout',()=>flush(record).catch(error=>{record.error=String(error);render(record);}));
  mounts.set(block.block_id,record);render(record);
}
export function update(id, block) {
  const record=mounts.get(id);if(!record)return;
  if(block.source_sequence<record.block.source_sequence)return;
  record.block={...record.block,...block};
  if(!record.stateInitialized && supported(record.block) && record.block.document) {
    record.state={...defaults(record.block.document),...record.state};
    record.stateInitialized=true;
  }
  render(record);
  if(block.status==='ready'&&record.dirty)flush(record).catch(error=>{record.error=String(error);render(record);});
}
export function unmount(id) {
  const record=mounts.get(id);if(!record)return;
  clearTimeout(record.timer);record.root.unmount();mounts.delete(id);
}
export async function flushAll() {
  // Settle every save before navigation, even when another view has invalid edits.
  const results=await Promise.allSettled([...mounts.values()].map(async record=>{
    try {await flush(record);}
    catch(error) {record.error=String(error.message||error);if(mounts.has(record.block.block_id))render(record);throw error;}
  }));
  const failed=results.find(result=>result.status==='rejected');
  if(failed)throw failed.reason;
}
export function unmountAll() {for(const id of mounts.keys())unmount(id);}
export function setBusy(value) {busy=!!value;for(const record of mounts.values())render(record);}
export function inspect() {return [...mounts].map(([id,r])=>({id,state:r.state,sequence:r.sequence,status:r.block.status}));}
// Useful to deterministic probes; production mounting is otherwise asynchronous.
export function sync() {flushSync(()=>{});}
