// A transport for the existing TilePlayer, not a second workspace/player UI.
// It owns every socket, object URL, event listener and timer it creates.
export function createLiveMsePlayer({ video, cameraId, stream, requestToken, isCurrent = () => true,
  onPlaying = () => {}, onAudio = () => {}, onFailure = () => {},
  MediaSourceClass = globalThis.MediaSource, WebSocketClass = globalThis.WebSocket,
  location = globalThis.location, urls = globalThis.URL, schedule = setTimeout, unschedule = clearTimeout }) {
  let disposed = false, generation = 0, attempts = 0, healthySince = 0;
  let socket = null, media = null, buffer = null, objectUrl = null, mime = null;
  let queue = [], queuedBytes = 0, tokenTimer = null, retryTimer = null, startupTimer = null;
  let removeListeners = [];
  const alive = own => !disposed && isCurrent() && generation === own;
  const clearTimer = value => { if (value != null) unschedule(value); };

  function cleanupConnection() {
    generation++;
    clearTimer(tokenTimer); clearTimer(startupTimer);
    tokenTimer = startupTimer = null;
    for (const remove of removeListeners) remove();
    removeListeners = [];
    if (socket) { socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null; socket.close(); }
    socket = null;
    if (video.getAttribute("src") === objectUrl) {
      video.pause(); video.removeAttribute("src"); video.load();
    }
    if (objectUrl) urls.revokeObjectURL(objectUrl);
    objectUrl = media = buffer = mime = null;
    queue = []; queuedBytes = 0;
    video.playbackRate = 1;
  }

  function fail(code, terminal = false, status = null) {
    if (disposed) return;
    cleanupConnection();
    if (!isCurrent()) return;
    if (!terminal && attempts++ < 2) {
      retryTimer = schedule(() => { retryTimer = null; void connect(); }, attempts * 1000);
    } else {
      disposed = true;
      onFailure(Object.assign(new Error(code), { code, status }));
    }
  }

  function listen(target, event, listener) {
    target.addEventListener(event, listener);
    removeListeners.push(() => target.removeEventListener(event, listener));
  }

  function pump(own) {
    if (!alive(own) || !buffer || buffer.updating) return;
    try {
      const ranges = video.buffered;
      if (ranges.length) {
        const end = ranges.end(ranges.length - 1), start = ranges.start(ranges.length - 1);
        const behind = end - video.currentTime;
        if (video.currentTime < start || behind > .85) video.currentTime = Math.max(start, end - .2);
        video.playbackRate = behind > .4 && behind <= .85 ? 1.05 : 1;
        if (healthySince && Date.now() - healthySince > 30000) attempts = 0;
        if (video.currentTime - ranges.start(0) > 10) {
          buffer.remove(0, video.currentTime - 5);
          return;
        }
      }
      if (queue.length) {
        const next = queue.shift(); queuedBytes -= next.byteLength;
        buffer.appendBuffer(next);
      }
    } catch (_) { fail("mse_buffer_failed"); }
  }

  function prepareBuffer(own) {
    if (!alive(own) || buffer || !mime || media?.readyState !== "open") return;
    try {
      if (!MediaSourceClass.isTypeSupported(mime)) { fail("mse_codec_unsupported", true); return; }
      buffer = media.addSourceBuffer(mime);
      listen(buffer, "updateend", () => pump(own));
      listen(buffer, "error", () => { if (alive(own)) fail("mse_buffer_failed"); });
      media.duration = Infinity;
      pump(own);
    } catch (_) { fail("mse_codec_unsupported", true); }
  }

  async function renew(own, expiresIn) {
    clearTimer(tokenTimer);
    tokenTimer = schedule(async () => {
      try {
        const info = await requestToken(cameraId, stream);
        if (!alive(own)) return;
        if (!info.mediaToken || socket?.readyState !== 1) throw new Error("mse_token_unavailable");
        socket.send(JSON.stringify({ type: "refresh", media_token: info.mediaToken }));
        void renew(own, info.expiresIn);
      } catch (error) {
        if (alive(own)) fail("mse_access_unavailable", [401, 403].includes(error?.status), error?.status);
      }
    }, Math.max(1000, Number(expiresIn || 300) * 1000 - 90000));
  }

  async function connect() {
    if (disposed || !isCurrent()) return;
    cleanupConnection();
    const own = generation;
    if (!MediaSourceClass || !WebSocketClass) { fail("mse_browser_unsupported", true); return; }
    try {
      const info = await requestToken(cameraId, stream);
      if (!alive(own)) return;
      if (!info.mediaToken) throw new Error("mse_token_unavailable");
      media = new MediaSourceClass();
      objectUrl = urls.createObjectURL(media);
      listen(media, "sourceopen", () => prepareBuffer(own));
      listen(video, "playing", () => {
        if (!alive(own)) return;
        clearTimer(startupTimer); startupTimer = null;
        healthySince ||= Date.now();
        onPlaying();
      });
      listen(video, "error", () => { if (alive(own)) fail("mse_video_failed"); });
      video.src = objectUrl;
      const endpoint = new URL(`/api/live/${encodeURIComponent(cameraId)}/${encodeURIComponent(stream)}/mse`, location.href);
      endpoint.protocol = location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocketClass(endpoint.href);
      socket.binaryType = "arraybuffer";
      socket.onopen = () => { if (alive(own)) void renew(own, info.expiresIn); };
      socket.onmessage = event => {
        if (!alive(own)) return;
        if (typeof event.data === "string") {
          let message;
          try { message = JSON.parse(event.data); } catch (_) { fail("mse_invalid_data", true); return; }
          if (message.type === "error") {
            const denied = message.code === "mse_access_denied";
            fail(denied ? message.code : "mse_stream_failed", denied, denied ? 403 : null);
          } else if (message.type === "init" && typeof message.mime === "string" && message.mime.length < 256) {
            mime = message.mime;
            onAudio(message);
            prepareBuffer(own);
          }
          return;
        }
        if (!(event.data instanceof ArrayBuffer)) { fail("mse_invalid_data", true); return; }
        queuedBytes += event.data.byteLength;
        if (queue.length >= 16 || queuedBytes > 16 * 1024 * 1024) { fail("mse_slow_viewer"); return; }
        queue.push(event.data);
        pump(own);
        video.play().catch(() => {});
      };
      socket.onclose = () => { if (alive(own)) fail("mse_connection_closed"); };
      socket.onerror = () => { if (alive(own)) fail("mse_connection_failed"); };
      startupTimer = schedule(() => { if (alive(own)) fail("mse_startup_timeout"); }, 15000);
    } catch (error) {
      if (alive(own)) fail("mse_connection_failed", [401, 403, 404].includes(error?.status), error?.status);
    }
  }

  return {
    start: connect,
    destroy: () => { disposed = true; clearTimer(retryTimer); retryTimer = null; cleanupConnection(); },
  };
}
