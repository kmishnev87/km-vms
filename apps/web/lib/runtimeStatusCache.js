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

export function createRuntimeStatusRequestCache(now = () => Date.now()) {
  let current = null;
  return (session, fetcher, { force = false } = {}) => {
    if (!session) return Promise.reject(Object.assign(new Error('Sign in required'), { status: 401 }));
    if (current?.session !== session) current = { session, promise: null, data: null, updatedAt: 0 };
    const owner = current;
    if (owner.promise) return owner.promise;
    if (!force && owner.data && now() - owner.updatedAt < 30000) return Promise.resolve(owner.data);
    const request = Promise.resolve(fetcher()).then(data => {
      if (current === owner) { owner.data = data; owner.updatedAt = now(); }
      return data;
    }).finally(() => { if (owner.promise === request) owner.promise = null; });
    owner.promise = request;
    return request;
  };
}

export const runtimeStatusRequestCache = createRuntimeStatusRequestCache();
