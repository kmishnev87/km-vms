import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { inflateSync } from "node:zlib";
import { readSettingsMaintenanceSources } from "./helpers/readSettingsMaintenanceSources.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const readText = (relative) => fs.readFileSync(resolve(webRoot, relative), "utf8");
const readBinary = (relative) => fs.readFileSync(resolve(webRoot, relative));

function decodeRgbaPng(relative) {
  const png = readBinary(relative);
  assert.deepEqual([...png.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10], `${relative} must be PNG`);

  let offset = 8;
  let width = 0;
  let height = 0;
  let bitDepth = 0;
  let colorType = 0;
  let interlace = 0;
  const idat = [];
  while (offset < png.length) {
    const length = png.readUInt32BE(offset);
    const type = png.toString("ascii", offset + 4, offset + 8);
    const data = png.subarray(offset + 8, offset + 8 + length);
    if (type === "IHDR") {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      bitDepth = data[8];
      colorType = data[9];
      interlace = data[12];
    } else if (type === "IDAT") {
      idat.push(data);
    } else if (type === "IEND") {
      break;
    }
    offset += 12 + length;
  }

  assert.equal(width, 128, `${relative} width`);
  assert.equal(height, 128, `${relative} height`);
  assert.equal(bitDepth, 8, `${relative} bit depth`);
  assert.equal(colorType, 6, `${relative} must be RGBA`);
  assert.equal(interlace, 0, `${relative} must be non-interlaced`);

  const bytesPerPixel = 4;
  const stride = width * bytesPerPixel;
  const raw = inflateSync(Buffer.concat(idat));
  assert.equal(raw.length, height * (stride + 1), `${relative} scanline size`);
  const pixels = Buffer.alloc(height * stride);
  let rawOffset = 0;
  for (let y = 0; y < height; y += 1) {
    const filter = raw[rawOffset];
    rawOffset += 1;
    const rowOffset = y * stride;
    const previousOffset = (y - 1) * stride;
    for (let x = 0; x < stride; x += 1) {
      const encoded = raw[rawOffset + x];
      const left = x >= bytesPerPixel ? pixels[rowOffset + x - bytesPerPixel] : 0;
      const up = y > 0 ? pixels[previousOffset + x] : 0;
      const upLeft = y > 0 && x >= bytesPerPixel ? pixels[previousOffset + x - bytesPerPixel] : 0;
      let value;
      if (filter === 0) value = encoded;
      else if (filter === 1) value = (encoded + left) & 0xff;
      else if (filter === 2) value = (encoded + up) & 0xff;
      else if (filter === 3) value = (encoded + Math.floor((left + up) / 2)) & 0xff;
      else if (filter === 4) {
        const estimate = left + up - upLeft;
        const leftDistance = Math.abs(estimate - left);
        const upDistance = Math.abs(estimate - up);
        const upLeftDistance = Math.abs(estimate - upLeft);
        const predictor = leftDistance <= upDistance && leftDistance <= upLeftDistance
          ? left
          : upDistance <= upLeftDistance
            ? up
            : upLeft;
        value = (encoded + predictor) & 0xff;
      } else {
        assert.fail(`${relative} uses unsupported PNG filter ${filter}`);
      }
      pixels[rowOffset + x] = value;
    }
    rawOffset += stride;
  }

  let transparent = 0;
  let visible = 0;
  for (let index = 3; index < pixels.length; index += bytesPerPixel) {
    if (pixels[index] === 0) transparent += 1;
    if (pixels[index] > 0) visible += 1;
  }
  assert.ok(transparent > 1_000, `${relative} must have transparent canvas`);
  assert.ok(visible > 1_000, `${relative} must contain visible artwork`);
  assert.equal(pixels[3], 0, `${relative} top-left corner must be transparent`);
}

for (const icon of ["open.png", "operation-history.png", "add-storage-location.png"]) {
  decodeRgbaPng(`public/assets/icons/ui/${icon}`);
}

for (const [icon, requiredGeometry] of [
  ["backup-create.svg", 'circle cx="99" cy="91" r="21"'],
  ["update-check.svg", 'circle cx="91" cy="91" r="17"'],
  ["download-report.svg", 'circle cx="91" cy="91" r="23"'],
]) {
  const svg = readText(`public/assets/icons/ui/${icon}`);
  assert.match(svg, /<svg[^>]+viewBox="0 0 128 128"/);
  assert.ok(svg.includes(requiredGeometry), `${icon} must keep approved geometry`);
  assert.match(svg, /<feDropShadow/);
}

const storagePage = readText("app/storage/page.js");
const settingsPage = readSettingsMaintenanceSources();
const center = readText("components/storage/ArchiveManagementCenter.js");
const storageCss = readText("app/styles/40-storage-records-shared.css");
const settingsCss = readText("app/styles/20-settings-maintenance.css");
const responsiveCss = readText("app/styles/60-responsive-shared.css");

assert.match(center, /operation-history\.png/);
assert.doesNotMatch(center, /historyCount/);
assert.doesNotMatch(center, /aria-hidden="true">\{historyCount\}/);

