# Honeycomb frontend: Next.js 14, built and served from one stage.
#
# Deliberately NOT multi-stage. Coolify rewrites this file before building --
# it injected 45 ARG declarations across three stages -- and the rewritten
# result lost `/app/.next` between the build stage and the runtime stage, so
# `COPY --from=build` failed on a directory the build had just produced.
# One stage has nothing to copy across and cannot fail that way. The cost is a
# larger image; the benefit is a build that survives being rewritten.
FROM node:20-alpine

WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

# npm ci, not install: the lockfile is the build's input, and `install` would
# quietly resolve something newer than what was tested.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Baked into the bundle at build time, so it cannot be changed by a restart --
# the browser calls the API directly, so this is a public origin.
# The name lib/api.ts actually reads, and it must carry the /api suffix --
# the fallback in that file is "http://localhost:8000/api", so getting the
# name wrong does not fail the build, it ships a bundle that asks the
# visitor's own machine for the API.
ARG NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL

RUN npm run build

ENV NODE_ENV=production
EXPOSE 3000
CMD ["sh", "-c", "npm run start -- --port ${PORT:-3000} --hostname 0.0.0.0"]
