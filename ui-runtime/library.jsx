import React, {createContext, useContext, useLayoutEffect, useRef} from 'react';
import {createLibrary, defineComponent} from '@openuidev/react-lang';
import {z} from 'zod/v4';

export const Host = createContext(null);
const h = React.createElement;
const valueText = value => typeof value === 'boolean' ? (value ? 'Yes' : 'No') : String(value);
const heading = d => <h3>{d.title}</h3>;
function Text({d}) { return <section>{heading(d)}<p className="ui-text">{d.text}</p></section>; }
function Form({d}) {
  const host = useContext(Host);
  const values = host.state;
  return <form onSubmit={e => {e.preventDefault(); host.submit();}}>
    {heading(d)}
    {d.fields.map(f => {
      const id = `ui-${host.block.block_id}-${f.id}`;
      const value = values[f.id] ?? (f.kind === 'checkbox' ? false : '');
      const common = {id, name:f.id, required:!!f.required,
        onChange:e => host.change(f.id, f.kind === 'checkbox' ? e.target.checked :
          f.kind === 'number' ? (e.target.value === '' ? undefined : Number(e.target.value)) :
          f.kind === 'select' && e.target.value === '' ? undefined : e.target.value)};
      return <div className="ui-field" key={f.id}>
        <label htmlFor={id}>{f.label}{f.required ? ' *' : ''}</label>
        {f.kind === 'select' ? <select {...common} value={value}><option value="" disabled={!!f.required}>{f.required ? 'Choose…' : 'Not specified'}</option>{f.options.map(o=><option key={o}>{o}</option>)}</select> :
          f.kind === 'checkbox' ? <input {...common} type="checkbox" checked={!!value}/> :
          <input {...common} type={f.kind} value={value} min={f.minimum} max={f.maximum} maxLength={4000} step={f.kind === 'number' ? 'any' : undefined}/>}
      </div>;
    })}
    <button type="submit" disabled={host.effectsDisabled}>{d.submit_label}</button>
    <p className="ui-hint">{host.effectsDisabled ? host.disabledReason : 'Sends these choices as a new message.'}</p>
  </form>;
}
function Comparison({d}) {
  const host = useContext(Host);
  // Old snapshots with a sort column retain their ascending order. New views
  // start in source order; filtering and sorting never change row identity.
  const {query='', sort='', direction='ascending', selected} = host.state;
  const rows = d.rows.map((row,index)=>({row,index})).filter(({row})=>Object.values(row).some(v=>valueText(v).toLocaleLowerCase().includes(query.toLocaleLowerCase())));
  if(sort)rows.sort((a,b)=>{
    const order=typeof a.row[sort] === 'number' && typeof b.row[sort] === 'number' ? a.row[sort]-b.row[sort] : valueText(a.row[sort]).localeCompare(valueText(b.row[sort]));
    return (direction==='descending'?-order:order) || a.index-b.index;
  });
  const selectionVisible = rows.some(({index})=>index===selected);
  const selectionHidden = selected != null && !selectionVisible;
  return <section>{heading(d)}<div className="ui-toolbar">
    <label>Filter rows<input type="search" value={query} maxLength={4000} onChange={e=>host.change('query',e.target.value)}/></label>
    <label>Sort by<select aria-label="Sort by" value={sort} onChange={e=>host.change('sort',e.target.value || undefined)}><option value="">Source order</option>{d.columns.map(c=><option key={c}>{c}</option>)}</select></label>
    <label>Sort direction<select aria-label="Sort direction" value={direction} disabled={!sort} onChange={e=>host.change('direction',e.target.value)}><option value="ascending">Ascending</option><option value="descending">Descending</option></select></label>
  </div><div className="ui-table-scroll" tabIndex="0" role="region" aria-label={d.title}><table>
    <thead><tr>{d.submit_label && <th scope="col">Choose</th>}{d.columns.map(c=><th scope="col" key={c} aria-sort={sort===c?direction:undefined}>{c}</th>)}</tr></thead>
    <tbody>{rows.map(({row,index})=><tr key={index}>{d.submit_label && <td><input type="radio" name={`select-${host.block.block_id}`} checked={selected===index} aria-label={`Select ${valueText(row[d.columns[0]])}`} onChange={()=>host.change('selected',index)}/></td>}{d.columns.map(c=><td key={c}>{valueText(row[c])}</td>)}</tr>)}</tbody>
  </table></div><p className="ui-hint">{rows.length} of {d.rows.length} rows · {sort?`${sort}, ${direction}`:'Source order'}</p>
  {d.submit_label && <><button disabled={host.effectsDisabled || !selectionVisible} onClick={()=>{if(selectionVisible)host.submit();}}>{d.submit_label}</button><p className="ui-hint" role={selectionHidden?'status':undefined}>{selectionHidden ? 'The selected row is hidden by the filter. Clear the filter or select a visible row to continue.' : host.effectsDisabled ? host.disabledReason : 'Sends the selected row as a new message.'}</p></>}
  </section>;
}
function Steps({d}) {
  const host = useContext(Host), step=host.state.step ?? 0;
  const title=useRef(null), navigating=useRef(false);
  useLayoutEffect(()=>{
    if(navigating.current) {navigating.current=false;title.current?.focus();}
  },[step]);
  const navigate=next=>{navigating.current=true;host.change('step',next);};
  return <section>{heading(d)}<p className="ui-hint">Step {step+1} of {d.items.length}</p><h4 ref={title} tabIndex={-1} aria-label={`Step ${step+1} of ${d.items.length}: ${d.items[step].title}`}>{d.items[step].title}</h4><p className="ui-text">{d.items[step].text}</p>
    <div className="ui-toolbar"><button disabled={step===0} onClick={()=>navigate(step-1)}>Previous</button><button disabled={step===d.items.length-1} onClick={()=>navigate(step+1)}>Next</button></div></section>;
}
// Keep ordinary values compact without rounding small measurements to zero.
const format = value => Number.isFinite(value) ? value.toLocaleString(undefined,
  value !== 0 && Math.abs(value) < 0.0001 ? {notation:'scientific',maximumSignificantDigits:6} : {maximumFractionDigits:4}) : 'Unavailable';
