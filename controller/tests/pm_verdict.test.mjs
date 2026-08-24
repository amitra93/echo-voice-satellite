// Tests for _pmVerdict() in dashboard.jsx — what the wizard concludes from a
// `pm disable` / `pm hide` reply.
//
//     node controller/tests/pm_verdict.test.mjs
//
// Source extraction rather than import, for the reason wifi_scan.test.mjs
// gives: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// The distinction under test is between pm REFUSING a call and pm ANSWERING
// that the package is not installed. Counting only successes made those two
// identical, so a device whose image genuinely lacks the Alexa packages was
// told the package manager had rejected every call and to wait longer and
// retry, which can never help — and the wizard could never be completed (#91).

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8")
  + "\n" + readFileSync(join(HERE, "..", "static", "dashboard_logic.js"), "utf8")
  + "\nfunction _bannerMode(banner) { return bannerMode(banner); }\n";

// Handles both arrow forms in this file: `= (x) => { ... };` with a body, and
// `= (x) => expr || expr;` without one. Scanning for a matching brace only
// works for the first, and silently swallows the following declaration for the
// second, which is how this test first failed with "already declared".
function liftArrow(name) {
  const start = src.indexOf(`const ${name} = (`);
  if (start < 0) {
    throw new Error(`dashboard.jsx no longer defines ${name}() — if it was `
                  + `renamed or moved, update this test to match`);
  }
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    const ch = src[i];
    if (ch === "{" || ch === "(" || ch === "[") depth++;
    else if (ch === "}" || ch === ")" || ch === "]") depth--;
    else if (ch === ";" && depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`could not find the end of ${name}`);
}

const { _pmVerdict, _pmNotReady } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftArrow("_pmVerdict") + "\n" + liftArrow("_pmNotReady")
    + "\nexport { _pmVerdict, _pmNotReady };"
  ).toString("base64"));

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}

// Real output, from a successful run on 2026-08-08.
check("a disabled package is a success",
  _pmVerdict("Package amazon.speech.sim new state: disabled") === "disabled");
check("a hidden package is a success",
  _pmVerdict("Package com.amazon.whad new hidden state: true") === "disabled");

// Real output from #91. pm answered; the package is not installed.
const UNKNOWN =
  "Error: java.lang.IllegalArgumentException: Unknown package: com.amazon.echo.csm.oobe";
check("an unknown package is absent, not a rejection",
  _pmVerdict(UNKNOWN) === "absent", _pmVerdict(UNKNOWN));

// The two shapes of pm not being ready. Both must count as rejections, since
// waiting and retrying IS the right advice for them.
for (const out of [
  "Error: Could not access the Package Manager. Is the system running?",
  "java.lang.NullPointerException: Attempt to invoke interface method "
  + "'int java.util.ArrayList.size()' on a null object reference",
]) {
  check(`not-ready is a rejection: ${out.slice(0, 40)}`,
        _pmVerdict(out) === "rejected", _pmVerdict(out));
  check(`_pmNotReady still matches: ${out.slice(0, 40)}`, _pmNotReady(out));
}

// Anything unrecognised is treated as the BAD case on purpose: the cost of
// being wrong is continuing to WiFi with the Alexa stack live.
for (const out of ["", "Killed", "segfault", "Permission denied"]) {
  check(`unrecognised output is a rejection: ${out || "(empty)"}`,
        _pmVerdict(out) === "rejected", _pmVerdict(out));
}

// An absent package must not be mistaken for not-ready, or the retry path
// fires eleven times against a device that answered correctly every time.
check("absent is not confused with not-ready", !_pmNotReady(UNKNOWN));

if (failures) {
  console.error(`\n${failures} check(s) failed.`);
  process.exit(1);
}
console.log("pm_verdict: all checks passed.");

// ─── Step mode / banner classification ────────────────────────────────────────
//
// Pulling the cable powers the Dot off, and `reboot recovery` is a one-shot,
// so a replug is a cold boot into Android whatever phase the wizard is in.
// Reconnecting during the TWRP phase hands back an Android device that looks
// healthy. In Android `/dev/block/other-boot` points at boot_b, which holds
// amonet's unlock payload, so a retried Patch Boot Image there would write
// over the unlock.

