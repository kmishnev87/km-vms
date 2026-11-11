// One gesture owns its renewals; short presses finish in FIFO order.
export function createPtzController({ send, makeId, isCurrent = () => true, onChange = () => {}, onError = () => {},
  schedule = setTimeout, unschedule = clearTimeout, now = Date.now }) {
  let active = null;
  let disposed = false;
  let presetPending = false;
  let steps = [], currentStep = null, pressedStep = null;

  const current = (gesture) => !disposed && isCurrent() && active === gesture;
  const stopPayload = (gesture) => ({ action: "stop", dry_run: false,
    ...(gesture ? { control_id: gesture.id, sequence: ++gesture.sequence } : {}) });

  function clearSteps() {
    if (currentStep?.pulseTimer != null) unschedule(currentStep.pulseTimer);
    steps = []; currentStep = pressedStep = null;
  }

  async function stopGesture(force = false) {
    const gesture = active;
    active = null;
    if (gesture?.timer) unschedule(gesture.timer);
    onChange(null);
    if ((!gesture && !force) || !isCurrent()) return false;
    try {
      const result = await send(stopPayload(gesture), { keepalive: true });
      if (gesture) gesture.stopAcknowledged = true;
      return result?.camera_stopped === true || result?.ignored_stale_control === true;
    } catch (error) {
      if (gesture) gesture.stopFailed = true;
      if (!disposed && isCurrent()) onError(error);
      return false;
    }
  }

  function stop(force = false) {
    clearSteps();
    return stopGesture(force);
  }

  async function tick(gesture) {
    if (!current(gesture)) return;
    try {
      const result = await send({ action: gesture.action, direction: gesture.direction, speed: gesture.speed,
        duration_seconds: 1.5, dry_run: false, hold: true, control_id: gesture.id, sequence: ++gesture.sequence });
      if (!current(gesture)) {
        if (!gesture.stopAcknowledged && isCurrent()) {
          await send(stopPayload(gesture), { keepalive: true }).catch(() => {});
        }
        return result;
      }
      if (result?.ignored_stale_control) {
        active = null;
        onChange(null);
        return result;
      }
      if (!result?.executed) throw new Error("ptz_not_executed");
      const lease = Number(result.lease_seconds);
      const delay = result.movement_mode === "continuous"
        ? Math.max(150, Math.min(600, (Number.isFinite(lease) && lease > 0 ? lease : 1.5) * 400))
        : 300;
      gesture.timer = schedule(() => tick(gesture), delay);
      return result;
    } catch (error) {
      if (current(gesture)) {
        await stop();
        if (!disposed && isCurrent()) onError(error);
      }
      return null;
    }
  }

  function startGesture(action, direction, speed) {
    if (disposed || !isCurrent() || presetPending) return;
    void stopGesture();
    const gesture = { id: makeId(), sequence: 0, action, direction, speed, timer: null, stopAcknowledged: false };
    active = gesture;
    onChange({ action, direction });
    gesture.initial = tick(gesture);
    return gesture;
  }

  function start(action, direction, speed) {
    clearSteps();
    return startGesture(action, direction, speed);
  }

  const ownsStep = step => !disposed && isCurrent() && currentStep === step;

  async function finishStep(step) {
    if (!ownsStep(step) || step.finishing) return;
    step.finishing = true;
    const stopped = await stopGesture();
    if (!ownsStep(step)) return;
    currentStep = null;
    if (!stopped) {
      clearSteps();
      if (!step.gesture?.stopFailed) onError(new Error("ptz_stop_unconfirmed"));
      return;
    }
    void nextStep();
  }

  function finishReleasedStep(step) {
    if (!ownsStep(step) || !step.released || step.finishing || step.pulseTimer != null) return;
    // A tap must actually reach the camera before its Stop. Longer holds keep
    // the independent immediate-release Stop, even before the start reply.
    if (step.short && step.readyAt == null) return;
    const delay = step.short ? Math.max(0, 100 - (now() - step.readyAt)) : 0;
    if (delay) {
      step.pulseTimer = schedule(() => { step.pulseTimer = null; void finishStep(step); }, delay);
    } else {
      void finishStep(step);
    }
  }

  async function nextStep() {
    if (disposed || !isCurrent() || currentStep || presetPending || !steps.length) return;
    const step = steps.shift();
    currentStep = step;
    step.gesture = startGesture(step.action, step.direction, step.speed);
    if (!step.gesture) { clearSteps(); return; }
    const result = await step.gesture.initial;
    if (!ownsStep(step)) return;
    if (!result?.executed || result?.ignored_stale_control) { clearSteps(); return; }
    step.readyAt = now();
    finishReleasedStep(step);
  }

  function press(action, direction, speed) {
    if (disposed || !isCurrent() || presetPending || pressedStep) return false;
    if (steps.length >= 32) { onError(new Error("ptz_queue_full")); return false; }
    const step = { action, direction, speed, pressedAt: now(), released: false, pulseTimer: null };
    pressedStep = step;
    steps.push(step);
    void nextStep();
    return true;
  }

  function release() {
    const step = pressedStep;
    pressedStep = null;
    if (!step) return;
    step.released = true;
    // A press released while waiting is one finite step, never a late hold.
    step.short = currentStep !== step || now() - step.pressedAt <= 250;
    finishReleasedStep(step);
  }

  async function discrete(payload, tracked = false) {
    if (disposed || !isCurrent() || presetPending) return false;
    presetPending = true;
    let gesture = null;
    try {
      clearSteps();
      const stopped = active ? await stopGesture() : true;
      if (!stopped || disposed || !isCurrent()) return false;
      if (tracked) {
        gesture = { id: makeId(), sequence: 1, timer: null, stopAcknowledged: false };
        active = gesture;
      }
      const result = await send({ ...payload, dry_run: false,
        ...(gesture ? { control_id: gesture.id, sequence: gesture.sequence } : {}) });
      if (gesture && !current(gesture)) {
        if (!gesture.stopAcknowledged && isCurrent()) await send(stopPayload(gesture), { keepalive: true }).catch(() => {});
        return false;
      }
      return result?.executed === true && !disposed && isCurrent();
    } catch (error) {
      if (gesture && current(gesture)) await stopGesture();
      if (!disposed && isCurrent()) onError(error);
      return false;
    } finally { presetPending = false; }
  }

  function dispose() {
    const pending = stop();
    disposed = true;
    return pending;
  }
  return { start, press, release, stop,
    gotoPreset: token => token ? discrete({ action: "goto_preset", preset_token: token }) : Promise.resolve(false),
    point: ({ x, y }) => [x, y].every(v => Number.isFinite(v) && v >= 0 && v <= 1)
      ? discrete({ action: "point", x, y }) : Promise.resolve(false),
    areaZoom: rectangle => rectangle && ["left", "top", "right", "bottom"].every(key =>
      Number.isFinite(rectangle[key]) && rectangle[key] >= 0 && rectangle[key] <= 1)
      && rectangle.right - rectangle.left >= 1 / 8192 && rectangle.bottom - rectangle.top >= 1 / 8192
      ? discrete({ action: "area_zoom", left: rectangle.left, top: rectangle.top,
          right: rectangle.right, bottom: rectangle.bottom }, true) : Promise.resolve(false),
    dispose, activate: () => { disposed = false; } };
}
