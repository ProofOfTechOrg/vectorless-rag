FROM node:22.23-alpine AS build
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json ./apps/web/package.json
RUN pnpm install --frozen-lockfile --filter @vectorless-rag/web...
COPY apps/web ./apps/web
RUN pnpm --filter @vectorless-rag/web build

FROM node:22.23-alpine AS runtime
ENV NODE_ENV=production HOST=0.0.0.0 PORT=3001
WORKDIR /app
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/apps/web/node_modules ./apps/web/node_modules
COPY --from=build --chown=node:node /app/apps/web/package.json ./apps/web/package.json
COPY --from=build --chown=node:node /app/apps/web/dist ./apps/web/dist
USER node
EXPOSE 3001
HEALTHCHECK --interval=10s --timeout=5s --retries=6 CMD node -e "fetch('http://127.0.0.1:3001/healthz').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"
CMD ["apps/web/node_modules/.bin/srvx", "serve", "--prod", "--static=../client", "--entry=apps/web/dist/server/server.js"]