function liftConstObject(name) {
  const start = src.indexOf(`const ${name} = {`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}`);
  const end = src.indexOf("};", start);
  return src.slice(start, end + 2);
}

function liftFunctionDecl(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  let depth = 0, seen = false, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") { depth++; seen = true; }
    else if (src[i] === "}") { depth--; if (seen && depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

const { _bannerMode, _STEP_MODE } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftFunctionDecl("bannerMode") + "\nfunction _bannerMode(banner) { return bannerMode(banner); }\n"
    + liftConstObject("_STEP_MODE")
    + "\nexport { _bannerMode, _STEP_MODE };"
  ).toString("base64"));

// Real banners, from provisioning transcripts.
check("TWRP is recognised", _bannerMode("omni_biscuit") === "twrp",
      _bannerMode("omni_biscuit"));
check("Android is recognised", _bannerMode("csm_biscuit") === "android",
      _bannerMode("csm_biscuit"));

// The ordering trap: "omni_biscuit" contains "biscuit", so an Android-first
// test calls every TWRP device Android — which is the exact direction that
// lets a TWRP step run against Android.
check("TWRP is not mistaken for Android", _bannerMode("omni_biscuit") !== "android");

check("an unknown banner is not guessed at",
      _bannerMode("something_else") === "unknown", _bannerMode("something_else"));
check("an empty banner is not guessed at", _bannerMode("") === "unknown");
check("a missing banner is not guessed at", _bannerMode(undefined) === "unknown");

// The boot-image step must be a TWRP step. If this ever flips, the wizard
// would invite a write to boot_b.
check("patch boot image is a TWRP step", _STEP_MODE[2] === "twrp", _STEP_MODE[2]);
check("connect device is an Android step", _STEP_MODE[0] === "android");
check("verify root is an Android step", _STEP_MODE[7] === "android");

// Every step must have a mode, or the check silently does nothing for it.
for (let i = 0; i <= 12; i++) {
  check(`step ${i} has a mode`, _STEP_MODE[i] === "twrp" || _STEP_MODE[i] === "android",
        String(_STEP_MODE[i]));
}

// ─── Disconnect-shaped errors ─────────────────────────────────────────────────
//
// The in-flight transfer can throw BEFORE the WebUSB disconnect event lands —
// the two race, and on a real run the throw won. That put "Failed to execute
// 'transferOut' on 'USBDevice'" in the transcript as though it were a
// provisioning failure, and then sent the diagnostics probes at a device that
// was no longer plugged in.

const { _isDisconnectError } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftArrow("_isDisconnectError") + "\nexport { _isDisconnectError };"
  ).toString("base64"));

// The real one, from the transcript that prompted this.
check("the observed transferOut error is recognised",
  _isDisconnectError(new Error(
    "Failed to execute 'transferOut' on 'USBDevice': The device was disconnected.")));
check("the transferIn direction is recognised",
  _isDisconnectError(new Error(
    "Failed to execute 'transferIn' on 'USBDevice': The device was disconnected.")));
check("a NetworkError is recognised",
  _isDisconnectError(new Error("NetworkError: A transfer error has occurred.")));

// Provisioning failures must NOT be swallowed as disconnects: taking the
// abandon path on a genuine failure hides the real message and skips the
// diagnostics capture, which is the whole point of #87.
for (const msg of [
  "Hash mismatch — expected 18e46b16b25e… (Magisk-v17.3.zip)",
  "Did not associate to the network within 20s",
  "The package manager rejected 11 of 11 calls and disabled none.",
  "Install verification failed: /data/local/bin/server points to \"\"",
]) {
  check(`a real failure is not treated as a disconnect: ${msg.slice(0, 34)}`,
        !_isDisconnectError(new Error(msg)), msg);
}

check("a missing message does not throw", _isDisconnectError(undefined) === false);
check("an error with no message does not throw", _isDisconnectError({}) === false);
