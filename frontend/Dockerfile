# Honeycomb frontend: Next.js 14, built once and served by `next start`.
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# npm ci, not install: the lockfile is the build's input, and `install` would
# silently resolve something newer than what was tested.
RUN npm ci

FROM node:20-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Baked into the bundle at build time, so it cannot be changed by a restart.
ARG NEXT_PUBLIC_API_BASE
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:20-alpine AS run
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
# Only what serving needs. node_modules comes from the build stage rather than
# a fresh `npm ci --omit=dev`, so the running image contains exactly the tree
# the bundle was built against.
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY --from=build /app/public ./public
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/next.config.mjs ./next.config.mjs
EXPOSE 3000
CMD ["sh", "-c", "npm run start -- --port ${PORT:-3000} --hostname 0.0.0.0"]
