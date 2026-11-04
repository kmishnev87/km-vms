// Coalesce overlapping readers, not writes. Failure always releases admission.
export function createSingleFlight() {
  let pending = null;
  return {
    run(task) {
      if (pending) return pending;
      const request = Promise.resolve().then(task).finally(() => {
        if (pending === request) pending = null;
      });
      pending = request;
      return request;
    },
  };
}
