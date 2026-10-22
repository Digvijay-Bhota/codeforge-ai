import { useEffect } from "react";

/** The browser tab's title: "<title> · CodeForge", so tabs and history entries say which page they are. */
export function usePageTitle(title: string | null): void {
  useEffect(() => {
    document.title = title ? `${title} · CodeForge` : "CodeForge";
  }, [title]);
}
