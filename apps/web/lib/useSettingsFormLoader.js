"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch, captureAuthSession } from "./api";
import { useCurrentUser } from "./currentUser";
import { normalizeLocale } from "./i18n";
import { languageOf, settingsDraftFromApi } from "./settingsPageHelpers";

export default function useSettingsFormLoader({ showToast, normalizedError }) {
  const [draft, setDraft] = useState(null);
  const pendingLanguageRef = useRef(null);
  const loadGenerationRef = useRef(0);
  const [savedDraft, setSavedDraft] = useState(null);
  const [hardware, setHardware] = useState(null);
  const [formatStatus, setFormatStatus] = useState(null);
  const { currentUser } = useCurrentUser();
  const lang = languageOf(draft || savedDraft);

  useEffect(() => {
    load();
    function onLanguage(event) {
      if (event.detail) {
        pendingLanguageRef.current = normalizeLocale(event.detail);
        patch("language", pendingLanguageRef.current);
      }
    }
    window.addEventListener("km-vms-language", onLanguage);
    return () => {
      loadGenerationRef.current += 1;
      window.removeEventListener("km-vms-language", onLanguage);
    };
  }, []);

  async function load() {
    const generation = ++loadGenerationRef.current;
    const ownsSession = captureAuthSession();
    const ownsLoad = () => ownsSession() && generation === loadGenerationRef.current;
    const failed = (err) => { if (ownsLoad()) showToast(normalizedError(err, lang)); };
    await Promise.allSettled([
      apiFetch("/settings").then((settingsData) => {
        if (!ownsLoad()) return;
        const nextDraft = settingsDraftFromApi(settingsData);
        setSavedDraft(nextDraft);
        setDraft(pendingLanguageRef.current ? { ...nextDraft, language: pendingLanguageRef.current } : nextDraft);
        setFormatStatus(settingsData.recording_format_change);
      }).catch(failed),
      apiFetch("/hardware/capabilities").then((data) => { if (ownsLoad()) setHardware(data); }).catch(failed),
    ]);
  }

  function patch(key, value) {
    setDraft((current) => current ? ({ ...current, [key]: value }) : current);
  }

  return { draft, setDraft, savedDraft, setSavedDraft, hardware, setHardware, formatStatus, setFormatStatus, currentUser, pendingLanguageRef, patch, load, lang };
}
