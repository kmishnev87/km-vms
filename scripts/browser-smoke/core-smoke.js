"use strict";

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const SCHEMA_VERSION = "km-vms-browser-smoke/v1";
const NAVIGATION_TIMEOUT_MS = 20000;
const MANDATORY_TIMEOUT_MS = 15000;
const STABILIZATION_MS = 500;

const VIEWPORTS = [
  { name: "desktop", width: 1365, height: 900 },
  { name: "mobile", width: 390, height: 844 },
];

const ROUTES = [
  { pathname: "/", slug: "dashboard", marker: ".dashboardPage" },
  { pathname: "/live", slug: "live", marker: ".liveWorkspaceShell" },
  { pathname: "/chronology", slug: "chronology", marker: ".chronologyShell" },
  { pathname: "/recordings", slug: "recordings", marker: ".standardPage .recordingsHeader" },
  { pathname: "/cameras", slug: "cameras", marker: ".standardPage .cameraPageHeader" },
  { pathname: "/storage", slug: "storage", marker: ".storageOpsPage" },
  { pathname: "/settings", slug: "settings", marker: ".settingsPage .settingsHeader" },
  { pathname: "/diagnostics", slug: "diagnostics", marker: ".settingsPage .settingsHeader" },
  { pathname: "/security-journal", slug: "security-journal", marker: ".settingsPage .settingsHeader" },
  { pathname: "/system-status", slug: "system-status", marker: ".systemStatusPage" },
  { pathname: "/apk", slug: "apk", marker: ".apkPage" },
];

const SHARED_MANDATORY = [
  ["GET", "/api/system/status"],
  ["GET", "/api/auth/me"],
];

const ROUTE_MANDATORY = Object.freeze({
  "/": [],
  "/live": [
    ["GET", "/api/viewer/cameras"],
    ["GET", "/api/users/me"],
    ["GET", "/api/users/me/workspaces/live/layout"],
  ],
  "/chronology": [
    ["GET", "/api/viewer/cameras"],
    ["GET", "/api/users/me"],
    ["GET", "/api/users/me/workspaces/chronology/layout"],
  ],
  "/recordings": [
    ["GET", "/api/recordings/cameras"],
    ["GET", "/api/recordings"],
  ],
  "/cameras": [["GET", "/api/cameras"]],
  "/storage": [["GET", "/api/storage/status"]],
  "/settings": [
    ["GET", "/api/settings"],
    ["GET", "/api/hardware/capabilities"],
    ["GET", "/api/users/me"],
  ],
  "/diagnostics": [],
  "/security-journal": [["GET", "/api/audit/events"]],
  "/system-status": [["GET", "/api/system/runtime/status"]],
  "/apk": [],
});

const SAFE_STATIC_ENDPOINTS = new Set([
  ...SHARED_MANDATORY.map(([method, pathname]) => `${method} ${pathname}`),
  ...Object.values(ROUTE_MANDATORY).flat().map(([method, pathname]) => `${method} ${pathname}`),
  "GET /api/users",
  "GET /api/recordings/cameras",
  "GET /api/system/runtime/status",
  "GET /api/system/recorder/summary",
  "GET /api/storage/status",
  "GET /api/storage/migration/operations/active",
  "GET /api/system/update/status",
  "GET /api/system/update/apply/status",
]);

function requestKey(method, pathname) {
  return `${String(method || "").toUpperCase()} ${pathname}`;
}

function mandatoryStatusFails(isMandatory, status) {
  return Boolean(isMandatory && Number(status) >= 400);
}

function unauthenticatedStatusFails(status, redirectedToLogin) {
  if (redirectedToLogin && [401, 403].includes(Number(status))) return false;
  return Number(status) >= 400;
}

