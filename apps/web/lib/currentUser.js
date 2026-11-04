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
let stateSession = null;
let sessionGeneration = 0;
let removeAuthListeners = null;
const listeners = new Set();

function emit(nextState) {
  state = { ...state, ...nextState };
  listeners.forEach((listener) => listener());
}

function subscribe(listener) {
  listeners.add(listener);
  if (!removeAuthListeners && typeof window !== "undefined") {
    const onAuthChanged = () => {
      if (shouldFetchCurrentUser(getAuthToken())) loadCurrentUser({ force: true });
      else clearCurrentUserState(CURRENT_USER_NO_TOKEN);
    };
    const onStorage = (event) => {
      if (event.key === null || ["token", "token_expires_at"].includes(event.key)) onAuthChanged();
    };
    window.addEventListener("km-vms-auth-changed", onAuthChanged);
    window.addEventListener("storage", onStorage);
    removeAuthListeners = () => {
      window.removeEventListener("km-vms-auth-changed", onAuthChanged);
      window.removeEventListener("storage", onStorage);
      removeAuthListeners = null;
    };
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) removeAuthListeners?.();
  };
}

function snapshot() {
  return state;
}

export function clearCurrentUserState(status = CURRENT_USER_NO_TOKEN) {
  sessionGeneration += 1;
  stateSession = null;
  inFlight = null;
  emit({ status, currentUser: null, error: null });
}

export function loadCurrentUser({ force = false } = {}) {
  const token = getAuthToken();
  if (!shouldFetchCurrentUser(token)) {
    clearCurrentUserState(CURRENT_USER_NO_TOKEN);
    return Promise.resolve(null);
  }
  if (stateSession !== token) {
    sessionGeneration += 1;
    stateSession = token;
    inFlight = null;
    emit({ status: CURRENT_USER_IDLE, currentUser: null, error: null });
  }

  if (!force && state.status === CURRENT_USER_READY && state.currentUser) {
    return Promise.resolve(state.currentUser);
  }
  if (inFlight) return inFlight;

  const generation = sessionGeneration;
  const isCurrentSession = () => generation === sessionGeneration && stateSession === token && getAuthToken() === token;
  emit({ status: CURRENT_USER_LOADING, currentUser: state.currentUser, error: null });
  const request = apiFetch("/auth/me")
    .then((user) => {
      if (!isCurrentSession()) return null;
      const normalized = normalizeCurrentUser(user);
      emit({ status: CURRENT_USER_READY, currentUser: normalized, error: null });
      return normalized;
    })
    .catch((error) => {
      if (!isCurrentSession()) return null;
      if (isCurrentUserDeniedError(error)) {
        clearAuthToken();
        if (!getAuthToken()) clearCurrentUserState(CURRENT_USER_DENIED);
        return null;
      }
      emit({ status: CURRENT_USER_UNAVAILABLE, currentUser: null, error });
      return null;
    })
    .finally(() => {
      if (inFlight === request) inFlight = null;
    });
  inFlight = request;
  return inFlight;
}

export function useCurrentUser() {
  const [value, setValue] = useState(snapshot);

  useEffect(() => subscribe(() => setValue(snapshot())), []);

  useEffect(() => {
    if (stateSession !== getAuthToken() || state.status === CURRENT_USER_IDLE) {
      loadCurrentUser();
    } else if (shouldRefreshCurrentUserState(getAuthToken(), state.status)) {
      loadCurrentUser({ force: true });
    }

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
