import { api } from "../api/endpoints";
import type { SystemStatus, TaskOverview } from "../api/types";
import { Badge } from "../components/StatusBadge";
import { useApi } from "../lib/useApi";

// What the server's validation setting means for someone about to start a task.
const VALIDATION: Record<string, { label: string; detail: string; ready: boolean }> = {
  disabled: {
    label: "Off",
    detail: "Repository code is never run on this server, so an approved task stops at validation.",
    ready: false,
  },
  host_unsafe: {
    label: "On the worker host",
    detail: "Development only: the repository's tests run without isolation.",
    ready: true,
  },
  docker: { label: "Docker", detail: "The repository's tests run in a hardened, throwaway container.", ready: true },
  gvisor: { label: "gVisor", detail: "The repository's tests run in a container under gVisor.", ready: true },
  sandbox: {
    label: "Sandbox",
    detail: "No approved sandbox is installed yet, so an approved task stops at validation.",
    ready: false,
  },
};

/** For an account with no tasks yet: what this server is set up for, and how a task goes. */
export function GettingStarted({ data }: { data: TaskOverview }) {
  const system = useApi("system-status", (signal) => api.systemStatus(signal));
  return (
    <section className="card getting-started" aria-labelledby="getting-started-title">
      <header className="card-header">
        <div>
          <h2 className="card-title" id="getting-started-title">
            No tasks yet
          </h2>
          <p className="card-subtitle">
            Comment <code>@codeforge</code> followed by what you want done on an issue or pull request in a connected
            repository. The task appears here.
          </p>
        </div>
      </header>

      <div className="getting-started-grid">
        <div>
          <h3 className="getting-started-heading">How a task goes</h3>
          <ol className="flow-steps">
            <li>
              <strong>Request.</strong> Someone comments <code>@codeforge</code> with instructions.
            </li>
            <li>
              <strong>Plan.</strong> CodeForge reads the repository and proposes a plan. Nothing is changed yet.
            </li>
            <li>
              <strong>Plan approval.</strong> A person with write access approves or rejects the plan.
            </li>
            <li>
              <strong>Changes and validation.</strong> The approved plan is implemented and the repository's tests
              run, if this server allows it.
            </li>
            <li>
              <strong>Publication review.</strong> A person reviews the exact changes before anything is published
              as a pull request.
            </li>
          </ol>
          <p className="muted">Each step that changes a repository waits for a person. Approving a plan never publishes.</p>
        </div>

        <div>
          <h3 className="getting-started-heading">This server</h3>
          {system.error && !system.data ? (
            <p className="muted">Couldn't load this server's configuration.</p>
          ) : !system.data ? (
            <p className="muted">Checking this server's configuration…</p>
          ) : (
            <Readiness system={system.data} repositories={data.repositories.length} />
          )}
        </div>
      </div>
    </section>
  );
}

function Readiness({ system, repositories }: { system: SystemStatus; repositories: number }) {
  const validation = VALIDATION[system.test_execution_mode] ?? {
    label: system.test_execution_mode,
    detail: "",
    ready: false,
  };
  return (
    <dl className="readiness">
      <Item
        term="GitHub App"
        ready={system.github_app}
        label={system.github_app ? "Configured" : "Not configured"}
        detail={
          system.github_app
            ? "Repository access is checked with GitHub."
            : 'Repository tasks can\'t be listed, reviewed or run. See "GitHub App credentials" in docs/deployment.md.'
        }
      />
      <Item
        term="Comment webhooks"
        ready={system.github_webhooks}
        label={system.github_webhooks ? "Configured" : "Not configured"}
        detail={
          system.github_webhooks
            ? "@codeforge comments reach this server."
            : "@codeforge comments are not received, so no task can start from GitHub."
        }
      />
      <Item
        term="Your repositories"
        ready={repositories > 0}
        label={repositories > 0 ? `${repositories} you can read` : "None yet"}
        detail={
          repositories > 0
            ? "Registered repositories where you have at least read access."
            : "Install the GitHub App on a repository you have access to; it is listed here once registered."
        }
      />
      <Item term="Validation" ready={validation.ready} label={validation.label} detail={validation.detail} />
    </dl>
  );
}

function Item({ term, ready, label, detail }: { term: string; ready: boolean; label: string; detail: string }) {
  return (
    <div className="readiness-item">
      <dt>{term}</dt>
      <dd>
        <Badge tone={ready ? "success" : "warning"}>{label}</Badge>
        {detail && <span className="readiness-detail">{detail}</span>}
      </dd>
    </div>
  );
}
