// Clock ticks may replace the queued target, but must not invalidate an answer
// already in flight. Explicit seeks invalidate older generations immediately.
export function createCoalescedPlaybackResolver() {
  let generation = 0;
  let running = false;
  let pending = null;

  function invalidate() {
    generation += 1;
    pending?.finish({ applied: false });
    pending = null;
  }

  async function drain() {
    if (running) return;
    running = true;
    try {
      while (pending) {
        const task = pending;
        pending = null;
        try {
          const value = await task.run();
          const applied = task.generation === generation;
          if (applied) task.apply(value);
          task.finish({ applied });
        } catch (error) {
          task.fail(error);
        }
      }
    } finally {
      running = false;
    }
  }

  function resolve({ run, apply, automatic = false }) {
    if (!automatic) invalidate();
    pending?.finish({ applied: false });
    const result = new Promise((finish, fail) => {
      pending = { run, apply, finish, fail, generation };
    });
    void drain();
    return result;
  }
  return { resolve, invalidate };
}
