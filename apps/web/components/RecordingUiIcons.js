export function RecordingUiIcon({name}) {
  const paths = {
    gear: <><path d="m10 3.2 4 .1.6 2.3 1.6.9 2.3-.6 2 3.4-1.7 1.7v1.8l1.7 1.7-2 3.4-2.3-.6-1.6.9-.6 2.3h-4l-.6-2.3-1.6-.9-2.3.6-2-3.4 1.7-1.7v-1.8L3.5 9.3l2-3.4 2.3.6 1.6-.9z"/><circle cx="12" cy="12" r="3.1"/></>,
    cancel: <path d="m6 6 12 12M18 6 6 18"/>,
    info: <><circle cx="12" cy="12" r="8.6"/><path d="M12 8v5m0 3v.1"/></>,
    eraser: <><path d="m13.2 4.7 5.9 5.9a1.5 1.5 0 0 1 0 2.2l-6.9 7H7.4L3.7 16a1.5 1.5 0 0 1 0-2.2l7.3-9.1a1.5 1.5 0 0 1 2.2 0Z"/><path d="m7.1 10 7.4 7.4M11.8 19.8h9"/></>,
    frame: <><path d="M5 5h14a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Z"/><path d="m4 16 5-5 4 4 3-3 4 4"/><circle cx="15.5" cy="9" r=".7"/></>,
    expand: <path d="M9 4H4v5m11-5h5v5M4 15v5h5m11-5v5h-5"/>,
    collapse: <path d="M4 9h5V4m11 5h-5V4M9 20v-5H4m11 5v-5h5"/>,
    back: <path d="m11 5-7 7 7 7M5 12h15"/>,
    point: <><path d="M12 3v4m0 10v4M3 12h4m10 0h4"/><circle cx="12" cy="12" r="3"/></>,
    move: <path d="M12 3v18M3 12h18m-12-6 3-3 3 3M9 18l3 3 3-3m-9-9-3 3 3 3m12-6 3 3-3 3"/>,
    undo: <path d="M9 5 4 10l5 5m-5-5h10a5 5 0 0 1 5 5v2"/>,
    redo: <path d="m15 5 5 5-5 5m5-5H10a5 5 0 0 0-5 5v2"/>,
    cut: <path d="M5 12h14"/>,
  };
  return <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false"
    fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}
