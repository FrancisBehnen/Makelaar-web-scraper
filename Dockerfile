FROM oven/bun:1

WORKDIR /app

COPY package.json bun.lock ./

RUN bun install --frozen-lockfile

COPY . .

RUN mkdir -p data

ENV NODE_ENV=production

ENTRYPOINT ["bun", "src/app.ts"]
