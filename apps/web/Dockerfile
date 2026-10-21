# The review console for the local Compose stack: built from the committed
# lockfile, served by an unprivileged nginx that forwards /api/ to the API.
# No build arguments and no secrets: the bundle is the same for every user.

FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
# Lockfile only; no dependency needs an install script (see .github/workflows/web.yml).
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY index.html tsconfig.json vite.config.ts eslint.config.js ./
COPY public/ public/
COPY src/ src/
# Type-checks, then builds dist/ with the Content-Security-Policy meta tag.
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.28-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
# Healthy when nginx answers and serves the built app shell.
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz \
        && wget -q -O - http://127.0.0.1:8080/ | grep -q 'id="root"' || exit 1
