import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
const source = fs.readFileSync(new URL("../components/cameras/CameraDeviceSettings.js", import.meta.url), "utf8");
const requestBody = source.slice(source.indexOf("  async function request("), source.indexOf("  async function loadOnvifProfileConfig"));
for (const cameraId of [null, 7]) for (const endpoint of ["/draft-endpoint", "", null]) {
  const calls = [];
  const ctx = {cameraId, form: {host: "camera.invalid", port: "8080", username: "dummy", password: "", onvif_path: endpoint},
    apiFetch: async (path, options) => {calls.push({path, ...JSON.parse(options.body)}); return {}; }};
  vm.runInNewContext(requestBody + "\nthis.request = request;", ctx);
  await ctx.request("/cameras/onvif/profile_config", "main");
  await ctx.request("/cameras/onvif/update_profile", "sub", {config: {fps: 15}});
  for (const call of calls) {
    assert.equal(call.onvif_path, endpoint ?? "");
    assert.equal(call.camera_id, cameraId);
    assert.equal(call.port, 8080);
    assert.equal(call.password, "");
  }
  assert.equal(calls[0].profile_token, "main");
  assert.equal(calls[1].profile_token, "sub");
  assert.equal(calls[1].config.fps, 15);
}
console.log("Draft ONVIF endpoint reaches read and write requests without changing the saved camera PASS");
