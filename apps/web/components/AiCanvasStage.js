"use client";

import { useEffect, useRef, useState } from "react";
import { contourAt, frameFit, framePoint, movePoint, pickedVertex, shiftContour } from "../lib/aiCanvasGeometry";

function Shape({ shape, size, active, focused, vertex, editable, r, nudge }) {
  const { points, line, direction, mask } = shape;
  if (!points.length) return null;
  const vertices = points.map(point=>point.join(",")).join(" ");
  const [a,b] = points;
  let arrows = null;
  if (line && b) {
    const x=(a[0]+b[0])/2, y=(a[1]+b[1])/2;
    const dx=(b[0]-a[0])*size.width/1000, dy=(b[1]-a[1])*size.height/1000;
    const scale=19/(Math.hypot(dx,dy)||1), sx=1000/size.width, sy=1000/size.height;
    const gx=-dy*scale*sx, gy=dx*scale*sy, ux=dx*scale*.38*sx, uy=dy*scale*.38*sy;
    const head=sign=>`${x+sign*gx*.5+ux},${y+sign*gy*.5+uy} ${x+sign*gx},${y+sign*gy} ${x+sign*gx*.5-ux},${y+sign*gy*.5-uy}`;
    arrows=<>{direction!=="reverse" ? <polyline points={head(1)} /> : null}
      {direction!=="forward" ? <polyline points={head(-1)} /> : null}</>;
  }
  return <g className={["aiCanvasShape",active?"is-active":focused?"is-context":"",mask?"is-mask":""].join(" ")}
    style={{"--shape-ink":shape.color}} data-shape={shape.id}>
    {line || points.length<3 ? <polyline points={vertices} /> : <polygon points={vertices} />}
    {arrows}
    {active ? points.map(([x,y],i)=><ellipse key={i} cx={x} cy={y}
      rx={(vertex===i?6.5:5)*1000/size.width} ry={(vertex===i?6.5:5)*1000/size.height}
      className={vertex===i?"aiCanvasHandle is-picked":"aiCanvasHandle"} data-vertex={i} tabIndex={editable?0:undefined}
      role={editable?"button":undefined} aria-label={r("work_vertex",{n:i+1})}
      onFocus={()=>editable && nudge(i,null)} onKeyDown={e=>editable && nudge(i,e)} />) : null}
  </g>;
}

