# SaaS化 L2 鉴权改造 — 完成报告

**时间**: 2026-07-23 20:13~20:20  
**范围**: 竞品分析系统 Phase 10 后遗漏工作修复 — L2 鉴权体系全栈实现

---

## 改动清单（6 个文件，~520 行变更）

| 文件 | 操作 | 说明 |
|------|------|------|
| `pyproject.toml` | 改 | 加 PyJWT + passlib[bcrypt] 依赖 |
| `.env.example` | 改 | 加 JWT_SECRET + JWT_EXPIRE_HOURS 配置 |
| `src/config.py` | 改 | Settings 加 jwt_secret + jwt_expire_hours 字段 |
| `docker/docker-compose.yml` | 改 | 端口 5432:5432 → 5433:5432（对齐 .env PG_PORT=5433） |
| `src/db/schema.sql` | 改 | 新增 users 表（#0 号表，含完整 L3/L4 注释） |
| `src/db/dao.py` | 改 | 追加 UserDAO（create / get_by_username / get_by_id） |
| `src/db/__init__.py` | 改 | 导出 UserDAO |
| `src/auth/__init__.py` | **新建** | 模块导出 |
| `src/auth/security.py` | **新建** | bcrypt哈希 + JWT签发/验证（~100行，全中文注释） |
| `src/auth/dependencies.py` | **新建** | FastAPI Depends 鉴权中间件（CurrentUser 注入） |
| `src/api/auth_routes.py` | **新建** | POST /api/auth/register + login + GET /me |
| `src/api/routes.py` | 改 | 注册 auth_router + StaticFiles + create_task 注入 JWT |
| `static/login.html` | **新建** | 登录/注册页面（单文件 HTML/CSS/JS，暗色主题） |

## 关键技术决策

1. **PyJWT** 而非 python-jose → 后者已停维（2022）
2. **bcrypt** 而非 scrypt/argon2 → 够用+passlib一等公民
3. **HS256 算法锁定** → jwt.decode(algorithms=["HS256"]) 防伪造 "none" 算法绕过
4. **HTTPBearer auto_error=False** → 手动 401，不自动 403，统一错误信息
5. **CurrentUser dataclass frozen** → 不可变注入，防路由内误写
6. **docker-compose 端口 5433:5432** → 内部 5432 不变，宿主机暴露 5433
7. **login.html 单文件** → 零外部依赖，fetch API 直连 /api/auth

## 鉴权全链路

```
注册: POST /api/auth/register → bcrypt 哈希 → INSERT users → 签发 JWT → 返回 token
登录: POST /api/auth/login   → SELECT users → bcrypt 比对 → 签发 JWT → 返回 token
API:  POST /api/tasks         → Depends(get_current_user) → JWT 解码 → CurrentUser.user_id
                              → _make_task_dict(..., user_id) → task dict → DB
```

## 验证结果

- 全部源文件 ast.parse 语法校验通过
- .venv 导入链完整验证通过: security.py → dependencies.py → UserDAO → auth_routes.router
- 三新依赖安装成功: PyJWT 2.13.0 + bcrypt 5.0.0 + passlib 1.7.4

## 剩余待做

- 首次部署时需手动执行 `INSERT INTO users` 或通过 register API 创建初始用户
- login.html 登录后跳 `/`，当前无主页 → 可后续加 dashboard
