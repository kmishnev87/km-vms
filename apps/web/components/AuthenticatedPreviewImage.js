"use client";

import { useEffect, useState } from "react";

import { apiFetchBlob } from "../lib/api";


export default function AuthenticatedPreviewImage({ src, alt = "", fallback = null, className = "" }) {
  const [objectUrl, setObjectUrl] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    let createdUrl = "";
    setObjectUrl("");
    setFailed(false);
    if (!src) return () => {};

    apiFetchBlob(src)
      .then(({ blob }) => {
        if (!active) return;
        createdUrl = URL.createObjectURL(blob);
        setObjectUrl(createdUrl);
      })
      .catch(() => {
        if (active) setFailed(true);
      });

    return () => {
      active = false;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [src]);

  if (!src || failed || !objectUrl) return fallback;
  return <img src={objectUrl} alt={alt} className={className} />;
}