function sanitizeEndpointPath(method, pathname) {
  const normalizedMethod = String(method || "").toUpperCase();
  const key = requestKey(normalizedMethod, pathname);
  if (SAFE_STATIC_ENDPOINTS.has(key)) return pathname;

  if (/^\/api\/live\/[^/]+\/[^/]+\/index\.m3u8$/.test(pathname)) {
    return "/api/live/{camera_id}/{stream}/index.m3u8";
  }
  if (/^\/api\/live\//.test(pathname)) return "/api/live/{dynamic}";
  if (/^\/api\/recordings\//.test(pathname)) return "/api/recordings/{dynamic}";
  if (/^\/api\/cameras\//.test(pathname)) return "/api/cameras/{dynamic}";
  if (/^\/api\/storage\//.test(pathname)) return "/api/storage/{dynamic}";
  if (/^\/api\/users\//.test(pathname)) return "/api/users/{dynamic}";
  if (/^\/api\/audit\//.test(pathname)) return "/api/audit/{dynamic}";
  if (/^\/api\/system\//.test(pathname)) return "/api/system/{dynamic}";
  return null;
}

function safeFailureFact(failure) {
  const fact = {
    method: String(failure.method || "").toUpperCase(),
    status: Number(failure.status || 0),
  };
  const template = sanitizeEndpointPath(fact.method, failure.pathname);
  if (template) fact.path_template = template;
  return fact;
}

function uniqueFailureFacts(failures, limit = 20) {
  const seen = new Set();
  const result = [];
  for (const failure of failures) {
    const fact = safeFailureFact(failure);
    const key = JSON.stringify(fact);
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(fact);
    if (result.length >= limit) break;
  }
  return result;
}

function assertSelfTest(condition, checkId) {
  if (!condition) throw new Error(checkId);
}

function runSelfTest() {
  const cameraId = "camera-secret-id-123";
  const hls = sanitizeEndpointPath("GET", `/api/live/${cameraId}/main/index.m3u8`);
  assertSelfTest(hls === "/api/live/{camera_id}/{stream}/index.m3u8", "hls_template_mismatch");
  assertSelfTest(!hls.includes(cameraId), "hls_identifier_leaked");
  const recordingId = "recording-secret-id-456";
  const recording = sanitizeEndpointPath("GET", `/api/recordings/${recordingId}/media`);
  assertSelfTest(recording === "/api/recordings/{dynamic}", "recording_template_mismatch");
  assertSelfTest(!recording.includes(recordingId), "recording_identifier_leaked");
  assertSelfTest(mandatoryStatusFails(true, 404), "mandatory_404_not_fatal");
  assertSelfTest(mandatoryStatusFails(true, 504), "mandatory_504_not_fatal");
  assertSelfTest(!mandatoryStatusFails(false, 404), "optional_404_became_fatal");
  assertSelfTest(!mandatoryStatusFails(false, 504), "optional_504_became_fatal");
  assertSelfTest(!unauthenticatedStatusFails(401, true), "unauth_401_became_fatal");
  assertSelfTest(!unauthenticatedStatusFails(403, true), "unauth_403_became_fatal");
  process.stdout.write("SELF_TEST_PASS\n");
}

function validateLoopbackOrigin(value, expectedPort) {
  const parsed = new URL(value);
  const hostname = parsed.hostname.toLowerCase();
  const loopback = hostname === "127.0.0.1" || hostname === "localhost" || hostname === "[::1]" || hostname === "::1";
  if (!loopback) throw new Error("origin_not_loopback");
  if (parsed.protocol !== "http:") throw new Error("origin_scheme_invalid");
  if (parsed.port !== String(expectedPort)) throw new Error("origin_port_mismatch");
  if (parsed.pathname !== "/" || parsed.search || parsed.hash || parsed.username || parsed.password) {
    throw new Error("origin_not_root");
  }
  return parsed.origin;
}

function runtimeConfig() {
  const baseUrl = process.env.KMVMS_BASE_URL || "";
  const approvedOrigin = process.env.KMVMS_APPROVED_ORIGIN || "";
  const approvedPort = process.env.KMVMS_APPROVED_HTTP_PORT || "";
  const username = process.env.KMVMS_USERNAME || "";
  const password = process.env.KMVMS_PASSWORD || "";
  const outDir = path.resolve(process.env.KMVMS_SMOKE_OUT_DIR || "");
  if (!/^\d+$/.test(approvedPort)) throw new Error("approved_port_invalid");
  const normalizedBase = validateLoopbackOrigin(baseUrl, approvedPort);
  const normalizedApproved = validateLoopbackOrigin(approvedOrigin, approvedPort);
  if (normalizedBase !== normalizedApproved) throw new Error("origin_not_approved");
  if (!username || !password) throw new Error("credentials_missing");
  if (outDir !== "/artifacts") throw new Error("output_dir_invalid");
  return { baseUrl: normalizedBase, approvedOrigin: normalizedApproved, username, password, outDir };
}

function isoNow() {
  return new Date().toISOString();
}

function ensureOutputPath(outDir, relativePath) {
  const absolute = path.resolve(outDir, relativePath);
  if (absolute !== outDir && !absolute.startsWith(`${outDir}${path.sep}`)) {
    throw new Error("output_path_escape");
  }
  fs.mkdirSync(path.dirname(absolute), { recursive: true });
  return absolute;
}

function writeJson(outDir, relativePath, value, fileIndex) {
  const absolute = ensureOutputPath(outDir, relativePath);
  fs.writeFileSync(absolute, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  if (fileIndex && !fileIndex.includes(relativePath)) fileIndex.push(relativePath);
}

function safeUrl(url, approvedOrigin) {
  try {
    const parsed = new URL(url);
    if (parsed.origin !== approvedOrigin) return null;
    return parsed;
  } catch (_) {
    return null;
  }
}

function mandatoryEntriesForRoute(pathname) {
  const entries = [...SHARED_MANDATORY, ...(ROUTE_MANDATORY[pathname] || [])];
  return new Set(entries.map(([method, endpoint]) => requestKey(method, endpoint)));
}

function createRouteTracker(page, routePathname, approvedOrigin) {
  const expected = mandatoryEntriesForRoute(routePathname);
  const observedRequests = new Set();
  const responseStatuses = new Map();
  const failures = [];
  const documentFailures = [];
  const responseTasks = [];
  let pageErrorCount = 0;
  let crashed = false;
  let settingsAdminKnown = routePathname !== "/settings";
  let settingsAdminAccess = false;

  function maybeRequireSettingsUsers() {
    const usersKey = requestKey("GET", "/api/users");
    if (routePathname === "/settings" && settingsAdminKnown && settingsAdminAccess && observedRequests.has(usersKey)) {
      expected.add(usersKey);
    }
  }

  const onRequest = (request) => {
    const parsed = safeUrl(request.url(), approvedOrigin);
    if (!parsed) return;
    observedRequests.add(requestKey(request.method(), parsed.pathname));
    maybeRequireSettingsUsers();
  };

  const onRequestFailed = (request) => {
    const parsed = safeUrl(request.url(), approvedOrigin);
    if (!parsed) return;
    failures.push({ method: request.method(), pathname: parsed.pathname, status: 0 });
  };

  const onResponse = (response) => {
    const request = response.request();
    const parsed = safeUrl(response.url(), approvedOrigin);
    if (!parsed) return;
    const key = requestKey(request.method(), parsed.pathname);
    const status = response.status();
    if (!responseStatuses.has(key)) responseStatuses.set(key, status);
    if (request.resourceType() === "document" && status >= 400) {
      documentFailures.push(status);
    }
    if (status >= 400) failures.push({ method: request.method(), pathname: parsed.pathname, status });

    if (routePathname === "/settings" && key === requestKey("GET", "/api/users/me")) {
      if (status < 400) {
        const task = response.json()
          .then((value) => {
            const permissions = Array.isArray(value?.permissions) ? value.permissions : [];
            settingsAdminAccess = permissions.includes("admin_access");
            settingsAdminKnown = true;
            maybeRequireSettingsUsers();
          })
          .catch(() => {
            settingsAdminKnown = true;
            settingsAdminAccess = false;
          });
        responseTasks.push(task);
      } else {
        settingsAdminKnown = true;
        settingsAdminAccess = false;
      }
    }
  };

  const onPageError = () => {
    pageErrorCount += 1;
  };
  const onCrash = () => {
    crashed = true;
  };

  page.on("request", onRequest);
  page.on("requestfailed", onRequestFailed);
  page.on("response", onResponse);
  page.on("pageerror", onPageError);
  page.on("crash", onCrash);

  return {
    expected,
    observedRequests,
    responseStatuses,
    failures,
    documentFailures,
    responseTasks,
    get pageErrorCount() { return pageErrorCount; },
    get crashed() { return crashed; },
    get settingsAdminKnown() { return settingsAdminKnown; },
    refreshConditional: maybeRequireSettingsUsers,
    detach() {
      page.off("request", onRequest);
      page.off("requestfailed", onRequestFailed);
      page.off("response", onResponse);
      page.off("pageerror", onPageError);
      page.off("crash", onCrash);
    },
  };
}

async function waitForMandatory(tracker) {
  const deadline = Date.now() + MANDATORY_TIMEOUT_MS;
  let stableSince = null;
  while (Date.now() < deadline) {
    if (tracker.crashed) break;
    await Promise.allSettled([...tracker.responseTasks]);
    tracker.refreshConditional();
    const missing = [...tracker.expected].filter((key) => !tracker.responseStatuses.has(key));
    const ready = missing.length === 0 && tracker.settingsAdminKnown;
    if (ready) {
      if (stableSince === null) stableSince = Date.now();
      if (Date.now() - stableSince >= STABILIZATION_MS) return [];
    } else {
      stableSince = null;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  tracker.refreshConditional();
  return [...tracker.expected].filter((key) => !tracker.responseStatuses.has(key));
}

async function shellFacts(page, marker) {
  const markerFound = await page.locator(marker).count() > 0;
  const layoutFound = await page.locator(".layoutShell").count() > 0;
  const topNavFound = await page.locator(".topNav").count() > 0;
  const mainFound = await page.locator("main.mainContent").count() > 0;
  const geometry = await page.evaluate(() => {
    const root = document.documentElement;
    const main = document.querySelector("main.mainContent");
    return {
      client_width: Number(root?.clientWidth || 0),
      scroll_width: Number(root?.scrollWidth || 0),
      main_child_count: Number(main?.childElementCount || 0),
      main_text_length: Number((main?.innerText || "").trim().length),
    };
  });
  return { markerFound, layoutFound, topNavFound, mainFound, geometry };
}

async function hasFatalRender(page) {
  return page.evaluate(() => {
    const text = String(document.body?.innerText || "").toLowerCase();
    return text.includes("bad gateway") ||
      text.includes("gateway time-out") ||
      text.includes("gateway timeout") ||
      text.includes("internal server error") ||
      text.includes("application error: a client-side exception");
  });
}

async function redactForScreenshot(page) {
  return page.evaluate(() => {
    const style = document.createElement("style");
    style.id = "kmvms-smoke-redaction";
    style.textContent = `
      body .kmvmsSmokeTextRedacted {
        color: transparent !important;
        -webkit-text-fill-color: transparent !important;
        text-shadow: none !important;
        background: #dce3ea !important;
        border-radius: 4px !important;
      }
      body .kmvmsSmokeMediaParent {
        background: #dce3ea !important;
      }
      body input,
      body select,
      body textarea,
      body [contenteditable="true"] {
        color: transparent !important;
        -webkit-text-fill-color: transparent !important;
        caret-color: transparent !important;
        text-shadow: none !important;
        background-color: #dce3ea !important;
      }
      body input::placeholder,
      body textarea::placeholder {
        color: transparent !important;
        -webkit-text-fill-color: transparent !important;
      }
      body video,
      body canvas,
      body img {
        visibility: hidden !important;
      }
    `;
    document.head.appendChild(style);

    let textElements = 0;
    for (const element of document.body?.querySelectorAll("*") || []) {
      const hasDirectText = [...element.childNodes].some((node) => node.nodeType === 3 && String(node.textContent || "").trim());
      if (hasDirectText) {
        element.classList.add("kmvmsSmokeTextRedacted");
        textElements += 1;
      }
    }

    let mediaElements = 0;
    for (const element of document.querySelectorAll("body video, body canvas, body img")) {
      if (element.parentElement) element.parentElement.classList.add("kmvmsSmokeMediaParent");
      mediaElements += 1;
    }
    const controlElements = document.querySelectorAll(
      'body input, body select, body textarea, body [contenteditable="true"]',
    ).length;
    return {
      text_elements: textElements,
      media_elements: mediaElements,
      control_elements: controlElements,
      main_text_redacted: true,
      whole_page_redacted: true,
      operator_values_redacted: true,
      scope: "document.body",
    };
  });
}

async function runGlobalRedactionSelfTest(browser) {
  const context = await browser.newContext({ viewport: { width: 640, height: 480 } });
  try {
    const page = await context.newPage();
    await page.setContent(`
      <!doctype html>
      <html>
        <head><title>redaction self-test</title></head>
        <body>
          <section class="setupPage">
            <p id="redaction-text">synthetic operator value</p>
            <input id="redaction-input" value="synthetic input value" />
            <select id="redaction-select"><option selected>synthetic selected value</option></select>
            <textarea id="redaction-textarea">synthetic textarea value</textarea>
            <div id="redaction-editable" contenteditable="true">synthetic editable value</div>
            <img id="redaction-image" alt="synthetic media" />
            <canvas id="redaction-canvas"></canvas>
            <video id="redaction-video"></video>
          </section>
        </body>
      </html>
    `);
    const facts = await redactForScreenshot(page);
    const checks = await page.evaluate(() => {
      const transparent = (element) => getComputedStyle(element).color === "rgba(0, 0, 0, 0)";
      const hidden = (element) => getComputedStyle(element).visibility === "hidden";
      return {
        outsideMain: !document.querySelector("main.mainContent"),
        text: document.querySelector("#redaction-text")?.classList.contains("kmvmsSmokeTextRedacted") === true,
        input: transparent(document.querySelector("#redaction-input")),
        select: transparent(document.querySelector("#redaction-select")),
        textarea: transparent(document.querySelector("#redaction-textarea")),
        editable: transparent(document.querySelector("#redaction-editable")),
        image: hidden(document.querySelector("#redaction-image")),
        canvas: hidden(document.querySelector("#redaction-canvas")),
        video: hidden(document.querySelector("#redaction-video")),
      };
    });
    const passed = facts.whole_page_redacted === true &&
      facts.operator_values_redacted === true &&
      facts.scope === "document.body" &&
      Object.values(checks).every(Boolean);
    return passed ? [] : ["global_redaction_self_test_failed"];
  } catch (_) {
    return ["global_redaction_self_test_failed"];
  } finally {
    await context.close().catch(() => {});
  }
}

function mandatoryFacts(tracker) {
  return [...tracker.expected]
    .sort()
    .map((key) => {
      const separator = key.indexOf(" ");
      return {
        method: key.slice(0, separator),
        path_template: key.slice(separator + 1),
        observed: tracker.observedRequests.has(key),
        status: tracker.responseStatuses.has(key) ? tracker.responseStatuses.get(key) : 0,
      };
    });
}

async function runRoute(page, route, viewport, config, fileIndex) {
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  const tracker = createRouteTracker(page, route.pathname, config.approvedOrigin);
  const checkIds = [];
  let finalPathname = "";
  let shell = {
    markerFound: false,
    layoutFound: false,
    topNavFound: false,
    mainFound: false,
    geometry: { client_width: 0, scroll_width: 0, main_child_count: 0, main_text_length: 0 },
  };
  let fatalRender = false;
  let redaction = {
    text_elements: 0,
    media_elements: 0,
    control_elements: 0,
    main_text_redacted: false,
    whole_page_redacted: false,
    operator_values_redacted: false,
    scope: "none",
  };

  try {
    await page.goto(`${config.baseUrl}${route.pathname}`, { waitUntil: "domcontentloaded", timeout: NAVIGATION_TIMEOUT_MS });
    await page.locator(route.marker).waitFor({ state: "attached", timeout: NAVIGATION_TIMEOUT_MS });
  } catch (_) {
    checkIds.push(`${route.slug}_${viewport.name}_navigation_or_marker_failed`);
  }

  const missing = await waitForMandatory(tracker);
  if (missing.length) checkIds.push(`${route.slug}_${viewport.name}_mandatory_response_missing`);

  try {
    const parsed = new URL(page.url());
    finalPathname = parsed.pathname;
    if (parsed.origin !== config.approvedOrigin || finalPathname !== route.pathname) {
      checkIds.push(`${route.slug}_${viewport.name}_route_mismatch`);
    }
    if (finalPathname === "/login" || finalPathname === "/setup") {
      checkIds.push(`${route.slug}_${viewport.name}_unexpected_redirect`);
    }
  } catch (_) {
    checkIds.push(`${route.slug}_${viewport.name}_url_invalid`);
  }

  try {
    shell = await shellFacts(page, route.marker);
    if (!shell.markerFound) checkIds.push(`${route.slug}_${viewport.name}_marker_missing`);
    if (!shell.layoutFound || !shell.topNavFound || !shell.mainFound) {
      checkIds.push(`${route.slug}_${viewport.name}_authenticated_shell_missing`);
    }
    if (shell.geometry.main_child_count < 1 || shell.geometry.main_text_length < 1) {
      checkIds.push(`${route.slug}_${viewport.name}_main_empty`);
    }
    const overflow = shell.geometry.scroll_width - shell.geometry.client_width;
    if (overflow > 1) checkIds.push(`${route.slug}_${viewport.name}_page_overflow`);
    fatalRender = await hasFatalRender(page);
    if (fatalRender) checkIds.push(`${route.slug}_${viewport.name}_fatal_render`);
  } catch (_) {
    checkIds.push(`${route.slug}_${viewport.name}_dom_facts_failed`);
  }

  if (tracker.documentFailures.length) checkIds.push(`${route.slug}_${viewport.name}_document_http_error`);
  if (tracker.pageErrorCount) checkIds.push(`${route.slug}_${viewport.name}_pageerror`);
  if (tracker.crashed) checkIds.push(`${route.slug}_${viewport.name}_browser_crash`);

  const mandatory = mandatoryFacts(tracker);
  if (mandatory.some((item) => mandatoryStatusFails(true, item.status))) {
    checkIds.push(`${route.slug}_${viewport.name}_mandatory_http_error`);
  }

  const optionalFailures = tracker.failures.filter((failure) => {
    const key = requestKey(failure.method, failure.pathname);
    return !tracker.expected.has(key);
  });

  const screenshotRelative = `routes/${viewport.name}/${route.slug}.png`;
  try {
    redaction = await redactForScreenshot(page);
    await page.screenshot({
      path: ensureOutputPath(config.outDir, screenshotRelative),
      fullPage: false,
      animations: "disabled",
    });
    fileIndex.push(screenshotRelative);
  } catch (_) {
    checkIds.push(`${route.slug}_${viewport.name}_screenshot_failed`);
  }

  const uniqueCheckIds = [...new Set(checkIds)].sort();
  const factsRelative = `routes/${viewport.name}/${route.slug}.facts.json`;
  writeJson(config.outDir, factsRelative, {
    schema_version: SCHEMA_VERSION,
    status: uniqueCheckIds.length ? "FAIL" : "PASS",
    check_ids: uniqueCheckIds,
    route: route.pathname,
    viewport: { name: viewport.name, width: viewport.width, height: viewport.height },
    final_pathname: finalPathname,
    marker: { selector: route.marker, found: shell.markerFound },
    shell: {
      layout: shell.layoutFound,
      top_nav: shell.topNavFound,
      main: shell.mainFound,
      main_child_count: shell.geometry.main_child_count,
      main_text_present: shell.geometry.main_text_length > 0,
    },
    overflow: {
      client_width: shell.geometry.client_width,
      scroll_width: shell.geometry.scroll_width,
      overflow_pixels: Math.max(0, shell.geometry.scroll_width - shell.geometry.client_width),
    },
    mandatory_responses: mandatory,
    optional_failed_response_count: optionalFailures.length,
    optional_failed_responses: uniqueFailureFacts(optionalFailures),
    page_error_count: tracker.pageErrorCount,
    crashed: tracker.crashed,
    fatal_render: fatalRender,
    redaction,
    screenshot: screenshotRelative,
  }, fileIndex);

  tracker.detach();
  return uniqueCheckIds;
}

async function runUnauthenticated(browser, config, fileIndex) {
  const context = await browser.newContext({ viewport: VIEWPORTS[0] });
  const page = await context.newPage();
  const checkIds = [];
  const apiStatuses = [];
  let pageErrorCount = 0;
  let crashed = false;
  let redirected = false;

  page.on("pageerror", () => { pageErrorCount += 1; });
  page.on("crash", () => { crashed = true; });
  page.on("response", (response) => {
    const request = response.request();
    const parsed = safeUrl(response.url(), config.approvedOrigin);
    if (!parsed || !parsed.pathname.startsWith("/api/")) return;
    apiStatuses.push({ status: response.status(), method: request.method(), pathname: parsed.pathname });
  });

  try {
    const documentResponse = await page.goto(`${config.baseUrl}/settings`, {
      waitUntil: "domcontentloaded",
      timeout: NAVIGATION_TIMEOUT_MS,
    });
    if (documentResponse && documentResponse.status() >= 400) checkIds.push("unauthenticated_document_http_error");
    await page.waitForURL((url) => url.origin === config.approvedOrigin && url.pathname === "/login", {
      timeout: NAVIGATION_TIMEOUT_MS,
    });
    redirected = true;
  } catch (_) {
    checkIds.push("unauthenticated_redirect_failed");
  }

  let finalPathname = "";
  try {
    finalPathname = new URL(page.url()).pathname;
    if (finalPathname === "/setup") checkIds.push("unauthenticated_setup_redirect");
    if (finalPathname !== "/login") checkIds.push("unauthenticated_protected_route_visible");
    if (await hasFatalRender(page)) checkIds.push("unauthenticated_fatal_render");
  } catch (_) {
    checkIds.push("unauthenticated_url_invalid");
  }
  if (pageErrorCount) checkIds.push("unauthenticated_pageerror");
  if (crashed) checkIds.push("unauthenticated_browser_crash");

  const unexpected = apiStatuses.filter((item) => unauthenticatedStatusFails(item.status, redirected));
  const gatewayFailures = unexpected.filter((item) => [500, 502, 503, 504].includes(item.status));
  if (gatewayFailures.length) checkIds.push("unauthenticated_gateway_error");

  const uniqueCheckIds = [...new Set(checkIds)].sort();
  writeJson(config.outDir, "unauthenticated.facts.json", {
    schema_version: SCHEMA_VERSION,
    status: uniqueCheckIds.length ? "FAIL" : "PASS",
    check_ids: uniqueCheckIds,
    final_pathname: finalPathname,
    redirected_to_login: redirected,
    expected_pre_redirect_401_403_count: apiStatuses.filter((item) => [401, 403].includes(item.status)).length,
    page_error_count: pageErrorCount,
    crashed,
  }, fileIndex);

  await context.close();
  return uniqueCheckIds;
}

async function login(page, config, fileIndex) {
  const checkIds = [];
  let authenticatedShell = false;
  let finalPathname = "";
  let pageErrorCount = 0;
  const onPageError = () => { pageErrorCount += 1; };
  page.on("pageerror", onPageError);

  try {
    await page.goto(`${config.baseUrl}/login`, { waitUntil: "domcontentloaded", timeout: NAVIGATION_TIMEOUT_MS });
    await page.locator(".authPage").waitFor({ state: "attached", timeout: NAVIGATION_TIMEOUT_MS });
    if (new URL(page.url()).origin !== config.approvedOrigin) throw new Error("login_origin_mismatch");
    await page.locator('input[autocomplete="username"]').fill(config.username);
    await page.locator('input[autocomplete="current-password"]').fill(config.password);
    await page.locator('button[type="submit"]').click();
    await page.locator(".layoutShell").waitFor({ state: "attached", timeout: NAVIGATION_TIMEOUT_MS });
    authenticatedShell = true;
    finalPathname = new URL(page.url()).pathname;
    if (finalPathname === "/login" || finalPathname === "/setup") checkIds.push("login_redirect_invalid");
  } catch (_) {
    checkIds.push("login_failed");
  }
  if (pageErrorCount) checkIds.push("login_pageerror");
  page.off("pageerror", onPageError);

  const uniqueCheckIds = [...new Set(checkIds)].sort();
  writeJson(config.outDir, "login.facts.json", {
    schema_version: SCHEMA_VERSION,
    status: uniqueCheckIds.length ? "FAIL" : "PASS",
    check_ids: uniqueCheckIds,
    authenticated_shell: authenticatedShell,
    final_pathname: finalPathname,
    page_error_count: pageErrorCount,
  }, fileIndex);
  return uniqueCheckIds;
}

async function logout(page, config, fileIndex) {
  const checkIds = [];
  let finalPathname = "";
  try {
    await page.locator("button.topNavButton").click();
    await page.waitForURL((url) => url.origin === config.approvedOrigin && url.pathname === "/login", {
      timeout: NAVIGATION_TIMEOUT_MS,
    });
    finalPathname = new URL(page.url()).pathname;
  } catch (_) {
    checkIds.push("logout_failed");
  }
  if (finalPathname !== "/login") checkIds.push("logout_path_invalid");
  const uniqueCheckIds = [...new Set(checkIds)].sort();
  writeJson(config.outDir, "logout.facts.json", {
    schema_version: SCHEMA_VERSION,
    status: uniqueCheckIds.length ? "FAIL" : "PASS",
    check_ids: uniqueCheckIds,
    final_pathname: finalPathname,
  }, fileIndex);
  return uniqueCheckIds;
}

async function runSmoke() {
  const startedAt = isoNow();
  const config = runtimeConfig();
  fs.mkdirSync(config.outDir, { recursive: true });
  const fileIndex = [];
  const failedCheckIds = [];
  let browser = null;
  let globalRedactionSelfTestPassed = false;

  try {
    browser = await chromium.launch({ headless: true });
    const redactionSelfTestChecks = await runGlobalRedactionSelfTest(browser);
    globalRedactionSelfTestPassed = redactionSelfTestChecks.length === 0;
    failedCheckIds.push(...redactionSelfTestChecks);
    failedCheckIds.push(...await runUnauthenticated(browser, config, fileIndex));

    const context = await browser.newContext({ viewport: VIEWPORTS[0] });
    const page = await context.newPage();
    const loginChecks = await login(page, config, fileIndex);
    failedCheckIds.push(...loginChecks);

    if (!loginChecks.length) {
      for (const viewport of VIEWPORTS) {
        for (const route of ROUTES) {
          failedCheckIds.push(...await runRoute(page, route, viewport, config, fileIndex));
        }
      }
      failedCheckIds.push(...await logout(page, config, fileIndex));
    }
    await context.close();
  } catch (_) {
    failedCheckIds.push("browser_smoke_infrastructure_failure");
  } finally {
    if (browser) await browser.close().catch(() => {});
  }

  const uniqueCheckIds = [...new Set(failedCheckIds)].sort();
  const expectedRouteViewportCount = ROUTES.length * VIEWPORTS.length;
  const completedFacts = fileIndex.filter((item) => item.endsWith(".facts.json") && item.startsWith("routes/")).length;
  const summary = {
    schema_version: SCHEMA_VERSION,
    status: uniqueCheckIds.length ? "FAIL" : "PASS",
    started_at: startedAt,
    finished_at: isoNow(),
    route_count: ROUTES.length,
    viewport_count: VIEWPORTS.length,
    expected_route_viewport_count: expectedRouteViewportCount,
    completed_route_viewport_count: completedFacts,
    global_redaction_self_test_passed: globalRedactionSelfTestPassed,
    failed_check_ids: uniqueCheckIds,
    files: [...fileIndex, "summary.json"].sort(),
  };
  writeJson(config.outDir, "summary.json", summary, null);

  if (uniqueCheckIds.length) {
    process.stdout.write(`SMOKE_FAIL ${uniqueCheckIds[0]}\n`);
    process.exitCode = 1;
  } else {
    process.stdout.write("SMOKE_PASS\n");
  }
}

if (process.argv.includes("--self-test")) {
  try {
    runSelfTest();
  } catch (_) {
    process.stdout.write("SELF_TEST_FAIL\n");
    process.exitCode = 1;
  }
} else {
  runSmoke().catch(() => {
    process.stdout.write("SMOKE_FAIL unhandled_smoke_error\n");
    process.exitCode = 1;
  });
}
