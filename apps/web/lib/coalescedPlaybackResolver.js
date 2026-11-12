// Clock refreshes coalesce. User seeks cancel obsolete I/O immediately.
export function createCoalescedPlaybackResolver() {
  let generation = 0;
  let active = null;
  let pending = null;

  function invalidate() {
    generation += 1;
    pending?.finish({ applied: false });
    pending = null;
    if (active) {
      const previous = active;
      active = null;
      previous.controller.abort();
      previous.finish({ applied: false });
    }
  }

  function start(task) {
    active = task;
    let result;
    try { result = task.run(task.controller.signal); }
    catch (error) { result = Promise.reject(error); }
    Promise.resolve(result).then(value => {
      const applied = active === task && task.generation === generation && !task.controller.signal.aborted;
      if (applied) task.apply(value);
      task.finish({ applied });
    }).catch(error => {
      if (task.controller.signal.aborted || task.generation !== generation) task.finish({ applied: false });
      else task.fail(error);
    }).finally(() => {
      if (active !== task) return;
      active = null;
      if (pending) { const next = pending; pending = null; start(next); }
    });
  }

  function resolve({ run, apply, automatic = false }) {
    if (!automatic) invalidate();
    pending?.finish({ applied: false });
    const result = new Promise((finish, fail) => {
      const task = { run, apply, finish, fail, generation, controller: new AbortController() };
      if (active) pending = task;
      else start(task);
    });
    return result;
  }
  return { resolve, invalidate };
}
