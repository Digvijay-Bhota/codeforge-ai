import { useState } from "react";

// Repository content and agent output are untrusted: everything here renders
// as text nodes, never as markup.

const LINE_LIMIT = 2000;

function useLineLimit(lines: string[]) {
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? lines : lines.slice(0, LINE_LIMIT);
  const hidden = lines.length - visible.length;
  return { visible, hidden, showAll: () => setShowAll(true) };
}

function MoreLines({ hidden, onShow }: { hidden: number; onShow: () => void }) {
  if (hidden <= 0) return null;
  return (
    <div className="code-more">
      <button type="button" className="button button-secondary button-small" onClick={onShow}>
        Show {hidden.toLocaleString()} more lines
      </button>
    </div>
  );
}

function splitLines(text: string): string[] {
  const lines = text.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/** The exact content that will be published, with line numbers. */
export function CodeView({ content, label }: { content: string; label: string }) {
  const { visible, hidden, showAll } = useLineLimit(splitLines(content));
  return (
    <div className="code-frame">
      <pre className="code" tabIndex={0} aria-label={label}>
        <code>
          {visible.map((line, index) => (
            <span className="code-line" key={index}>
              <span className="code-gutter" aria-hidden="true">
                {index + 1}
              </span>
              <span className="code-text">{line || " "}</span>
            </span>
          ))}
        </code>
      </pre>
      <MoreLines hidden={hidden} onShow={showAll} />
    </div>
  );
}

function diffClass(line: string): string {
  if (line.startsWith("@@")) return "diff-hunk";
  if (line.startsWith("+++") || line.startsWith("---")) return "diff-header";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  return "diff-context";
}

/** A unified diff, colored by line kind. */
export function DiffView({ patch, label }: { patch: string; label: string }) {
  const { visible, hidden, showAll } = useLineLimit(splitLines(patch));
  return (
    <div className="code-frame">
      <pre className="code code-diff" tabIndex={0} aria-label={label}>
        <code>
          {visible.map((line, index) => (
            <span className={`code-line ${diffClass(line)}`} key={index}>
              <span className="code-text">{line || " "}</span>
            </span>
          ))}
        </code>
      </pre>
      <MoreLines hidden={hidden} onShow={showAll} />
    </div>
  );
}

/** Free text from the agent (PR body, commit message): wrapped, never interpreted. */
export function TextBlock({ text, label }: { text: string; label: string }) {
  return (
    <pre className="text-block" tabIndex={0} aria-label={label}>
      {text || "(empty)"}
    </pre>
  );
}
