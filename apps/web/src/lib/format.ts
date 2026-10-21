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

function clip(line: string): string {
  return line.length > 140 ? `${line.slice(0, 137)}…` : line;
}

// The request the API writes for an @codeforge comment
// (apps/api/app/github/command_consumer.py, build_task_description):
//   Execute @codeforge <action> for issue #<n>.
//   Title: <issue title>            (optional)
//   Arguments:                      (optional)
//   <the commenter's instructions>
const COMMENT_REQUEST = /^Execute (@codeforge [a-z]+) for (?:issue|pull request) #\d+\.$/;

/** The @codeforge command a comment task runs ("@codeforge implement"), or null for other tasks. */
export function taskCommand(requested: string): string | null {
  return COMMENT_REQUEST.exec(requested.trim().split("\n")[0] ?? "")?.[1] ?? null;
}

/**
 * A title for a task request.  For a comment task, the commenter's
 * instructions (joined, then clipped), else the issue title, else the command; every
 * comment task otherwise starts with the same "Execute @codeforge ..." line.
 * For any other request, its first line.
 */
export function taskTitle(requested: string): string {
  const lines = requested.trim().split("\n");
  const command = taskCommand(requested);
  if (command) {
    const args = lines.indexOf("Arguments:");
    // Comments wrap lines by hand; read the instructions as one sentence.
    const instruction =
      args >= 0 ? lines.slice(args + 1).map((line) => line.trim()).filter(Boolean).join(" ") || undefined : undefined;
    const issueTitle = lines.find((line) => line.startsWith("Title: "))?.slice("Title: ".length).trim();
    return clip((instruction ?? issueTitle ?? command).trim());
  }
  return clip(lines[0] ?? "") || "Untitled task";
}

const GITHUB_REPOSITORY = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

/** The GitHub URL of the issue or pull request a task came from, for a well-formed repository name only. */
export function githubOriginUrl(
  repository: string | null,
  origin: { issue_number: number | null; pull_request_number: number | null } | null | undefined,
): string | null {
  if (!repository || !origin || !GITHUB_REPOSITORY.test(repository)) return null;
  if (origin.pull_request_number) return `https://github.com/${repository}/pull/${origin.pull_request_number}`;
  if (origin.issue_number) return `https://github.com/${repository}/issues/${origin.issue_number}`;
  return null;
}

/** "Issue #2" or "Pull request #7". */
export function githubOriginLabel(origin: { issue_number: number | null; pull_request_number: number | null }): string {
  return origin.pull_request_number ? `Pull request #${origin.pull_request_number}` : `Issue #${origin.issue_number ?? "?"}`;
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
