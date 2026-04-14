import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'ytl-blog-secret-2026')

    # Railway PostgreSQL（DATABASE_URL 由 Railway 自动注入）
    # 如果有 DATABASE_URL 则用 PostgreSQL，否则用本地 SQLite
    if os.environ.get('DATABASE_URL'):
        SQLALCHEMY_DATABASE_URI = os.environ['DATABASE_URL'].replace(
            'postgres://', 'postgresql://', 1  # Railway 旧格式兼容
        )
    else:
        DATA_DIR = os.path.join(BASE_DIR, 'instance')
        os.makedirs(DATA_DIR, exist_ok=True)
        SQLALCHEMY_DATABASE_URI = f'sqlite:///{os.path.join(DATA_DIR, "blog.db")}'

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 上传文件目录（优先用 Railway 持久化卷，否则用 /tmp）
    _vol = os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', '')
    UPLOAD_FOLDER = os.path.join(_vol, 'uploads') if _vol else '/tmp/uploads'
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