assert.match(storagePage, /href="\/cameras"[\s\S]*\/assets\/icons\/ui\/camera\.png/);
assert.match(storagePage, /archiveManagementAddLocation[\s\S]*add-storage-location\.png/);
assert.match(storagePage, /storageOpsRootAddButton[\s\S]*add-storage-location\.png/);
assert.match(storagePage, /className="storageOpsTopMetricValueButton"[\s\S]*onValueClick=\{\(\) => openIntegrityDialog\(\)\}/);
assert.doesNotMatch(storagePage.slice(storagePage.indexOf("<section className={`storageOpsOverview"), storagePage.indexOf("{refreshWarning ?")), /storageOpsHealthAction|\/assets\/icons\/ui\/open\.png/);
assert.match(storagePage, /onClick=\{openMigrationDialog\}[\s\S]*\/assets\/icons\/ui\/open\.png/);
assert.match(storagePage, /appIllustratedActionGlyph[\s\S]*↻/);
assert.equal(storagePage.includes("/assets/icons/ui/refresh.png"), false, "refresh must reuse the existing glyph pattern");

const securityLabel = settingsPage.indexOf("<label>{t.security}");
const settingsCardsStart = settingsPage.lastIndexOf('<div className="settingsRow">', securityLabel);
const settingsCardsEnd = settingsPage.indexOf("</section>", securityLabel);
const settingsCards = settingsPage.slice(settingsCardsStart, settingsCardsEnd);
assert.equal((settingsCards.match(/\/assets\/icons\/ui\/open\.png/g) || []).length, 3);
assert.equal((settingsCards.match(/appIllustratedAction/g) || []).length, 3);
assert.match(settingsCards, /onClick=\{\(\) => setSecurityModalOpen\(true\)\}/);
assert.match(settingsCards, /ref=\{maintenanceController\.maintenanceTriggerRef\}/);
assert.match(settingsCards, /onClick=\{maintenanceController\.openMaintenanceModal\}/);
assert.match(settingsCards, /onClick=\{openUsersModal\}/);
assert.doesNotMatch(settingsCards, />\s*\{t\.open\}\s*</);
assert.match(settingsCss, /\.settingsRowControlMeta\s*\{[\s\S]*justify-items:\s*center;/);

assert.match(storageCss, /\.button\.appIllustratedAction,[\s\S]*width:\s*40px;[\s\S]*height:\s*40px;/);
assert.match(storageCss, /\.appIllustratedAction > img[\s\S]*width:\s*32px;[\s\S]*height:\s*32px;/);
assert.match(storageCss, /\.archiveManagementRowTitle[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\) auto;/);
assert.match(storageCss, /\.archiveManagementRow \.storageOpsStatusPill[\s\S]*justify-self:\s*end;/);
assert.match(storageCss, /--storage-root-action-inset:\s*calc\(var\(--storage-shared-action-edge, 16px\) - var\(--storage-root-horizontal-margin, 14px\)\)/);
assert.match(storageCss, /--storage-root-horizontal-margin:\s*14px;[\s\S]*--storage-shared-action-edge:\s*16px;/);
assert.match(storageCss, /@media \(max-width: 520px\)[\s\S]*\.archiveManagementRowTitle[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\);/);
assert.match(responsiveCss, /\.storageOpsRootAddButton\.appIllustratedAction[\s\S]*justify-self:\s*end;/);

const supportStart = settingsPage.indexOf('<div className="settingsMaintenanceSupportActions">');
const supportEnd = settingsPage.indexOf("</div>", supportStart);
const supportActions = settingsPage.slice(supportStart, supportEnd);
assert.equal((supportActions.match(/settingsMaintenanceSupportActionButton/g) || []).length, 1);
assert.match(supportActions, /download-report\.svg/);
assert.match(supportActions, /onOpenDiagnosticChoice/);
assert.match(settingsCss, /\.settingsMaintenanceSupportActions\s*\{[\s\S]*grid-template-columns:\s*40px;[\s\S]*width:\s*40px;[\s\S]*min-width:\s*40px;/);
assert.match(settingsCss, /\.settingsMaintenanceSupportActions \.settingsMaintenanceSupportActionButton\s*\{[\s\S]*width:\s*40px;[\s\S]*height:\s*40px;/);

const maintenanceOverview = settingsPage.slice(
  settingsPage.indexOf('<section className="settingsUpdateApplyPanel">'),
  settingsPage.indexOf('<section className="settingsMaintenanceBackupDetail">'),
);
assert.equal((maintenanceOverview.match(/update-check\.svg/g) || []).length, 1);
assert.equal((maintenanceOverview.match(/download-report\.svg/g) || []).length, 1);
assert.equal((settingsPage.match(/backup-create\.svg/g) || []).length, 2);
assert.match(maintenanceOverview, /maintenanceBackupOpenList[\s\S]*\/assets\/icons\/ui\/open\.png/);

console.log("Approved archive action icons and compact UI composition: PASS");
