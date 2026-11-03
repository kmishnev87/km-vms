// Memory only: never persist a user's diagnostic snapshot across sign-ins.
export const RUNTIME_STATUS_CACHE_MAX_AGE_MS = 90000;

export function createRuntimeStatusCache(now = () => Date.now()) {
  let entry = null;
  return {
    read(identity, session) {
      if (!identity || !session || entry?.identity !== identity || entry?.session !== session) return null;
      if (!entry.denied && now() - entry.updatedAt > RUNTIME_STATUS_CACHE_MAX_AGE_MS) return null;
      return entry;
    },
    write(identity, session, runtimeStatus, { stale = false, denied = false } = {}) {
      if (!identity || !session) return;
      entry = { identity, session, runtimeStatus: denied ? null : runtimeStatus, stale, denied, updatedAt: now() };
    },
  };
}

export const runtimeStatusCache = createRuntimeStatusCache();
