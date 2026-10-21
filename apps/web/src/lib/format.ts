const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
const absolute = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
  ["second", 1],
];

function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatRelative(value: string | null | undefined, now = Date.now()): string {
  const date = parse(value);
  if (!date) return "—";
  const seconds = Math.round((date.getTime() - now) / 1000);
  if (Math.abs(seconds) < 45) return "just now";
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return relative.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

export function formatAbsolute(value: string | null | undefined): string {
  const date = parse(value);
  return date ? absolute.format(date) : "—";
}

/** "2 h 5 min" until *value*, or null once it has passed. */
export function formatTimeLeft(value: string | null | undefined, now = Date.now()): string | null {
  const date = parse(value);
  if (!date) return null;
  const minutes = Math.floor((date.getTime() - now) / 60000);
  if (minutes < 0) return null;
  if (minutes < 1) return "less than a minute";
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const mins = minutes % 60;
  if (days > 0) return `${days} d ${hours} h`;
  if (hours > 0) return `${hours} h ${mins} min`;
  return `${mins} min`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}

export function shortId(id: string): string {
  return id.split("-")[0] ?? id;
}

/** The first line of a task request, as a title. */
export function taskTitle(requested: string): string {
  const first = requested.trim().split("\n")[0] ?? "";
  return first.length > 140 ? `${first.slice(0, 137)}…` : first || "Untitled task";
}

/** Only links to GitHub pull requests are rendered as links. */
export function safePullRequestUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && parsed.hostname === "github.com" ? parsed.toString() : null;
  } catch {
    return null;
  }
}
