#!/usr/bin/env bash
# 线上冒烟验证：首页 / 缓存端点 / 完整往返 / 过滤器
set -u
B=http://101.200.80.105

echo "=== 1. landing page ==="
curl -s -m 10 -o /dev/null -w 'status=%{http_code} type=%{content_type} size=%{size_download}\n' "$B/"

echo "=== 2. landing host substitution ==="
curl -s -m 10 "$B/" | grep -o 'http://101.200.80.105/api/shorten' | head -1

echo "=== 3. cache-stats endpoint (Week3) ==="
curl -s -m 10 "$B/internal/cache-stats"; echo

echo "=== 4. create + redirect roundtrip ==="
R=$(curl -s -m 10 -X POST "$B/api/shorten" -H 'Content-Type: application/json' \
  -d '{"url":"https://github.com/SaberAliceLOL/shortlink"}')
echo "$R"
C=$(printf '%s' "$R" | grep -o '"code":"[^"]*"' | cut -d'"' -f4)
curl -s -m 10 -o /dev/null -w "redirect $C: %{http_code} -> %{redirect_url}\n" "$B/$C"

echo "=== 5. second hit (cache) ==="
curl -s -m 10 -o /dev/null -w 'second hit: %{http_code}\n' "$B/$C"

echo "=== 6. stats after roundtrip ==="
curl -s -m 10 "$B/internal/cache-stats"; echo

echo "=== 7. watermark prefilter (future code) ==="
curl -s -m 10 -o /dev/null -w 'zzzzz: %{http_code}\n' "$B/zzzzz"

echo "=== 8. /docs /health ==="
curl -s -m 10 -o /dev/null -w 'docs: %{http_code}\n' "$B/docs"
curl -s -m 10 -o /dev/null -w 'health: %{http_code}\n' "$B/health"

echo "=== 9. case sensitivity on new cache path ==="
R2=$(curl -s -m 10 -X POST "$B/api/shorten" -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/case-probe"}')
C2=$(printf '%s' "$R2" | grep -o '"code":"[^"]*"' | cut -d'"' -f4)
echo "code=$C2"
U2=$(printf '%s' "$C2" | tr '[:lower:]' '[:upper:]')
if [ "$C2" != "$U2" ]; then
  curl -s -m 10 -o /dev/null -w "UPPER($U2): %{http_code} (expect 404)\n" "$B/$U2"
else
  echo "code has no lowercase letters, skip"
fi