function Calculation({d}) {
  const host = useContext(Host), scale=host.state.scale === undefined ? 1 : host.state.scale;
  // Missing quantities preserve legacy snapshots; explicit nulls preserve blanks.
  const quantities=host.state.quantities ?? d.items.map(item=>item.quantity);
  const complete=scale!==null && quantities.every(quantity=>quantity!==null);
  const subtotal=i=>scale===null || quantities[i]===null ? NaN : quantities[i]*d.items[i].unit_price*scale;
  const total=complete ? d.items.reduce((sum,_,i)=>sum+subtotal(i),0) : NaN;
  const edit=(event,save)=>{
    const input=event.target;
    if(input.validity.valid)save(input.value==='' ? null : Number(input.value));
  };
  return <section>{heading(d)}<label className="ui-field">Quantity multiplier<input type="number" min="0" max="1000" step="any" value={scale ?? ''} onChange={e=>edit(e,value=>host.change('scale',value))}/></label>
    <p className="ui-hint">Edit each quantity independently. The multiplier scales every quantity; 1 keeps the quantities as entered.</p>
    <div className="ui-table-scroll" tabIndex="0" role="region" aria-label={d.title}><table><thead><tr><th scope="col">Item</th><th scope="col">Quantity</th><th scope="col">Unit value</th><th scope="col">Subtotal</th></tr></thead><tbody>{d.items.map((item,i)=><tr key={i}><th scope="row">{item.label}</th><td><input className="ui-quantity" aria-label={`Quantity for ${item.label}`} type="number" min="0" max="1000000" step="any" value={quantities[i] ?? ''} onChange={e=>edit(e,value=>host.change('quantities',quantities.map((quantity,index)=>index===i?value:quantity)))}/>{scale!==1 && <span className="ui-hint ui-scaled-quantity">Scaled: {format(scale===null || quantities[i]===null ? NaN : quantities[i]*scale)}</span>}</td><td>{format(item.unit_price)} {d.unit}</td><td>{format(subtotal(i))} {d.unit}</td></tr>)}</tbody></table></div>
    <p className="ui-total"><span>Total</span><output aria-live="polite">{format(total)} {d.unit}</output></p><p className="ui-hint">{complete ? 'Calculated locally from the quantities, multiplier and supplied unit values.' : 'Enter every quantity and the multiplier to calculate the total. Blank fields are not zero.'}</p></section>;
}
function Metrics({d}) {
  const values=d.data.map(item=>item.value), sum=values.reduce((a,b)=>a+b,0);
  const aggregate=d.aggregate==='count'?values.length:d.aggregate==='average'?sum/values.length:sum;
  const max=Math.max(...values.map(Math.abs)) || 1;
  // Normalize first so the domain remains usable for subnormal measurements.
  const negative=Math.max(0,...values.map(value=>-value/max));
  const positive=Math.max(0,...values.map(value=>value/max));
  const range=negative+positive || 1, zero=negative/range*100;
  return <section>{heading(d)}<p className="ui-total"><span>{d.aggregate}</span><output>{format(aggregate)} {d.aggregate==='count'?'items':d.unit}</output></p>
    <ul className="ui-bars">{d.data.map((item,i)=>{
      const width=Math.abs(item.value)/max/range*100;
      return <li key={i}><span>{item.label}</span><span className="ui-bar-track" aria-hidden="true" data-sign={item.value<0?'negative':item.value>0?'positive':'zero'} style={{'--ui-zero':`${zero}%`}}><span style={{left:`${item.value<0?zero-width:zero}%`,width:`${width}%`}}/></span><span>{format(item.value)} {d.unit}</span></li>;
    })}</ul><p className="ui-hint">Calculated from the supplied dataset. Bars share a zero baseline: negative values extend left, positive values right; dots mark zero. Lengths compare magnitudes.</p></section>;
}
export const renderers = {text:Text,form:Form,comparison:Comparison,steps:Steps,calculation:Calculation,metrics:Metrics};
export const names = Object.fromEntries(Object.keys(renderers).map(name=>[name,name[0].toUpperCase()+name.slice(1)]));
export const library = createLibrary({root:'Answer',components:[
  defineComponent({name:'Answer',description:'Trusted vertical composition',props:z.object({children:z.array(z.any())}),component:({props,renderNode})=><div>{renderNode(props.children)}</div>}),
  ...Object.entries(renderers).map(([kind,Component])=>defineComponent({name:names[kind],description:`Trusted semantic ${kind} view`,props:z.object({content:z.any()}),component:({props})=>h(Component,{d:props.content})})),
]});
export function compile(document) {
  if (!names[document.type]) throw new Error('Unsupported UI component');
  // Only JSON literals are embedded, never model-authored language or code.
  return `root = Answer([${names[document.type]}(${JSON.stringify(document)})])`;
}
