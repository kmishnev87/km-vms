FROM node:24.22.2-alpine3.23@sha256:9b69af715c7f9d038150b54ccccbaeffa9064bbc93e2904c29591ca097316af1

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci --no-fund --no-audit

COPY . .

RUN npm run build
RUN cp -r public .next/standalone/public \
    && mkdir -p .next/standalone/.next \
    && cp -r .next/static .next/standalone/.next/static

ENV HOSTNAME=0.0.0.0
ENV PORT=3000

EXPOSE 3000

CMD ["node", ".next/standalone/server.js"]
