# The Stage 0 edge proxy: nginx with docker/proxy/nginx.conf, listening on port 80 of the
# instance (host networking). Built from the repo root:
#   docker buildx build -f docker/proxy/Dockerfile --platform linux/arm64 -t proxy .
# Docker Hub's official image through Google's mirror: public.ecr.aws rate-limits GitHub's
# shared runners (429 "Data limit exceeded"), and Docker Hub limits anonymous pulls.
FROM mirror.gcr.io/library/nginx:1.29-alpine
COPY docker/proxy/nginx.conf /etc/nginx/nginx.conf
RUN nginx -t
