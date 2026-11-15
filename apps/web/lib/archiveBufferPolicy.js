// Archive-only recovery. Seconds here mean playable time, never a fixed delay.
export const ARCHIVE_START_RESERVE_SECONDS = 0.5;
export const ARCHIVE_RECOVERY_RESERVES = [8, 12, 18];

export function requiredArchiveBuffer({ reserve = ARCHIVE_START_RESERVE_SECONDS, speed = 1,
  remaining = Infinity, capacity = Infinity } = {}) {
  const rate = Number.isFinite(Number(speed)) && Number(speed) > 0 ? Number(speed) : 1;
  const target = Math.max(0.35, Math.min(18, Number(reserve) || ARCHIVE_START_RESERVE_SECONDS) * rate);
  return Math.min(target, Math.max(0, remaining - 0.03), Math.max(0.35, capacity));
}

export function createArchiveBufferPolicy(now = Date.now) {
  let level = 0, runningSince = null;
  return {
    reset() { level = 0; runningSince = null; },
    starved() { level = Math.min(ARCHIVE_RECOVERY_RESERVES.length, level + 1); runningSince = null; },
    resumed() { runningSince = now(); },
    held() { runningSince = null; },
    progress() {
      if (runningSince !== null && now() - runningSince >= 60_000) {
        level = Math.max(0, level - 1); runningSince = now();
      }
    },
    reserve() { return level ? ARCHIVE_RECOVERY_RESERVES[level - 1] : ARCHIVE_START_RESERVE_SECONDS; },
  };
}
