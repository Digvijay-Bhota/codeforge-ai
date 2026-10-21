import { useEffect, useState } from "react";

export function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
    } catch {
      // Clipboard unavailable (insecure context or denied): nothing to do.
    }
  }

  return (
    <button type="button" className="button button-ghost button-small" onClick={copy} aria-label={label}>
      {copied ? "Copied" : "Copy"}
    </button>
  );
}
