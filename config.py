import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'ytl-blog-secret-2026')

    # Railway PostgreSQL（DATABASE_URL 由 Railway 自动注入，需通过 Reference Variable 引用）
    # 本地开发时使用 SQLite
    db_url = os.environ.get('DATABASE_URL', '')
    if db_url:
        # PostgreSQL 格式兼容（Railway / Supabase）
        uri = db_url.replace('postgres://', 'postgresql://', 1)
        # 使用纯 Python pg8000 驱动（避免编译依赖，Vercel 兼容）
        if '+pg8000' not in uri:
            uri = uri.replace('postgresql://', 'postgresql+pg8000://', 1)
        # Supabase 需要 SSL 连接
        if '?' not in uri:
            uri += '?sslmode=require'
        elif 'ssl' not in uri:
            uri += '&sslmode=require'
        SQLALCHEMY_DATABASE_URI = uri
    else:
        DATA_DIR = os.path.join(BASE_DIR, 'instance')
        os.makedirs(DATA_DIR, exist_ok=True)
        SQLALCHEMY_DATABASE_URI = f'sqlite:///{os.path.join(DATA_DIR, "blog.db")}'

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Railway 持久化卷 / Vercel 临时目录
    _vol = os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', '')
    _vercel = os.environ.get('VERCEL', '')
    if _vol:
        UPLOAD_FOLDER = os.path.join(_vol, 'uploads')
    elif _vercel:
        UPLOAD_FOLDER = '/tmp/uploads'
    else:
        UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    MAX_CONTENT_LENGTH = 2 * 1024 * 1024 * 1024  # 2GB
    MAX_FORM_MEMORY_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

    ALLOWED_EXTENSIONS = {
        'c', 'h', 'cpp', 'hpp', 'cc', 's', 'asm',
        'bin', 'img', 'hex', 'elf', 'fw',
        'py', 'js', 'ts', 'html', 'css', 'json', 'xml', 'yaml', 'yml',
        'md', 'txt', 'sh', 'bat', 'ps1',
        'zip', 'tar', 'gz', '7z',
        'pdf', 'doc', 'docx', 'xls', 'xlsx',
        'png', 'jpg', 'jpeg', 'gif', 'svg',
        'cmake', 'makefile', 'ini', 'cfg', 'conf', 'log',
    }
