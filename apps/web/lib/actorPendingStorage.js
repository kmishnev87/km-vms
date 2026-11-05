// Recovery receipts are private to the account, not to whoever next uses a tab.
export function actorPendingStorage(storage, userId) {
  const valid = storage && Number.isInteger(userId) && userId > 0;
  const scoped = key => `${key}:user:${userId}`;
  return {
    getItem: key => valid ? storage.getItem(scoped(key)) : null,
    setItem: (key, value) => { if (valid) storage.setItem(scoped(key), value); },
    removeItem: key => { if (valid) storage.removeItem(scoped(key)); },
  };
}
