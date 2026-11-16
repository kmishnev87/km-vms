"use client";
import { useEffect, useId, useRef, useState } from "react";
import { partitionArchiveCameras } from "../lib/archiveCameraGroups";

export default function ArchiveCameraSelect({ items, value, onChange, allLabel, deletedLabel, retainedLabel }) {
  const [open, setOpen] = useState(false);
  const root = useRef(null), trigger = useRef(null), menu = useRef(null);
  const id = useId();
  const groups = partitionArchiveCameras(items);
  const selected = items.find(item => item.value === value);
  const label = selected ? `${selected.name}${selected.deleted ? ` (${deletedLabel})` : ""}` : allLabel;
  useEffect(() => {
    if (!open) return undefined;
    const close = event => { if (!root.current?.contains(event.target)) setOpen(false); };
    document.addEventListener("pointerdown", close);
    menu.current?.querySelector('[aria-selected="true"]')?.focus();
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);
  function choose(next) { onChange(next); setOpen(false); trigger.current?.focus(); }
  function keys(event) {
    const options = [...menu.current.querySelectorAll('[role="option"]')];
    const index = options.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1
      : event.key === "ArrowDown" ? (index + 1) % options.length : event.key === "ArrowUp" ? (index + options.length - 1) % options.length : null;
    if (next !== null) { event.preventDefault(); options[next]?.focus(); }
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus(); }
  }
  function option(item) {
    return <button role="option" aria-selected={value === item.value} type="button" key={item.value} title={item.name}
      className="archiveCameraOption" onClick={() => choose(item.value)}>
      <span>{item.name}</span>{item.deleted ? <small>({deletedLabel})</small> : null}
    </button>;
  }
  return <div className="archiveCameraSelect" ref={root} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}>
    <button type="button" ref={trigger} className="select recordingsFilterSelect archiveCameraTrigger" title={label}
      aria-label={label} aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? id : undefined}
      onClick={() => setOpen(previous => !previous)} onKeyDown={event => { if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); setOpen(true); } }}>
      <span>{label}</span><svg className="archiveCameraChevron" aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="m6 9 6 6 6-6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/></svg>
    </button>
    {open ? <div className="archiveCameraMenu" role="listbox" aria-label={allLabel} id={id} ref={menu} onKeyDown={keys}>
      {option({ value: "__all__", name: allLabel })}{groups.active.map(option)}
      {groups.archived.length ? <><div className="archiveCameraDivider" role="separator">{retainedLabel}</div>{groups.archived.map(option)}</> : null}
    </div> : null}
  </div>;
}
