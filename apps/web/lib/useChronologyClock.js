"use client";

import { useEffect, useRef, useState } from "react";

export default function useChronologyClock(initialTs, isScrubbingRef) {
  const currentTsRef = useRef(initialTs);
  const [currentTs, setCurrentTsState] = useState(initialTs);
  const [previewTs, setPreviewTs] = useState(initialTs);
  const [speed, setSpeed] = useState(1);
  const [isPlaying, setIsPlaying] = useState(false);
  function setCurrentTs(value) {
    const next = typeof value === "function" ? value(currentTsRef.current) : value;
    currentTsRef.current = next;
    setCurrentTsState(next);
  }
  useEffect(() => {
    if (!isPlaying || !currentTsRef.current) return undefined;
    const timer = setInterval(() => {
      setCurrentTs((prev) => prev ? new Date(prev.getTime() + 1000 * Number(speed || 1)) : prev);
    }, 1000);
    return () => clearInterval(timer);
  }, [isPlaying, speed]);
  useEffect(() => {
    if (!isScrubbingRef.current) setPreviewTs(currentTs);
  }, [currentTs]);
  return { currentTs, setCurrentTs, currentTsRef, previewTs, setPreviewTs, speed, setSpeed, isPlaying, setIsPlaying };
}
