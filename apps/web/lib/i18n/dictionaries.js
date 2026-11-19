import enDictionary from "./en.js";
import { TEXT_TRANSLATIONS } from "./legacyTextTranslations.js";
import ruDictionary from "./ru.js";
import zhCNDictionary from "./zhCN.js";
import { RECORDING_CENTER_COPY } from "./recordingCenterCopy.js";

export const DICTIONARIES = {
  ru: { ...ruDictionary, aiRecording: RECORDING_CENTER_COPY.ru },
  en: { ...enDictionary, aiRecording: RECORDING_CENTER_COPY.en },
  "zh-CN": { ...zhCNDictionary, aiRecording: RECORDING_CENTER_COPY["zh-CN"] },
};

export { TEXT_TRANSLATIONS };
