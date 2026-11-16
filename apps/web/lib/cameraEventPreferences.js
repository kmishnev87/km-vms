export function mergeEventPreferenceDraft(draft, previous, next, force = false) {
  return force || draft === null || JSON.stringify(draft) === JSON.stringify(previous) ? next : draft;
}

export function toggleEventPreference(preference, key, item, all) {
  const values = preference[key] === null ? all : preference[key] || [];
  return { ...preference, [key]: values.includes(item) ? values.filter(value => value !== item) : [...values, item] };
}

export function changeCameraNotificationTime(preference, cameraId, key, minute) {
  return { ...preference, hours_enabled: true, camera_schedules: { ...preference.camera_schedules,
    [cameraId]: { start_minute: preference.start_minute, end_minute: preference.end_minute,
      ...preference.camera_schedules?.[cameraId], [key]: minute } } };
}

// The same top-to-bottom groups are used with common and individual hours.
export function notificationCameraGroups(cameras) {
  return Array.from({ length: Math.ceil(cameras.length / 5) }, (_, index) => cameras.slice(index * 5, index * 5 + 5));
}
