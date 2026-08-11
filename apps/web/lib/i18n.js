"use client";

import React, { createContext, useContext, useEffect, useMemo, useState } from "react";
import { DICTIONARIES, TEXT_TRANSLATIONS } from "./i18n/dictionaries.js";

export const DEFAULT_LOCALE = "ru";
export const LOCALE_STORAGE_KEY = "km_vms_language";

export const SUPPORTED_LOCALES = [
  { code: "ru", label: "Русский", nativeName: "Русский", shortLabel: "RU", htmlLang: "ru" },
  { code: "en", label: "English", nativeName: "English", shortLabel: "EN", htmlLang: "en" },
  { code: "zh-CN", label: "Simplified Chinese", nativeName: "简体中文", shortLabel: "ZH", htmlLang: "zh-CN" },
];

SUPPORTED_LOCALES[0].label = "Русский";
SUPPORTED_LOCALES[0].nativeName = "Русский";
SUPPORTED_LOCALES[2].nativeName = "简体中文";

const LOCALE_ALIASES = {
  ru: "ru",
  en: "en",
  zh: "zh-CN",
  "zh-cn": "zh-CN",
  zh_cn: "zh-CN",
  cn: "zh-CN",
  chinese: "zh-CN",
};

const I18nContext = createContext({
  locale: DEFAULT_LOCALE,
  setLocale: () => {},
  t: (key, params) => interpolate(key, params),
  text: (value) => value,
});

export function normalizeLocale(value, fallback = DEFAULT_LOCALE) {
  const raw = String(value || "").trim();
  if (SUPPORTED_LOCALES.some((locale) => locale.code === raw)) return raw;
  return LOCALE_ALIASES[raw.toLowerCase()] || fallback;
}

export function localeMetadata(locale) {
  return SUPPORTED_LOCALES.find((item) => item.code === normalizeLocale(locale)) || SUPPORTED_LOCALES[0];
}

export function readStoredLocale(fallback = DEFAULT_LOCALE) {
  if (typeof window === "undefined") return fallback;
  return normalizeLocale(window.localStorage.getItem(LOCALE_STORAGE_KEY), fallback);
}

export function persistLocale(locale) {
  if (typeof window === "undefined") return;
  const normalized = normalizeLocale(locale);
  window.localStorage.setItem(LOCALE_STORAGE_KEY, normalized);
  window.dispatchEvent(new CustomEvent("km-vms-language", { detail: normalized }));
}

export function interpolate(value, params = {}) {
  return String(value || "").replace(/\{([A-Za-z0-9_]+)\}/g, (_match, key) => {
    const next = params[key];
    return next === undefined || next === null ? "" : String(next);
  });
}

export { DICTIONARIES };

export function dictionaryFor(locale) {
  return DICTIONARIES[normalizeLocale(locale)] || DICTIONARIES[DEFAULT_LOCALE];
}

function valueAtPath(source, key) {
  return String(key || "").split(".").reduce((value, part) => value?.[part], source);
}

export function translateKey(locale, key, params = {}) {
  const dictionary = dictionaryFor(locale);
  const fallback = dictionaryFor(DEFAULT_LOCALE);
  const value = valueAtPath(dictionary, key) ?? valueAtPath(fallback, key) ?? key;
  return Array.isArray(value) ? value : interpolate(value, params);
}

export function translateText(locale, text, params = {}) {
  const normalized = normalizeLocale(locale);
  const value = String(text || "");
  const trimmed = value.trim();
  if (!trimmed) return value;
  const translated = translationIndex()[trimmed]?.[normalized];
  if (!translated) return value;
  return value.replace(trimmed, interpolate(translated, params));
}

function flattenDictionary(source, target = []) {
  for (const value of Object.values(source || {})) {
    if (Array.isArray(value)) {
      for (const item of value) target.push(item);
    } else if (value && typeof value === "object") {
      flattenDictionary(value, target);
    } else if (typeof value === "string") {
      target.push(value);
    }
  }
  return target;
}

function flattenDictionaryByKey(source, prefix = "", target = {}) {
  for (const [key, value] of Object.entries(source || {})) {
    const nextKey = prefix ? `${prefix}.${key}` : key;
    if (Array.isArray(value)) {
      value.forEach((item, index) => {
        target[`${nextKey}.${index}`] = item;
      });
    } else if (value && typeof value === "object") {
      flattenDictionaryByKey(value, nextKey, target);
    } else if (typeof value === "string") {
      target[nextKey] = value;
    }
  }
  return target;
}

let cachedTranslationIndex = null;

