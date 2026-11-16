export const DISCOVERY_TERMINAL = ["completed", "cancelled", "partial", "failed"];

export function discoveryConnection(candidate, preferredProtocol) {
  const protocol = candidate.selected_protocol || preferredProtocol;
  const endpoint = candidate.onvif_endpoints?.[0];
  const rtspPort = candidate.rtsp_ports?.[0] || 554;
  return { protocol, host: candidate.host, port: protocol === "rtsp" ? rtspPort : endpoint?.port || candidate.port || 80,
    rtsp_host: candidate.host, rtsp_port: rtspPort, onvif_path: protocol === "onvif" ? endpoint?.path || candidate.xaddr_path || "" : "",
    rtsp_main_url: "", rtsp_sub_url: "", onvif_profile_token: "", onvif_sub_profile_token: "" };
}

export function discoveryRanges(selected, manual) {
  return [...new Set([...selected, ...String(manual || "").split(/[\s,;]+/).filter(Boolean)])];
}

export function discoveryPorts(value) {
  const pieces = String(value || "").split(/[\s,;]+/).filter(Boolean);
  if (!pieces.length || pieces.length > 12 || pieces.some(piece => !/^\d+$/.test(piece) || Number(piece) < 1 || Number(piece) > 65535)) return null;
  return [...new Set(pieces.map(Number))];
}

export function discoveryProgress(job) {
  const total = Number(job?.total_hosts || 0);
  const checked = Number(job?.checked_hosts || 0);
  return total ? Math.min(100, Math.round(checked / total * 100)) : null;
}

export const DISCOVERY_COPY = {
  ru: { title: "Найти камеры в сети", help: "Поиск выполняется с NAS. Камеры и их настройки не изменяются.", networks: "Где искать", local: "Локальное обнаружение ONVIF", routed: "Через маршруты / VPN", lan: "Сеть NAS", manual: "Другая подсеть или IP", manualHint: "Например, 192.168.1.0/24 или 192.168.1.20. До 1024 адресов за поиск.", advanced: "Порты поиска", onvifPorts: "ONVIF-порты", rtspPorts: "RTSP-порты", search: "Найти", cancel: "Остановить", cancelling: "Остановка…", close: "Закрыть", loading: "Чтение сетей NAS…", localPhase: "Ожидаем объявления камер в локальной сети", addressPhase: "Проверяем адреса выбранных подсетей", ready: "Выберите сети и запустите поиск.", found: "Найдено", checked: "Проверено адресов", seconds: "с", select: "Выбрать", empty: "Камеры не ответили. Проверьте выбранные сети и порты; адрес можно указать вручную.", cancelled: "Поиск остановлен. Найденные устройства сохранены в списке.", partial: "Поиск завершён частично. Ниже показано, что удалось найти.", unavailable: "Сетевой поиск на NAS недоступен. Это не означает, что камер нет. Повторите попытку или укажите адрес вручную.", invalid: "Проверьте подсети и номера портов. Допустимы частные IPv4-подсети, до 1024 адресов и до 12 портов каждого типа.", busy: "На NAS уже выполняется поиск. Дождитесь завершения или остановите его в открытом окне поиска.", statusLost: "Не удалось получить ход поиска. Повторяем запрос; поиск можно остановить.", failed: "Не удалось завершить поиск. Уже найденные устройства остаются в списке.", auth: "Понадобится авторизация", rtspHint: "Обнаружена RTSP-служба. Путь потока и учётные данные задаются при подключении.", large: "Укажите меньший диапазон", known: "Показываются и уже добавленные камеры; повторно они не добавляются автоматически.", protocols: "ONVIF и RTSP" },
  en: { title: "Find cameras on the network", help: "The NAS performs the search. Cameras and their settings are not changed.", networks: "Search networks", local: "Local ONVIF discovery", routed: "Routed / VPN network", lan: "NAS network", manual: "Another subnet or IP", manualHint: "For example, 192.168.1.0/24 or 192.168.1.20. Up to 1,024 addresses per search.", advanced: "Discovery ports", onvifPorts: "ONVIF ports", rtspPorts: "RTSP ports", search: "Find", cancel: "Stop", cancelling: "Stopping…", close: "Close", loading: "Reading NAS networks…", localPhase: "Waiting for local camera announcements", addressPhase: "Checking addresses in selected networks", ready: "Select networks and start the search.", found: "Found", checked: "Addresses checked", seconds: "s", select: "Select", empty: "No cameras responded. Check the selected networks and ports, or enter an address manually.", cancelled: "Search stopped. Discovered devices remain listed.", partial: "The search is incomplete. Available results are listed below.", unavailable: "Network discovery on the NAS is unavailable. This does not mean there are no cameras. Try again or enter the address manually.", invalid: "Check the networks and ports. Use private IPv4 ranges, up to 1,024 addresses and 12 ports of each type.", busy: "A search is already running on the NAS. Wait for it to finish or stop it in its discovery window.", statusLost: "Search progress is temporarily unavailable. Retrying; you can stop the search.", failed: "The search could not finish. Devices already found remain listed.", auth: "Sign-in required", rtspHint: "RTSP service detected. Set the stream path and credentials when connecting.", large: "Enter a smaller range", known: "Already added cameras are also shown; they are never added again automatically.", protocols: "ONVIF and RTSP" },
  "zh-CN": { title: "搜索网络摄像机", help: "由 NAS 执行搜索，不会更改摄像机及其设置。", networks: "搜索网络", local: "本地 ONVIF 发现", routed: "路由 / VPN 网络", lan: "NAS 网络", manual: "其他子网或 IP", manualHint: "例如 192.168.1.0/24 或 192.168.1.20，每次最多检查 1024 个地址。", advanced: "搜索端口", onvifPorts: "ONVIF 端口", rtspPorts: "RTSP 端口", search: "搜索", cancel: "停止", cancelling: "正在停止…", close: "关闭", loading: "正在读取 NAS 网络…", localPhase: "正在等待本地摄像机响应", addressPhase: "正在检查所选网络的地址", ready: "选择网络并开始搜索。", found: "已找到", checked: "已检查地址", seconds: "秒", select: "选择", empty: "未收到摄像机响应，请检查网络和端口，或手动输入地址。", cancelled: "搜索已停止，已发现的设备仍保留在列表中。", partial: "搜索未全部完成，以下是已获得的结果。", unavailable: "NAS 网络搜索暂不可用，这不表示没有摄像机。请重试或手动输入地址。", invalid: "请检查子网和端口。仅支持私有 IPv4 地址，每次最多 1024 个地址，每种协议最多 12 个端口。", busy: "NAS 已有搜索任务，请等待其完成或在对应搜索窗口中停止。", statusLost: "暂时无法获取搜索进度，正在重试，也可停止搜索。", failed: "搜索未能完成，已发现的设备仍保留在列表中。", auth: "需要登录", rtspHint: "已发现 RTSP 服务，请在连接时填写流路径和登录信息。", large: "请输入较小的地址范围", known: "同时显示已添加的摄像机，不会自动重复添加。", protocols: "ONVIF 和 RTSP" },
};

export function discoveryError(error, copy) {
  const code = error?.detail?.code || error?.data?.detail?.code || error?.code;
  if (code === "discovery_busy") return copy.busy;
  if (["invalid_networks", "invalid_network", "private_ipv4_required", "network_too_large", "too_many_hosts", "invalid_ports", "empty_search", "invalid_protocols"].includes(code) || error?.status === 422) return copy.invalid;
  return copy.unavailable;
}
