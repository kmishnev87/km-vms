export const normalizeOnlineMode = value => value === "fast" ? "fast" : "stable";
export const wantsFastOnline = (mode, ptz) => normalizeOnlineMode(mode) === "fast" || Boolean(ptz);
export const isMseCompatibilityFailure = error => [
  "mse_browser_unsupported", "mse_codec_unsupported", "mse_decode_unsupported",
].includes(error?.code);

export function onlineModeCopy(language) {
  if (language === "en") return {
    title: "Live mode", fast: "Fast", stable: "Stable",
    fastSummary: "Lower delay; more sensitive to connection interruptions.",
    stableSummary: "More delay; more resilient to connection interruptions.",
    help: "Fast: lower video delay and usually faster startup; more sensitive to connection interruptions. Incompatible streams use HLS with an explanation.\n\nStable: more buffering and a longer delay; opening PTZ controls still enables the fast route. Shared by the web and Android clients of this NAS; recording is unchanged.",
    fallback: "HLS enabled due to incompatibility",
  };
  if (language === "zh-CN") return {
    title: "实时模式", fast: "快速", stable: "稳定",
    fastSummary: "延迟较低，但对网络中断更敏感。",
    stableSummary: "延迟较长，但更能应对网络中断。",
    help: "快速：视频延迟较低，通常启动更快，但对网络中断更敏感。不兼容的视频流会切换到 HLS 并显示说明。\n\n稳定：缓冲更多、延迟较长；打开 PTZ 控制仍会启用快速传输。此设置由本 NAS 的网页和 Android 客户端共用，不影响录像。",
    fallback: "由于不兼容，已启用 HLS",
  };
  return {
    title: "Режим онлайн", fast: "Быстрый", stable: "Стабильный",
    fastSummary: "Меньше задержка, чувствительнее к перебоям связи.",
    stableSummary: "Больше задержка, устойчивее к перебоям связи.",
    help: "Быстрый: меньше задержка видео и обычно быстрее появляется картинка; чувствительнее к перебоям связи. Несовместимый поток перейдёт на HLS с пояснением.\n\nСтабильный: больше запас видео и задержка; открытие пульта PTZ по-прежнему включает быстрый маршрут. Общая настройка веб-версии и Android для этого NAS; запись не меняется.",
    fallback: "Включён режим HLS из-за несовместимости",
  };
}
