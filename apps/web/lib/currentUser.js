"use client";

import { useEffect, useState } from "react";
import { apiFetch, clearAuthToken, getAuthToken } from "./api";
import {
  CURRENT_USER_DENIED,
  CURRENT_USER_IDLE,
  CURRENT_USER_LOADING,
  CURRENT_USER_NO_TOKEN,
  CURRENT_USER_READY,
  CURRENT_USER_UNAVAILABLE,
  isCurrentUserDeniedError,
  normalizeCurrentUser,
  shouldFetchCurrentUser,
  shouldRefreshCurrentUserState,
} from "./currentUserCore";

let state = {
  status: CURRENT_USER_IDLE,
  currentUser: null,
  error: null,
};
let inFlight = null;
const listeners = new Set();

function emit(nextState) {
  state = { ...state, ...nextState };
  listeners.forEach((listener) => listener());
}

function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function snapshot() {
  return state;
}

export function clearCurrentUserState(status = CURRENT_USER_NO_TOKEN) {
  inFlight = null;
  emit({ status, currentUser: null, error: null });
}

export function loadCurrentUser({ force = false } = {}) {
  const token = getAuthToken();
  if (!shouldFetchCurrentUser(token)) {
    clearCurrentUserState(CURRENT_USER_NO_TOKEN);
    return Promise.resolve(null);
  }

  if (!force && state.status === CURRENT_USER_READY && state.currentUser) {
    return Promise.resolve(state.currentUser);
  }
  if (!force && inFlight) return inFlight;

  emit({ status: CURRENT_USER_LOADING, currentUser: force ? null : state.currentUser, error: null });
  inFlight = apiFetch("/auth/me")
    .then((user) => {
      const normalized = normalizeCurrentUser(user);
      emit({ status: CURRENT_USER_READY, currentUser: normalized, error: null });
      return normalized;
    })
    .catch((error) => {
      if (isCurrentUserDeniedError(error)) {
        clearAuthToken();
        emit({ status: CURRENT_USER_DENIED, currentUser: null, error });
        return null;
      }
      emit({ status: CURRENT_USER_UNAVAILABLE, currentUser: null, error });
      return null;
    })
    .finally(() => {
      inFlight = null;
    });
  return inFlight;
}

export function useCurrentUser() {
  const [value, setValue] = useState(snapshot);

  useEffect(() => subscribe(() => setValue(snapshot())), []);

  useEffect(() => {
    if (state.status === CURRENT_USER_IDLE) {
      loadCurrentUser();
    } else if (shouldRefreshCurrentUserState(getAuthToken(), state.status)) {
      loadCurrentUser({ force: true });
    }

    function onAuthChanged() {
      if (shouldFetchCurrentUser(getAuthToken())) {
        loadCurrentUser({ force: true });
      } else {
        clearCurrentUserState(CURRENT_USER_NO_TOKEN);
      }
    }

    window.addEventListener("km-vms-auth-changed", onAuthChanged);
    window.addEventListener("storage", onAuthChanged);
    return () => {
      window.removeEventListener("km-vms-auth-changed", onAuthChanged);
      window.removeEventListener("storage", onAuthChanged);
    };
  }, []);

  return {
    currentUser: value.currentUser,
    status: value.status,
    error: value.error,
    loading: value.status === CURRENT_USER_IDLE || value.status === CURRENT_USER_LOADING,
    refresh: () => loadCurrentUser({ force: true }),
    clear: () => clearCurrentUserState(CURRENT_USER_NO_TOKEN),
  };
}
