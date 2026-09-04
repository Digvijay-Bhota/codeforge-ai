# docs/security/

This directory contains security policies and documentation for CodeForge AI.

## Phase 0 Security Rules

- No secrets are committed to source control.
- `.env` is listed in `.gitignore`.
- Credentials in `.env.example` are local-development placeholders only.
- No autonomous code execution is implemented.
- No GitHub write access is implemented.
- No automatic merging or deployment mechanisms exist.

## Future Considerations

- Secret management via Vault or cloud-native secrets managers.
- Sandboxed execution with strict resource limits.
- Human approval gates before any repository write operation.
- Audit logging for all agent actions.
- Rate limiting and authentication on all API endpoints.
