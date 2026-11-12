"use client";

import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import { createPlaybackCoordinator } from "./chronologyPlaybackCoordinator";

const serverSnapshot = () => null;

export default function useChronologyPlaybackCoordinator(setIsPlaying, options = {}) {
  const resumeRef = useRef(setIsPlaying);
  resumeRef.current = setIsPlaying;
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const storeRef = useRef(null);
  if (!storeRef.current) storeRef.current = createPlaybackCoordinator({
    onResume: facts => { optionsRef.current.onEmptyMode?.(facts.empty); resumeRef.current(true); },
    onHold: (targetMs, reason) => { resumeRef.current(false); optionsRef.current.onHold?.(targetMs, reason); },
    onProgress: value => optionsRef.current.onProgress?.(value),
  });
  const store = storeRef.current;
  const playbackCoordinator = useSyncExternalStore(store.subscribe, store.getSnapshot, serverSnapshot);
  const startPlaybackCoordinator = useCallback(input => store.start({ ...input, targetMs: optionsRef.current.getTargetMs?.() }), [store]);
  useEffect(() => { store.activate(); return () => store.dispose(); }, [store]);
  return {
    playbackCoordinator,
    startPlaybackCoordinator,
    cancelPlaybackCoordinator: store.cancel,
    releasePlaybackCoordinator: store.release,
    handleTilePlaybackState: store.updateTile,
    registerPlaybackTile: store.registerTile,
    setPlaybackMembership: store.setMembership,
  };
}
