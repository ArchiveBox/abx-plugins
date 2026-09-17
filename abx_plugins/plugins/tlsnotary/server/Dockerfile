FROM node:24.18.0-trixie-slim
WORKDIR /app
COPY server/package.json server/package-lock.json ./
RUN npm ci --omit=dev
COPY server/server.mjs ./
COPY web ./web
CMD ["node", "server.mjs"]
