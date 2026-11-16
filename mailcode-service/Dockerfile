FROM node:20-bookworm-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && npm cache clean --force

COPY server.mjs mailbox.mjs store.mjs web-alias-client.mjs ./

RUN mkdir -p /var/lib/mailcode \
    && chown -R node:node /var/lib/mailcode /app

ENV NODE_ENV=production \
    MAILCODE_HOST=127.0.0.1 \
    MAILCODE_PORT=18787 \
    MAILCODE_DATA_DIR=/var/lib/mailcode

USER node
EXPOSE 18787

CMD ["node", "server.mjs"]
