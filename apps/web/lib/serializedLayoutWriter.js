// A workspace has one serialized write queue for its session, even across page mounts.
// Session keys stay in memory only and are removed when no lease or write remains.
const workspaceQueues = new Map();

export function createSerializedLayoutWriter(write, { scope, ownerSession } = {}) {
  const key = scope && ownerSession ? JSON.stringify([ownerSession, scope]) : null;
  let queue = key ? workspaceQueues.get(key) : null;
  if (!queue) {
    queue = { revision: 0, running: false, pending: null, leases: 0, waiters: [] };
    if (key) workspaceQueues.set(key, queue);
  }
  queue.leases += 1;
  let disposed = false;

  function collect() {
    if (!queue.running && !queue.pending && !queue.leases && key && workspaceQueues.get(key) === queue) {
      workspaceQueues.delete(key);
    }
  }
  function invalidate() {
    if (disposed) return;
    queue.revision += 1;
    queue.pending = null;
  }
  function dispose() {
    if (disposed) return;
    disposed = true;
    queue.leases -= 1;
    collect();
  }
  async function drain() {
    if (queue.running) return;
    queue.running = true;
    try {
      while (queue.pending) {
        const task = queue.pending;
        queue.pending = null;
        if (!task.isCurrentSession()) continue;
        try {
          const result = await task.write(task.payload);
          if (task.isActive() && task.revision === queue.revision && task.isCurrentSession()) task.onSaved(result);
        } catch (error) {
          if (task.isActive() && task.revision === queue.revision && task.isCurrentSession()) task.onError(error);
        }
      }
    } finally {
      queue.running = false;
      const waiters = queue.waiters.splice(0);
      for (const resolve of waiters) resolve();
      collect();
    }
  }
  function save(payload, onSaved, onError, isCurrentSession = () => true) {
    if (disposed) return;
    queue.revision += 1;
    queue.pending = { payload, write, onSaved, onError, isCurrentSession, revision: queue.revision, isActive: () => !disposed };
    void drain();
  }
  function flush() {
    return queue.running || queue.pending ? new Promise(resolve => queue.waiters.push(resolve)) : Promise.resolve();
  }
  return { save, invalidate, dispose, flush };
}
