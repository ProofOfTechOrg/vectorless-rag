FROM node:22.23-alpine AS dependencies
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json ./apps/web/package.json
RUN pnpm install --frozen-lockfile --filter @vectorless-rag/web...

FROM dependencies AS source
COPY --chown=node:node apps/web ./apps/web

FROM source AS development
ENV NODE_ENV=development HOST=0.0.0.0 PORT=3001
USER node
EXPOSE 3001
CMD ["pnpm", "--filter", "@vectorless-rag/web", "exec", "vite", "dev", "--host", "0.0.0.0", "--port", "3001"]

FROM source AS build
RUN pnpm --filter @vectorless-rag/web build \
    && pnpm --filter @vectorless-rag/web production-smoke

FROM node:22.23-alpine AS runtime
ENV NODE_ENV=production HOST=0.0.0.0 PORT=3001
WORKDIR /app
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/apps/web/node_modules ./apps/web/node_modules
COPY --from=build --chown=node:node /app/apps/web/package.json ./apps/web/package.json
COPY --from=build --chown=node:node /app/apps/web/dist ./apps/web/dist
COPY --from=build --chown=node:node /app/apps/web/scripts/verify-production-assets.mjs ./apps/web/scripts/verify-production-assets.mjs
USER node
EXPOSE 3001
HEALTHCHECK --interval=10s --timeout=5s --retries=6 CMD ["node", "apps/web/scripts/verify-production-assets.mjs", "http://127.0.0.1:3001"]
CMD ["apps/web/node_modules/.bin/srvx", "serve", "--prod", "--static=../client", "--entry=apps/web/dist/server/server.js"]
