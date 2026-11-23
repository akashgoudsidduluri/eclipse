/** Regression tests for formatFileSize in src/hooks.ts (PR #7:
 * the chat file chips show human sizes, never raw bytes).
 *
 * Run with `npm test` — the pretest script compiles hooks.ts
 * into tests-dist/, mirroring hooks.test.mjs. No test
 * framework: node --test + node:assert only. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { formatFileSize } from "../tests-dist/hooks.js";

test("bytes stay bytes under 1 KB", () => {
  assert.equal(formatFileSize(0), "0 B");
  assert.equal(formatFileSize(842), "842 B");
  assert.equal(formatFileSize(1023), "1023 B");
});

test("kilobytes round to whole KB", () => {
  assert.equal(formatFileSize(1024), "1 KB");
  assert.equal(formatFileSize(12 * 1024), "12 KB");
  assert.equal(formatFileSize(1536), "2 KB");
});

test("megabytes keep one decimal under 10 MB", () => {
  assert.equal(formatFileSize(2.4 * 1024 * 1024), "2.4 MB");
  assert.equal(formatFileSize(10 * 1024 * 1024), "10 MB");
});

test("bad input renders a dash, never NaN", () => {
  assert.equal(formatFileSize(-1), "—");
  assert.equal(formatFileSize(NaN), "—");
  assert.equal(formatFileSize(Infinity), "—");
});
