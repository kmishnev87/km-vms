import test from "node:test";
import assert from "node:assert/strict";
import { androidRelease, fetchAndroidRelease, ANDROID_RELEASE_METADATA } from "../lib/androidRelease.js";
const release = { format: 1, package_name: "ru.kmvms.mobile", published: true, version_name: "0.8.0", version_code: 800, release_tag: "android-v0.8.0", size_bytes: 6500000, sha256: "a".repeat(64), certificate_sha256: "b".repeat(64), min_android_sdk: 26 };
test("official APK URL is derived, never taken from arbitrary metadata", () => {
  assert.equal(androidRelease({ ...release, download_url: "https://other.invalid/app.apk" }).url, "https://github.com/kmishnev87/km-vms/releases/download/android-v0.8.0/km-vms-android-0.8.0.apk");
});
test("unpublished APK must not become a false download", () => assert.equal(androidRelease({ format: 1, package_name: "ru.kmvms.mobile", published: false }), null));
test("rejects malformed release identity and oversized artifacts", () => {
  for (const field of [{ release_tag: "../../latest" }, { version_name: "bad" }, { size_bytes: 300 * 1024 * 1024 }, { sha256: "bad" }, { package_name: "other" }]) assert.throws(() => androidRelease({ ...release, ...field }));
});
test("an absent first publication is not a failed connection and never sends NAS credentials", async () => {
  const previous=globalThis.fetch;
  try {
    globalThis.fetch=async(url,options)=>{
      assert.equal(url,ANDROID_RELEASE_METADATA);assert.equal(options.credentials,"omit");assert.equal(options.redirect,"error");
      assert.equal(options.headers,undefined);return new Response("",{status:404});
    };
    assert.equal(await fetchAndroidRelease(new AbortController().signal),null);
  }finally{globalThis.fetch=previous;}
});
test("metadata reading is bounded even when the public server omits content length", async () => {
  const previous=globalThis.fetch;
  try {
    globalThis.fetch=async()=>new Response("x".repeat(65537),{status:200});
    await assert.rejects(()=>fetchAndroidRelease(new AbortController().signal),/invalid_release/);
  }finally{globalThis.fetch=previous;}
});
