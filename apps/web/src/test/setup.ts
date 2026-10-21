import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  // DOM teardown, skipped in node-environment tests (no window).
  if (typeof window === "undefined") return;
  cleanup();
  window.sessionStorage.clear();
});
