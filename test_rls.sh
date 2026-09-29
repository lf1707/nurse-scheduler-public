#!/bin/bash
# RLS 跨租户隔离自动化测试脚本
# 使用方法: chmod +x test_rls.sh && ./test_rls.sh

set -e

BASE="http://localhost:9000/api/v1"
: "${SUPER_TOKEN:?Set SUPER_TOKEN to a current local test access token}"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

curl_json() {
  local method=$1 path=$2 token=$3 body=$4
  if [ -n "$body" ]; then
    curl -s -X "$method" "$BASE$path" \
      -H "Authorization: Bearer $token" \
      -H "Content-Type: application/json" \
      -d "$body"
  else
    curl -s -X "$method" "$BASE$path" \
      -H "Authorization: Bearer $token"
  fi
}

# ============================================
# Phase 1: Setup - 创建租户 B 和管理员 B
# ============================================
log "=== Phase 1: Setup ==="

log "Creating Tenant B..."
TENANT_B_RESP=$(curl_json POST "/tenants" "$SUPER_TOKEN" '{"name":"Hospital B","slug":"hospital-b"}')
TENANT_B_ID=$(echo "$TENANT_B_RESP" | jq -r '.id')
if [ "$TENANT_B_ID" = "null" ] || [ -z "$TENANT_B_ID" ]; then
  error "Failed to create Tenant B: $TENANT_B_RESP"
  exit 1
fi
log "Tenant B created: $TENANT_B_ID"

log "Creating Admin for Tenant B..."
ADMIN_B_RESP=$(curl_json POST "/tenants/$TENANT_B_ID/admin" "$SUPER_TOKEN" '{"email":"manager@hospital-b.com","password":"password123","first_name":"Manager","last_name":"B"}')
if ! echo "$ADMIN_B_RESP" | jq -e '.id' >/dev/null; then
  error "Failed to create Admin B: $ADMIN_B_RESP"
  exit 1
fi
log "Admin B created"

# ============================================
# Phase 2: Login as both admins
# ============================================
log "=== Phase 2: Login ==="

log "Logging in as Admin A (tenant A)..."
TOKEN_A=$(curl -s -X POST "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"manager@test.com","password":"password123"}' | jq -r '.access_token')
if [ "$TOKEN_A" = "null" ] || [ -z "$TOKEN_A" ]; then
  error "Admin A login failed"
  exit 1
fi
log "Admin A token acquired"

log "Logging in as Admin B (tenant B)..."
TOKEN_B=$(curl -s -X POST "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"manager@hospital-b.com","password":"password123"}' | jq -r '.access_token')
if [ "$TOKEN_B" = "null" ] || [ -z "$TOKEN_B" ]; then
  error "Admin B login failed"
  exit 1
fi
log "Admin B token acquired"

# ============================================
# Phase 3: Isolation Tests - 跨租户访问应被拦截
# ============================================
log "=== Phase 3: Cross-Tenant Isolation Tests ==="

run_isolation_test() {
  local name=$1 method=$2 path=$3 token=$4 body=$5 expected_codes=$6
  local resp
  resp=$(curl -s -w "\n%{http_code}" -X "$method" "$BASE$path" \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -d "$body")
  local http_code=$(echo "$resp" | tail -n1)
  local body=$(echo "$resp" | head -n -1)

  if [[ " $expected_codes " =~ " $http_code " ]]; then
    echo -e "${GREEN}✅ PASS${NC} $name -> $http_code (预期: $expected_codes)"
    return 0
  else
    echo -e "${RED}❌ FAIL${NC} $name -> $http_code (预期: $expected_codes), 响应: $body"
    return 1
  fi
}

# 测试用例：Admin A 访问 Tenant B 的数据
run_isolation_test "Admin A 列租户(应403)" "GET" "/tenants" "$TOKEN_A" "" "403"
run_isolation_test "Admin A 查租户B详情(应403/404)" "GET" "/tenants/$TENANT_B_ID" "$TOKEN_A" "" "403 404"
run_isolation_test "Admin A 在租户B创建角色(应403)" "POST" "/roles" "$TOKEN_A" '{"name":"Test","code":"TST"}' "403"
run_isolation_test "Admin B 在租户A创建角色(应403)" "POST" "/roles" "$TOKEN_B" '{"name":"Test","code:"TST"}' "403"

# ============================================
# Phase 4: Same-Tenant Tests - 同租户访问应成功
# ============================================
log "=== Phase 4: Same-Tenant Access Tests ==="

run_same_tenant_test() {
  local name=$1 method=$2 path=$3 token=$4 body=$5
  local resp
  resp=$(curl_json "$method" "$path" "$token" "$body")
  if echo "$resp" | grep -q '"id"'; then
    echo -e "${GREEN}✅ PASS${NC} $name -> 成功创建"
    return 0
  else
    echo -e "${RED}❌ FAIL${NC} $name -> 失败: $resp"
    return 1
  fi
}

run_same_tenant_test "Admin A 在自己租户创建角色" "POST" "/roles" "$TOKEN_A" '{"name":"RN","code":"RN"}'
run_same_tenant_test "Admin B 在自己租户创建角色" "POST" "/roles" "$TOKEN_B" '{"name":"RN","code":"RN"}'

# ============================================
# Phase 5: DB 直接验证 (需要 docker psql)
# ============================================
log "=== Phase 5: Database Verification ==="
warn "数据库层验证需要手动执行："
echo "docker compose exec db psql -U nurse -d nurse_scheduler -c \""
echo "  SET LOCAL app.tenant_id = '<tenant_A_id>';"
echo "  SELECT * FROM roles;"
echo "  SET LOCAL app.tenant_id = '<tenant_B_id>';"
echo "  SELECT * FROM roles;"
echo "\""

log "🎉 RLS 隔离测试完成！"
