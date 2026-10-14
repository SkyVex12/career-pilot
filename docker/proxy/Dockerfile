# The Stage 0 edge proxy: nginx with docker/proxy/nginx.conf, listening on port 80 of the
# instance (host networking). Built from the repo root:
#   docker buildx build -f docker/proxy/Dockerfile --platform linux/arm64 -t proxy .
FROM public.ecr.aws/nginx/nginx:1.29-alpine
COPY docker/proxy/nginx.conf /etc/nginx/nginx.conf
RUN nginx -t
