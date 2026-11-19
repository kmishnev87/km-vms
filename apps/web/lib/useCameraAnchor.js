"use client";

import { useEffect } from "react";

export function useCameraAnchor(ready, view = "") {
  useEffect(() => {
    if (!ready) return;
    const scrollToCamera = () => {
      const hash = window.location.hash;
      if (!/^#camera-[1-9][0-9]*$/.test(hash)) return;
      document.getElementById(hash.slice(1))?.scrollIntoView({ block: "nearest" });
    };
    scrollToCamera();
    window.addEventListener("hashchange", scrollToCamera);
    return () => window.removeEventListener("hashchange", scrollToCamera);
  }, [ready, view]);
}
