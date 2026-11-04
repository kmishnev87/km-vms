// One write at a time: the server cannot commit A after B. A response may
// update the UI only while it still owns the user's latest local revision.
export function createSerializedLayoutWriter(write) {
  let revision = 0;
  let running = false;
  let pending = null;
  let disposed = false;
  function invalidate() { revision += 1; pending = null; }
  function dispose() { disposed = true; }
  async function drain() {
    if (running) return;
    running = true;
    try {
      while (pending) {
        const task = pending;
        pending = null;
        if (!task.isCurrentSession()) continue;
        try {
          const result = await write(task.payload);
          if (!disposed && task.revision === revision && task.isCurrentSession()) task.onSaved(result);
        } catch (error) {
          if (!disposed && task.revision === revision && task.isCurrentSession()) task.onError(error);
        }
      }
    } finally { running = false; }
  }
  function save(payload, onSaved, onError, isCurrentSession = () => true) {
    revision += 1;
    pending = { payload, onSaved, onError, isCurrentSession, revision };
    void drain();
  }
  return { save, invalidate, dispose };
}
