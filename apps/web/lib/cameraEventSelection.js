export function selectedEventCount(ids, snapshot) {
  return snapshot ? Math.max(0, snapshot.count - (snapshot.excluded_ids?.length || 0)) : ids.length;
}

export function eventIsSelected(id, ids, snapshot) {
  return snapshot ? snapshot.visible_ids.includes(id) && !snapshot.excluded_ids.includes(id) : ids.includes(id);
}

export function toggleSnapshotEvent(snapshot, id, checked) {
  if (!snapshot?.visible_ids.includes(id)) return snapshot;
  const excluded = new Set(snapshot.excluded_ids);
  if (checked) excluded.delete(id); else excluded.add(id);
  return { ...snapshot, excluded_ids: [...excluded] };
}

export function snapshotHistoryQuery(query, offset, snapshot) {
  if (!snapshot) return query;
  const params = new URLSearchParams(snapshot.query);
  params.set("offset", String(Math.max(0, offset)));
  params.set("selection_id", snapshot.id);
  return params.toString();
}

export function filteredSelectionBody(query, through, rows) {
  const params = new URLSearchParams(query);
  return { through, camera_id: params.has("camera_id") ? Number(params.get("camera_id")) : null,
    kind: params.get("kind"), since: params.get("since"), until: params.get("until"),
    ...(params.has("source") ? { source: params.get("source") } : {}),
    ...(params.has("rule") ? { rule: params.get("rule") } : {}),
    ...(params.has("condition") ? { condition: params.get("condition") } : {}),
    unread: params.get("unread") === "true", visible_ids: rows.map(row => row.id) };
}
