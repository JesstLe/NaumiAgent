# syntax=docker/dockerfile:1.7

FROM node:24-alpine AS builder

WORKDIR /build/frontend

RUN corepack enable && corepack prepare pnpm@10.12.4 --activate

COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
COPY frontend/shared/package.json shared/package.json
COPY frontend/web/package.json web/package.json
COPY frontend/web2/package.json web2/package.json

RUN pnpm install --frozen-lockfile

COPY frontend/ ./

RUN pnpm build

FROM nginx:1.29-alpine

COPY docker/public/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /build/frontend/web/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1
