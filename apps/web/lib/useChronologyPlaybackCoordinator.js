"use client";

import { useEffect, useRef, useSyncExternalStore } from "react";
import { createPlaybackCoordinator } from "./chronologyPlaybackCoordinator";

const serverSnapshot = () => null;

export default function useChronologyPlaybackCoordinator(setIsPlaying) {
  const resumeRef = useRef(setIsPlaying);
  resumeRef.current = setIsPlaying;
  const storeRef = useRef(null);
  if (!storeRef.current) storeRef.current = createPlaybackCoordinator({ onResume: () => resumeRef.current(true) });
  const store = storeRef.current;
  const playbackCoordinator = useSyncExternalStore(store.subscribe, store.getSnapshot, serverSnapshot);
  useEffect(() => { store.activate(); return () => store.dispose(); }, [store]);
  return {
    playbackCoordinator,
    startPlaybackCoordinator: store.start,
    cancelPlaybackCoordinator: store.cancel,
    releasePlaybackCoordinator: store.release,
    handleTilePlaybackState: store.updateTile,
  };
}