function translationIndex() {
  if (cachedTranslationIndex) return cachedTranslationIndex;
  const index = {};
  const locales = SUPPORTED_LOCALES.map((item) => item.code);
  const keyed = Object.fromEntries(locales.map((locale) => [locale, flattenDictionaryByKey(DICTIONARIES[locale])]));
  const allKeys = new Set(locales.flatMap((locale) => Object.keys(keyed[locale])));
  for (const key of allKeys) {
    const translations = Object.fromEntries(locales.map((locale) => [locale, keyed[locale][key]])).valueOf();
    for (const source of Object.values(translations)) {
      if (!source) continue;
      index[source] = index[source] || {};
      for (const locale of locales) {
        if (translations[locale]) index[source][locale] = translations[locale];
      }
    }
  }
  for (const locale of locales) {
    for (const value of flattenDictionary(DICTIONARIES[locale])) {
      index[value] = index[value] || {};
      index[value][locale] = value;
    }
  }
  for (const [locale, table] of Object.entries(TEXT_TRANSLATIONS)) {
    for (const [source, translated] of Object.entries(table)) {
      index[source] = index[source] || {};
      index[source][locale] = translated;
      const ruFallback = index[source][DEFAULT_LOCALE] || source;
      index[translated] = index[translated] || {};
      index[translated][DEFAULT_LOCALE] = ruFallback;
      index[translated][locale] = translated;
    }
  }
  cachedTranslationIndex = index;
  return index;
}

function applyDocumentLanguage(locale) {
  if (typeof document === "undefined") return;
  const meta = localeMetadata(locale);
  document.documentElement.lang = meta.htmlLang;
  document.documentElement.dataset.locale = meta.code;
}

export function I18nProvider({ children }) {
  const [locale, setLocaleState] = useState(DEFAULT_LOCALE);

  useEffect(() => {
    setLocaleState(readStoredLocale(DEFAULT_LOCALE));
  }, []);

  useEffect(() => {
    function onLanguage(event) {
      setLocaleState(normalizeLocale(event.detail));
    }
    window.addEventListener("km-vms-language", onLanguage);
    return () => window.removeEventListener("km-vms-language", onLanguage);
  }, []);

  useEffect(() => {
    applyDocumentLanguage(locale);
  }, [locale]);

  const value = useMemo(() => ({
    locale,
    setLocale: (next) => {
      const normalized = normalizeLocale(next);
      setLocaleState(normalized);
      persistLocale(normalized);
    },
    t: (key, params) => translateKey(locale, key, params),
    text: (input, params) => translateText(locale, input, params),
  }), [locale]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  return useContext(I18nContext);
}

export function useLocaleText(namespace) {
  const { locale } = useI18n();
  return dictionaryFor(locale)[namespace] || dictionaryFor(DEFAULT_LOCALE)[namespace] || {};
}

export function LanguageSelect({ value, onChange, className = "select", "aria-label": ariaLabel, ...props }) {
  const { locale, setLocale, t } = useI18n();
  const current = normalizeLocale(value || locale);

  function handleChange(event) {
    const next = normalizeLocale(event.target.value);
    setLocale(next);
    onChange?.(next);
  }

  return (
    <select {...props} className={className} value={current} onChange={handleChange} aria-label={ariaLabel || t("common.language")}>
      {SUPPORTED_LOCALES.map((item) => (
        <option value={item.code} key={item.code}>
          {item.nativeName}
        </option>
      ))}
    </select>
  );
}

export function assertDictionaryCompleteness() {
  const locales = SUPPORTED_LOCALES.map((item) => item.code);
  const flatten = (source, prefix = "") => Object.entries(source).flatMap(([key, value]) => {
    const nextKey = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === "object" && !Array.isArray(value)) return flatten(value, nextKey);
    return [[nextKey, value]];
  });
  const reference = new Map(flatten(DICTIONARIES[DEFAULT_LOCALE]));
  const errors = [];
  for (const locale of locales) {
    const current = new Map(flatten(DICTIONARIES[locale]));
    for (const [key, value] of reference.entries()) {
      if (!current.has(key)) errors.push(`${locale} missing ${key}`);
      const candidate = current.get(key);
      if (typeof value === "string" && (!candidate || /TODO|MISSING|\?\?\?|undefined|null/i.test(String(candidate)))) {
        errors.push(`${locale} invalid ${key}`);
      }
      if (Array.isArray(value) && (!Array.isArray(candidate) || candidate.length !== value.length)) {
        errors.push(`${locale} invalid array ${key}`);
      }
    }
  }
  return { ok: errors.length === 0, locales, errors };
}
