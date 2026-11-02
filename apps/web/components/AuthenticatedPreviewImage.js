"use client";

import { useEffect, useState } from "react";

import { apiFetchBlob } from "../lib/api";


export default function AuthenticatedPreviewImage({ src, alt = "", fallback = null, className = "", cache = null }) {
  const [loaded, setLoaded] = useState({ src: "", url: "" });
  const [failedSource, setFailedSource] = useState(null);

  useEffect(() => {
    let active = true;
    let createdUrl = "";
    setFailedSource(null);
    if (!src) return () => {};
    if (cache?.current?.has(src)) return () => {};

    apiFetchBlob(src)
      .then(({ blob }) => {
        if (!active) return;
        createdUrl = URL.createObjectURL(blob);
        if (cache) {
          cache.current.set(src, createdUrl);
          while (cache.current.size > 8) {
            const oldest = cache.current.keys().next().value;
            URL.revokeObjectURL(cache.current.get(oldest)); cache.current.delete(oldest);
          }
        }
        setLoaded({ src, url: createdUrl });
      })
      .catch(() => {
        if (active) setFailedSource(src);
      });

    return () => {
      active = false;
      if (createdUrl && !cache) URL.revokeObjectURL(createdUrl);
    };
  }, [src, cache]);

  const objectUrl = cache?.current?.get(src) || (loaded.src === src ? loaded.url : "");
  if (!src || failedSource === src || !objectUrl) return fallback;
  return <img src={objectUrl} alt={alt} className={className} />;
}
