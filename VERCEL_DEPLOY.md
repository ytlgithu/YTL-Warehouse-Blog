# Vercel 部署 YTL仓博系统 - 说明

## ⚠️ 重要限制

Vercel 是 **Serverless** 平台，有以下限制：

### 1. 数据库问题（最关键）
- ❌ **SQLite 不可用**：文件系统只读，每次请求可能在不同容器
- ✅ 需要外部 PostgreSQL（推荐 Vercel Postgres / Supabase / Neon / PlanetScale）
- 你的 `config.py` 已经支持 `DATABASE_URL` 环境变量，只需在 Vercel 设置即可

### 2. 文件上传
- ❌ 上传的文件不会持久保存
- ✅ 需要接对象存储（如 Vercel Blob / AWS S3 / Cloudflare R2）

### 3. 同步功能
- ❌ 后台同步线程无法运行（Serverless 无常驻进程）
- ✅ 需要改用 Cron Job 或移除

## 部署步骤

### 方案 A：纯展示博客（推荐先用这个验证）

1. 在 Vercel 项目设置中添加环境变量：
   ```
   SECRET_KEY=your-random-secret-key-here
   DATABASE_URL=postgresql://user:pass@host:5432/dbname
   ```

2. 推送代码到 GitHub（Vercel 自动部署）

3. 访问 Vercel 给的域名

### 方案 B：完整功能（需要额外服务）

1. **数据库**：用 [Supabase](https://supabase.com) 免费 PostgreSQL
   - 创建项目 → 获取连接串 → 填入 Vercel 环境变量

2. **文件存储**：用 [Cloudflare R2](https://r2.cloudflare.com) 免费对象存储

3. **后台任务**：用 Vercel Cron 或外部调度器

## 快速开始（最小可用版本）

只要设置了 `DATABASE_URL`，你的代码基本可以直接跑。
config.py 已经处理了 PostgreSQL 和 SQLite 的切换逻辑。

## 推荐数据库选择

| 服务 | 免费额度 | 说明 |
|------|---------|------|
| Supabase | 500MB | 最推荐，免费够用 |
| Neon | 0.5GB | Serverless PG，自动休眠 |
| PlanetScale | 5GB | MySQL，需改驱动 |
| Railway | $5/月 | 你之前用过 |

## 下一步

1. 选择一个 PostgreSQL 服务并创建数据库
2. 把连接字符串填到 Vercel 的 Environment Variables
3. 点 Deploy