export default function AiCanvasStage({ frame, shapes, active, choose, tool, canEdit, pointLimit, picked, pick, change, centre, toggleFull, r }) {
  const app = useRef(null);
  const held = useRef(null);
  const pending=useRef(null);
  const commitRef=useRef(null);
  const [space,setSpace]=useState(null);
  const [preview,setPreview]=useState(null);
  const [adding,setAdding]=useState(null);
  const [cursor,setCursor]=useState([500,500]);
  const shape=shapes.find(item=>item.id===active);
  const savedPoints=shape?.points;
  const points=savedPoints || [];
  const shown=preview||adding;
  commitRef.current=(next)=>{change(next);pick(next.length-1)};
  const size=frame && space ? frameFit(space.width,space.height,frame.width,frame.height) : null;
  useEffect(()=>{
    const slot=app.current;
    if(!slot) return;
    const observer=new ResizeObserver(([entry])=>setSpace({width:entry.contentRect.width,height:entry.contentRect.height}));
    observer.observe(slot);
    return ()=>observer.disconnect();
  },[]);
  useEffect(()=>{
    held.current=null;setPreview(null);setAdding(null);
    return ()=>{clearTimeout(pending.current?.timer);pending.current=null;};
  },[active,frame,tool,canEdit,savedPoints]);

  function finishAdd(commit) {
    const work=pending.current;
    if(!work)return;
    clearTimeout(work.timer);pending.current=null;setAdding(null);
    if(commit)commitRef.current?.(work.next);
  }

  function stageAdd(event,d) {
    const point=framePoint(d.rect,event.clientX,event.clientY);
    if(event.pointerType!=="mouse") {
      change([...d.points,point]);pick(d.points.length);return;
    }
    const old=pending.current;
    if(old && Math.hypot(old.x-event.clientX,old.y-event.clientY)<8)return;
    const next=[...(old?.next||d.points),point].slice(0,pointLimit);
    clearTimeout(old?.timer);
    // Show the vertex immediately; don't commit a click that the browser turns into a double-click.
    pending.current={next,previous:old?.next,x:event.clientX,y:event.clientY,timer:setTimeout(()=>finishAdd(true),475)};
    setAdding(next);
  }

  function doubleClick(event) {
    if(!toggleFull)return;
    const work=pending.current;
    if(work && Math.hypot(work.x-event.clientX,work.y-event.clientY)<10) {
      finishAdd(false);
      if(work.previous)commitRef.current?.(work.previous);
    } else finishAdd(true);
    event.preventDefault();
    toggleFull();
  }

  function begin(event) {
    if(held.current || !event.isPrimary || event.button!==0) return;
    const rect=event.currentTarget.getBoundingClientRect();
    if(rect.width<=0 || rect.height<=0) return;
    const point=framePoint(rect,event.clientX,event.clientY);
    const radius=event.pointerType==="touch"?24:13;
    if(!canEdit || !shape) {
      if(choose) {
        const at=[...shapes].reverse();
        const edge=at.find(s=>pickedVertex(s.points,point,rect,radius)>=0 ||
          contourAt(s.line || s.points.length<3?s.points:[...s.points,s.points[0]],point,rect,radius,false));
        const selected=edge || at.find(s=>!s.line&&s.points.length>=3&&contourAt(s.points,point,rect,0,true));
        if(selected)choose(selected.id);
      }
      return;
    }
    const vertex=tool==="move" ? -1 : pickedVertex(points,point,rect,radius);
    const inside=contourAt(points,point,rect,radius,!shape.line&&points.length>=3);
    const action=vertex>=0?"point": tool==="move"&&inside?"move":tool==="add"&&points.length<pointLimit?"add": inside?"move":"";
    if (!action) { pick(null); return; }
    held.current={id:event.pointerId,action,vertex,point,rect,points,startX:event.clientX,startY:event.clientY};
    pick(vertex>=0?vertex:null);
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  }
  function move(event) {
    const d=held.current;
    if(!d || event.pointerId!==d.id || d.action==="add") return;
    const p=framePoint(d.rect,event.clientX,event.clientY);
    if(d.action==="point") {
      const old=d.points[d.vertex];
      d.next=movePoint(d.points,d.vertex,[old[0]+p[0]-d.point[0],old[1]+p[1]-d.point[1]]);
    } else d.next=shiftContour(d.points,p[0]-d.point[0],p[1]-d.point[1]);
    setPreview(d.next);
  }
  function finish(event,commit) {
    const d=held.current;
    if(!d || event.pointerId!==d.id) return;
    held.current=null;
    setPreview(null);
    if(!commit) return;
    if(d.action==="add"&&Math.hypot(event.clientX-d.startX,event.clientY-d.startY)<9) {
      stageAdd(event,d);
    } else if(d.next && JSON.stringify(d.next)!==JSON.stringify(d.points)) change(d.next);
  }
  function keyboard(index,event) {
    if(!canEdit || !shape) return;
    if(!event) {pick(index);return;}
    const vectors={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]};
    const vector=vectors[event.key];
    if(vector) {
      event.preventDefault();
      event.stopPropagation();
      const [dx,dy]=vector.map(x=>x*(event.shiftKey?14:4));
      if(index!==null) {
        const [x,y]=points[index];
        pick(index);change(movePoint(points,index,[x+dx,y+dy]));
      } else if(tool==="move") change(shiftContour(points,dx,dy));
      else setCursor(shiftContour([cursor],dx,dy)[0]);
    } else if(index===null && tool==="add" && points.length<pointLimit && ["Enter"," "].includes(event.key)) {
      event.preventDefault();
      change([...points,cursor]);pick(points.length);
    }
  }

  return <div className="aiCanvasMat" ref={app}>
    {!frame ? <div className="aiCanvasEmpty">{centre}</div> : null}
    {frame && size ? <div className="aiCanvasImage" style={size} data-ai-frame onDoubleClick={doubleClick}>
      <img src={"data:image/jpeg;base64,"+frame.image} alt="" draggable={false}/>
      <svg aria-label={r("work_canvas")} tabIndex={canEdit?0:-1} viewBox="0 0 1000 1000" preserveAspectRatio="none"
        className={canEdit?"aiCanvasOverlay can-edit tool-"+tool:"aiCanvasOverlay"+(choose?" can-select":"")}
        onPointerDown={begin} onPointerMove={move} onPointerUp={event=>finish(event,true)}
        onPointerCancel={event=>finish(event,false)} onLostPointerCapture={event=>finish(event,false)}
        onKeyDown={e=>keyboard(null,e)}>
        {shapes.map(s=><Shape key={s.id} shape={s.id===active&&shown ? {...s,points:shown} : s} size={size}
          active={s.id===active} focused={Boolean(active)} vertex={picked} editable={canEdit} nudge={keyboard} r={r} />)}
        {canEdit&&tool==="add" ? <ellipse className="aiCanvasKeyCursor" cx={cursor[0]} cy={cursor[1]}
          rx={5*1000/size.width} ry={5*1000/size.height}/> : null}
      </svg>
      {shapes.map(s=>{
        const points=s.id===active&&shown?shown:s.points;
        if(!points.length)return null;
        const [x,y]=points[0];
        return <span key={s.id} aria-hidden="true" className={"aiCanvasBadge"+(active&&s.id!==active?" is-context":"")}
          style={{left:Math.max(1,Math.min(97,x/10))+"%",top:Math.max(2,Math.min(96,y/10))+"%","--shape-ink":s.color}}>{s.mark}</span>;
      })}
    </div> : null}
  </div>;
}
