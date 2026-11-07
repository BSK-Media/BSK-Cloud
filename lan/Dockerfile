# Caddy z modułem DNS Cloudflare — certyfikat Let's Encrypt dla cloud.bskmedia.pl
# pobierany przez rekord DNS (bez otwierania portów), do pracy w sieci biurowej.
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
