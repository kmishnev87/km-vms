"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../../lib/api";
import { useI18n } from "../../lib/i18n";
import { DISCOVERY_COPY, DISCOVERY_TERMINAL, discoveryError, discoveryPorts, discoveryProgress, discoveryRanges } from "../../lib/cameraDiscovery";
import { OperationDialog } from "../OperationFeedback";

const post = (path, body, extra = {}) => apiFetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), ...extra });

export function CameraSearchIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12.5 18H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h2l1.5-2h5L15 6h3a2 2 0 0 1 2 2v2" /><circle cx="10.5" cy="11.5" r="3" /><circle cx="17" cy="16.5" r="3.5" /><path d="m19.5 19 2 2" /></svg>;
}

export default function CameraDiscoveryDialog({ protocol, onSelect, onClose }) {
  const { locale } = useI18n();
  const copy = DISCOVERY_COPY[locale] || DISCOVERY_COPY.en;
  const [networks, setNetworks] = useState([]);
  const [selected, setSelected] = useState([]);
  const [manual, setManual] = useState("");
  const [local, setLocal] = useState(true);
  const [onvifPorts, setOnvifPorts] = useState("80, 443, 2020, 8000, 8080, 8899");
  const [rtspPorts, setRtspPorts] = useState("554, 8554");
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [job, setJob] = useState(null);
  const alive = useRef(true);
  const jobId = useRef(null);
  const running = Boolean(job?.job_id && !DISCOVERY_TERMINAL.includes(job.status));
  const busy = starting || running;

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      if (jobId.current) post(`/cameras/discovery/jobs/${jobId.current}/cancel`, {}, { keepalive: true }).catch(() => {});
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    apiFetch("/cameras/discovery/networks", { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      setNetworks(result.networks || []);
      setSelected((result.networks || []).filter(row => row.kind === "local" && row.selectable).map(row => row.cidr));
    }).catch(failure => { if (!controller.signal.aborted) setError(discoveryError(failure, copy)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!running) return undefined;
    let disposed = false;
    let timer;
    let failures = 0;
    const began = Date.now();
    const controller = new AbortController();
    async function poll() {
      if (Date.now() - began > 180000) {
        post(`/cameras/discovery/jobs/${job.job_id}/cancel`, {}, { keepalive: true }).catch(() => {});
        setError(copy.failed); setJob(previous => ({ ...previous, status: "partial" }));
        return;
      }
      try {
        const next = await apiFetch(`/cameras/discovery/jobs/${job.job_id}`, { signal: controller.signal });
        if (disposed) return;
        failures = 0; setError(""); setJob(next);
        if (!DISCOVERY_TERMINAL.includes(next.status)) timer = window.setTimeout(poll, 900);
      } catch (failure) {
        if (disposed) return;
        if ([401, 403, 404].includes(failure?.status)) {
          setError(copy.failed); setJob(previous => ({ ...previous, status: "failed" }));
        } else {
          failures += 1;
          if (failures >= 2) setError(copy.statusLost);
          timer = window.setTimeout(poll, 2500);
        }
      }
    }
    timer = window.setTimeout(poll, 300);
    return () => { disposed = true; controller.abort(); window.clearTimeout(timer); };
  }, [job?.job_id, running]);

  async function search() {
    const onvif = discoveryPorts(onvifPorts), rtsp = discoveryPorts(rtspPorts);
    if (!onvif || !rtsp) { setError(copy.invalid); return; }
    setStarting(true); setError("");
    try {
      const next = await post("/cameras/discovery/jobs", { local, networks: discoveryRanges(selected, manual), protocols: ["onvif", "rtsp"], onvif_ports: onvif, rtsp_ports: rtsp });
      if (!alive.current) { post(`/cameras/discovery/jobs/${next.job_id}/cancel`, {}, { keepalive: true }).catch(() => {}); return; }
      jobId.current = next.job_id; setJob(next);
    } catch (failure) { if (alive.current) setError(discoveryError(failure, copy)); }
    finally { if (alive.current) setStarting(false); }
  }
  async function cancel() {
    if (!jobId.current) return;
    setJob(previous => ({ ...previous, cancel_requested: true }));
    try {
      const next = await post(`/cameras/discovery/jobs/${jobId.current}/cancel`, {});
      if (alive.current) setJob(next);
    } catch (failure) { if (alive.current) setError(discoveryError(failure, copy)); }
  }
  const progress = discoveryProgress(job);
  const rows = job?.candidates || [];
  const resultSummary = job?.status === "completed" && rows.length
    ? `${job.elapsed_seconds} ${copy.seconds}`
    : `${copy.found}: ${rows.length}${job ? ` · ${job.elapsed_seconds} ${copy.seconds}` : ""}`;
  const statusText = loading ? copy.loading : starting || running ? job?.cancel_requested ? copy.cancelling : job?.phase === "addresses" ? copy.addressPhase : copy.localPhase
    : job?.status === "cancelled" ? copy.cancelled : job?.status === "partial" ? copy.partial : job?.status === "failed" ? copy.failed
      : job ? rows.length ? `${copy.found}: ${rows.length}` : copy.empty : copy.ready;

  return <OperationDialog dialog={{ id: "camera-network-discovery", title: copy.title, closeLabel: copy.close,
    tone: "neutral", className: "cameraDiscoveryDialog", showFooterClose: false,
    content: <div className="cameraDiscoveryEditor">
      <p className="cameraDiscoveryHelp">{copy.help}</p>
      <fieldset className="cameraDiscoveryNetworks" disabled={busy || loading}>
        <legend>{copy.networks}</legend>
        <label className="cameraDiscoveryLocal"><input type="checkbox" checked={local} onChange={event => setLocal(event.target.checked)} />{copy.local}</label>
        <div className="cameraDiscoveryNetworkOptions">{networks.map(row => <label key={`${row.cidr}:${row.interface}`}>
          <input type="checkbox" checked={selected.includes(row.cidr)} disabled={!row.selectable} onChange={event => setSelected(previous => event.target.checked ? [...previous, row.cidr] : previous.filter(cidr => cidr !== row.cidr))} />
          <span><strong>{row.cidr}</strong><small>{row.kind === "local" ? copy.lan : copy.routed} · {row.interface}{!row.selectable ? ` · ${copy.large}` : ""}</small></span>
        </label>)}</div>
        <label className="cameraDiscoveryManual">{copy.manual}<input className="input" value={manual} onChange={event => setManual(event.target.value)} placeholder="192.168.1.0/24" spellCheck={false} /><small>{copy.manualHint}</small></label>
        <details className="cameraDiscoveryPorts"><summary>{copy.advanced}</summary><div>
          <label>{copy.onvifPorts}<input className="input" value={onvifPorts} onChange={event => setOnvifPorts(event.target.value)} /></label>
          <label>{copy.rtspPorts}<input className="input" value={rtspPorts} onChange={event => setRtspPorts(event.target.value)} /></label>
        </div></details>
      </fieldset>
      <div className="cameraDiscoveryStatus" role="status" aria-live="polite"><span>{statusText}</span><span>{resultSummary}</span>
        {job?.total_hosts ? <><progress max="100" value={progress ?? 0} aria-label={copy.checked} /><small>{copy.checked}: {job.checked_hosts} / {job.total_hosts}</small></> : running ? <progress aria-label={statusText} /> : null}
      </div>
      {error ? <p className="cameraDiscoveryError" role="alert">{error}</p> : null}
      <div className="cameraDiscoveryResults">
        {rows.map(row => <div className="cameraDiscoveryResult" key={row.id}>
          <div><strong>{row.name || row.host}</strong>{row.name ? <span>{row.host}</span> : null}<small>{(row.protocols || []).map(value => value.toUpperCase()).join(" · ")}{row.auth_required ? ` · ${copy.auth}` : ""}</small></div>
          <div className="cameraDiscoveryChoose">
            {[protocol, ...["onvif", "rtsp"].filter(value => value !== protocol)].filter(value => row.protocols?.includes(value)).map(value => <button key={value} type="button" className="button secondary small" disabled={starting || job?.cancel_requested && running} title={value === "rtsp" ? copy.rtspHint : copy.select} onClick={() => onSelect({ ...row, selected_protocol: value })}>{value === protocol ? `${copy.select} ${value.toUpperCase()}` : value.toUpperCase()}</button>)}
          </div>
        </div>)}
      </div>
      <footer className="cameraDiscoveryActions"><small>{copy.known}</small>{busy ? <button className="button secondary small" type="button" disabled={starting || job?.cancel_requested} onClick={cancel}>{job?.cancel_requested ? copy.cancelling : copy.cancel}</button> : <button className="button secondary small cameraNetworkSearchButton" type="button" disabled={loading} onClick={search}><CameraSearchIcon />{copy.search}</button>}</footer>
    </div>,
  }} onClose={onClose} />;
}
